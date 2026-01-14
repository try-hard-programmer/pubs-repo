// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with Redis Queue (Dual Connection)
// VERSION: 2.4 - Production Ready (Full)
// ==========================================

const express = require("express");
const axios = require("axios");
const Redis = require("ioredis");
const router = express.Router();

// ==========================================
// 1. REDIS SETUP (Dual Connection Strategy)
// ==========================================

const redisConfig = {
  host: process.env.REDIS_HOST || "localhost",
  port: process.env.REDIS_PORT || 6379,
  maxRetriesPerRequest: 3,
  // Note: No password field here. It will connect without auth by default.
};

// 1. General Client (Non-blocking: RPUSH, GET, SET)
const redisClient = new Redis(redisConfig);

// 2. Worker Client (Blocking: BLPOP only)
const redisBlocking = new Redis(redisConfig);

redisClient.on("error", (err) => console.error("Redis (Main) Error:", err));
redisBlocking.on("error", (err) => console.error("Redis (Block) Error:", err));

redisClient.on("connect", () => console.log("✓ Redis (Main) Connected"));
redisBlocking.on("connect", () => console.log("✓ Redis (Block) Connected"));

// Worker registry to keep track of active processors
const userWorkers = {};

// Lua script for atomic cleanup (Prevents race conditions when stopping workers)
const CLEANUP_SCRIPT = `
  local queueLen = redis.call('LLEN', KEYS[1])
  if queueLen == 0 then
    redis.call('DEL', KEYS[2])
    return 1
  end
  return 0
`;

// ==========================================
// 2. API CONFIGURATIONS
// ==========================================

const API_CONFIGS = {
  openai: {
    baseUrl: "https://api.openai.com/v1",
    chatModel: "gpt-4o-mini",
    visionModel: "gpt-4o-mini",
    embeddingModel: "text-embedding-3-small",
    maxTokens: 4096,
    supportsVision: true,
  },
  gemini: {
    baseUrl: "https://generativelanguage.googleapis.com/v1beta",
    chatModel: "gemini-2.0-flash-exp",
    visionModel: "gemini-2.0-flash-exp",
    embeddingModel: "text-embedding-004",
    maxTokens: 8192,
    supportsVision: true,
  },
  runpod: {
    baseUrl: process.env.RUNPOD_BASE_URL || "https://api.runpod.ai/v2",
    endpointId: process.env.RUNPOD_ENDPOINT_ID,
    chatModel: "openai-compatible",
    embeddingModel: "sentence-transformers",
    maxTokens: 4096,
    supportsVision: false,
  },
};

const CREDIT_COSTS = {
  basic_query: 1,
  file_search: 2,
  document_analysis: 3,
  image_analysis: 4,
  complex_query: 5,
  embedding: 0.5,
};

// ==========================================
// 3. UTILITY FUNCTIONS
// ==========================================

const getApiKey = (provider) => {
  const keys = {
    openai: process.env.OPENAI_API_KEY,
    gemini: process.env.GEMINI_API_KEY,
    runpod: process.env.RUNPOD_API_KEY,
  };
  return keys[provider];
};

const authenticate = (req, res, next) => {
  if (!process.env.SERVICE_API_KEY) return next();
  const apiKey = req.headers["x-service-key"];
  if (apiKey !== process.env.SERVICE_API_KEY) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
};

const detectFiles = (files) => {
  if (!files || !Array.isArray(files) || files.length === 0) return null;
  const fileTypes = files.map((f) => f.type);
  if (fileTypes.includes("image")) return "image";
  if (fileTypes.includes("pdf")) return "pdf";
  return "unknown";
};

async function downloadFileAsBase64(url) {
  try {
    const response = await axios.get(url, {
      responseType: "arraybuffer",
      timeout: 30000,
    });
    const base64 = Buffer.from(response.data).toString("base64");
    return {
      base64,
      mimeType: response.headers["content-type"] || "application/octet-stream",
    };
  } catch (error) {
    throw new Error(`Failed to download file from ${url}`);
  }
}

function getProvider(requested) {
  if (
    requested &&
    process.env.ALLOW_PROVIDER_OVERRIDE === "true" &&
    API_CONFIGS[requested]
  )
    return requested;
  return process.env.PRIMARY_LLM_PROVIDER || "openai";
}

function detectQueryType(messages, files) {
  const lastMessage = messages[messages.length - 1]?.content || "";
  const hasFiles = files && files.length > 0;
  if (hasFiles && files[0]?.type === "image") return "image_analysis";
  if (hasFiles && files[0]?.type === "pdf") return "document_analysis";
  if (lastMessage.length < 50) return "basic_query";
  if (lastMessage.length > 200) return "complex_query";
  return "basic_query";
}

function calculateTokenCost(provider, input, output) {
  const pricing = {
    openai: { input: 0.15 / 1e6, output: 0.6 / 1e6 },
    gemini: { input: 0.075 / 1e6, output: 0.3 / 1e6 },
    runpod: { input: 0, output: 0 },
  };
  const rates = pricing[provider] || pricing.gemini;
  return input * rates.input + output * rates.output;
}

function calculateEmbeddingCost(provider, tokens) {
  const pricing = {
    openai: 0.02 / 1e6,
    gemini: 0.025 / 1e6,
    runpod: 0,
  };
  const rate = pricing[provider] || pricing.openai;
  return tokens * rate;
}

// ==========================================
// 4. LOGGING FUNCTIONS
// ==========================================

async function logCreditUsage(
  orgId,
  queryType,
  response,
  provider,
  startTime,
  priority = null
) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS[queryType];
  const tokenCost = calculateTokenCost(
    provider,
    response.usage?.prompt_tokens || 0,
    response.usage?.completion_tokens || 0
  );

  return {
    organization_id: orgId,
    query_type: queryType,
    credits_used: credits,
    response_time_ms: responseTime,
    provider: provider,
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };
}

async function logEmbeddingUsage(orgId, response, provider, startTime) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS.embedding;
  const tokens =
    response.usage?.total_tokens || response.usage?.prompt_tokens || 0;
  const tokenCost = calculateEmbeddingCost(provider, tokens);

  return {
    organization_id: orgId,
    query_type: "embedding",
    credits_used: credits,
    response_time_ms: responseTime,
    provider: provider,
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };
}

// ==========================================
// 5. CHAT PROVIDER HANDLERS
// ==========================================

async function handleOpenAI(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.openai;
  const hasFiles = files && files.length > 0;
  const model = hasFiles ? config.visionModel : config.chatModel;

  let processedMessages = messages;
  if (hasFiles) {
    const lastUserIndex = messages
      .map((m, i) => ({ role: m.role, index: i }))
      .reverse()
      .find((m) => m.role === "user").index;
    processedMessages = [...messages];
    const lastMessage = processedMessages[lastUserIndex];
    const content = [{ type: "text", text: lastMessage.content || "" }];
    for (const file of files) {
      if (file.type === "image") {
        content.push({
          type: "image_url",
          image_url: { url: file.url || `data:image/jpeg;base64,${file.data}` },
        });
      }
    }
    processedMessages[lastUserIndex] = { ...lastMessage, content: content };
  }

  const response = await axios.post(
    `${config.baseUrl}/chat/completions`,
    {
      model,
      messages: processedMessages,
      temperature,
      max_tokens: config.maxTokens,
    },
    {
      headers: { Authorization: `Bearer ${getApiKey("openai")}` },
      timeout: 180000,
    }
  );
  return response.data;
}

async function handleGemini(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.gemini;
  const hasFiles = files && files.length > 0;
  const model = hasFiles ? config.visionModel : config.chatModel;
  const apiKey = getApiKey("gemini");

  const contents = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const parts = [];
    if (msg.content) parts.push({ text: msg.content });
    if (msg.role === "user" && hasFiles && i === messages.length - 1) {
      for (const file of files) {
        if (file.type === "image") {
          let base64Data;
          if (file.url) {
            const { base64 } = await downloadFileAsBase64(file.url);
            base64Data = base64;
          } else if (file.data) base64Data = file.data;
          if (base64Data)
            parts.push({
              inline_data: { mime_type: "image/jpeg", data: base64Data },
            });
        }
      }
    }
    contents.push({
      role: msg.role === "assistant" ? "model" : "user",
      parts: parts,
    });
  }

  try {
    const response = await axios.post(
      `${config.baseUrl}/models/${model}:generateContent?key=${apiKey}`,
      {
        contents,
        generationConfig: { temperature, maxOutputTokens: config.maxTokens },
      },
      { headers: { "Content-Type": "application/json" }, timeout: 180000 }
    );

    // [FIX] Safety Check to prevent Crash
    const candidate = response.data.candidates?.[0];
    const text =
      candidate?.content?.parts?.[0]?.text ||
      "⚠️ I cannot answer this due to safety filters.";

    return {
      choices: [
        {
          message: {
            role: "assistant",
            content: text,
          },
        },
      ],
      usage: { prompt_tokens: 0, completion_tokens: 0 },
    };
  } catch (error) {
    console.error("Gemini API Error:", error.response?.data || error.message);
    throw error; // Let the main handler switch to fallback (OpenAI)
  }
}

async function handleRunPod(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.runpod;
  const response = await axios.post(
    `${config.baseUrl}/${config.endpointId}/runsync`,
    { input: { messages, temperature, max_tokens: config.maxTokens } },
    {
      headers: { Authorization: `Bearer ${getApiKey("runpod")}` },
      timeout: 180000,
    }
  );
  return response.data.output || response.data;
}

async function routeWithFallback(
  provider,
  messages,
  files = [],
  temperature = 0.7
) {
  try {
    if (provider === "openai")
      return await handleOpenAI(messages, files, temperature);
    if (provider === "gemini")
      return await handleGemini(messages, files, temperature);
    return await handleRunPod(messages, files, temperature);
  } catch (error) {
    console.error(`[FALLBACK] ${provider} failed: ${error.message}`);
    const fallback = ["openai", "gemini", "runpod"].find(
      (p) => p !== provider && getApiKey(p)
    );
    if (fallback) {
      if (fallback === "openai")
        return await handleOpenAI(messages, files, temperature);
      if (fallback === "gemini")
        return await handleGemini(messages, files, temperature);
      return await handleRunPod(messages, files, temperature);
    }
    throw new Error("All providers failed");
  }
}

// ==========================================
// 6. EMBEDDING FUNCTIONS (The missing link!)
// ==========================================

async function getOpenAIEmbeddings(texts) {
  const config = API_CONFIGS.openai;
  const response = await axios.post(
    `${config.baseUrl}/embeddings`,
    { model: config.embeddingModel, input: texts },
    {
      headers: { Authorization: `Bearer ${getApiKey("openai")}` },
      timeout: 60000,
    }
  );
  return response.data;
}

async function getGeminiEmbeddings(texts) {
  const config = API_CONFIGS.gemini;
  const apiKey = getApiKey("gemini");
  const requests = Array.isArray(texts) ? texts : [texts];
  const embeddings = [];
  let totalTokens = 0;

  for (const text of requests) {
    const response = await axios.post(
      `${config.baseUrl}/models/${config.embeddingModel}:embedContent?key=${apiKey}`,
      { content: { parts: [{ text: text }] } },
      { headers: { "Content-Type": "application/json" }, timeout: 60000 }
    );
    embeddings.push({
      object: "embedding",
      embedding: response.data.embedding.values,
      index: embeddings.length,
    });
    totalTokens += Math.ceil(text.length / 4);
  }
  return {
    object: "list",
    data: embeddings,
    model: config.embeddingModel,
    usage: { prompt_tokens: totalTokens, total_tokens: totalTokens },
  };
}

async function getRunPodEmbeddings(texts) {
  const config = API_CONFIGS.runpod;
  const response = await axios.post(
    `${config.baseUrl}/${config.endpointId}/runsync`,
    { input: { texts, model: config.embeddingModel } },
    {
      headers: { Authorization: `Bearer ${getApiKey("runpod")}` },
      timeout: 60000,
    }
  );
  const estimatedTokens = (Array.isArray(texts) ? texts : [texts]).reduce(
    (sum, t) => sum + Math.ceil(t.length / 4),
    0
  );
  return {
    ...(response.data.output || response.data),
    usage: { prompt_tokens: estimatedTokens, total_tokens: estimatedTokens },
  };
}

async function routeEmbeddings(texts, provider = null) {
  const p = provider || process.env.EMBEDDING_PROVIDER || "openai";
  if (p === "gemini") return await getGeminiEmbeddings(texts);
  if (p === "runpod") return await getRunPodEmbeddings(texts);
  return await getOpenAIEmbeddings(texts);
}

// ==========================================
// 7. WORKER SYSTEM (Dual Connection)
// ==========================================

async function processUserQueue(userId) {
  const queueKey = `queue:${userId}`;
  const lockKey = `lock:${userId}`;

  try {
    const locked = await redisClient.set(lockKey, "1", "EX", 300, "NX");
    if (!locked) return;

    console.log(`[${userId}] Worker started.`);

    while (true) {
      // 1. BLOCKING POP (Uses Worker Client)
      const jobData = await redisBlocking.blpop(queueKey, 1);

      if (!jobData) {
        // 2. ATOMIC CLEANUP (Uses Main Client)
        const deleted = await redisClient.eval(
          CLEANUP_SCRIPT,
          2,
          queueKey,
          lockKey
        );
        if (deleted === 1) {
          delete userWorkers[userId];
          console.log(`[${userId}] Worker stopped (Idle).`);
          break;
        } else {
          continue;
        }
      }

      const [, jobStr] = jobData;
      const job = JSON.parse(jobStr);
      console.log(`[${userId}] Processing Job ${job.jobId}`);

      try {
        const response = await routeWithFallback(
          job.provider,
          job.messages,
          job.files,
          job.temperature
        );

        const queryType = detectQueryType(job.messages, job.files);
        const creditUsage = await logCreditUsage(
          job.organization_id,
          queryType,
          response,
          job.provider,
          job.startTime,
          job.category
        );

        const finalResponse = {
          ...response,
          metadata: {
            request_id: job.requestId,
            provider: job.provider,
            nameUser: job.nameUser || "Anonymous",
            hasFiles: job.files.length > 0,
            timestamp: new Date().toISOString(),
            query_type: queryType,
            priority: job.category || null,
            credits_used: creditUsage.credits_used,
            response_time_ms: creditUsage.response_time_ms,
            cost_usd: creditUsage.cost_usd,
          },
        };

        await redisClient.setex(
          `result:${job.jobId}`,
          300,
          JSON.stringify({ success: true, data: finalResponse })
        );
        console.log(`[${userId}] Job ${job.jobId} completed`);
      } catch (error) {
        console.error(`[${userId}] Job Failed: ${error.message}`);
        await redisClient.setex(
          `result:${job.jobId}`,
          300,
          JSON.stringify({ success: false, error: error.message })
        );
      }
    }
  } catch (error) {
    console.error(`[${userId}] Worker Crashed:`, error);
    await redisClient.del(lockKey).catch(() => {});
    delete userWorkers[userId];
  }
}

async function waitForResult(jobId, timeoutMs, req) {
  const startTime = Date.now();
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      // 1. Timeout Check (Keep this)
      if (Date.now() - startTime > timeoutMs) {
        clearInterval(interval);
        reject(new Error("Timeout"));
        return;
      }

      // [FIX] REMOVED "req.destroyed" CHECK
      // It was causing false positives (Ghost Disconnects) in 0.1s.
      /* if (req.destroyed) {
        clearInterval(interval);
        reject(new Error("Client disconnected"));
        return;
      }
      */

      // 3. Poll Redis (Keep this)
      const result = await redisClient.get(`result:${jobId}`);
      if (result) {
        clearInterval(interval);
        await redisClient.del(`result:${jobId}`);
        resolve(JSON.parse(result));
      }
    }, 100);
  });
}

// ==========================================
// 8. ROUTES
// ==========================================

router.get("/test", (req, res) => {
  res.json({
    message: "Proxy Router v2 - Production + Queue",
    timestamp: new Date(),
    redis_status: "Dual Connection Active",
  });
});

router.post("/chat", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `req-${Date.now()}`;

  try {
    const {
      messages,
      files = [],
      temperature = 0.7,
      provider: reqProvider,
      organization_id,
      category,
      nameUser,
    } = req.body;

    if (!messages || !Array.isArray(messages))
      return res.status(400).json({ error: "Missing messages array" });

    const userId = organization_id || "default_org";
    const jobId = `${userId}-${Date.now()}-${Math.random()
      .toString(36)
      .substr(2, 9)}`;
    const provider = getProvider(reqProvider);

    const job = {
      jobId,
      requestId,
      provider,
      messages,
      files,
      temperature,
      organization_id,
      category,
      nameUser,
      startTime,
    };

    // Push to Redis (Main Client)
    await redisClient.rpush(`queue:${userId}`, JSON.stringify(job));
    console.log(`[${userId}] Job ${jobId} Queued`);

    if (!userWorkers[userId]) {
      userWorkers[userId] = processUserQueue(userId);
    }

    const result = await waitForResult(jobId, 180000, req);

    if (result.success) {
      res.json(result.data);
    } else {
      res.status(500).json({ error: result.error });
    }
  } catch (error) {
    if (error.message !== "Client disconnected") {
      console.error(`[ERROR] ${requestId}: ${error.message}`);
    }
    if (!res.headersSent) res.status(500).json({ error: error.message });
  }
});

router.post("/embeddings", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `embed-${Date.now()}`;

  try {
    const { texts, input, provider, organization_id } = req.body;
    const textsToEmbed = texts || input;

    if (!textsToEmbed) {
      return res.status(400).json({ error: "Missing texts or input" });
    }

    const embeddingProvider =
      provider || process.env.EMBEDDING_PROVIDER || "openai";
    const response = await routeEmbeddings(textsToEmbed, embeddingProvider);

    const embeddingUsage = await logEmbeddingUsage(
      organization_id,
      response,
      embeddingProvider,
      startTime
    );

    const finalResponse = {
      ...response,
      metadata: {
        request_id: requestId,
        provider: embeddingProvider,
        timestamp: new Date().toISOString(),
        credits_used: embeddingUsage.credits_used,
        response_time_ms: embeddingUsage.response_time_ms,
        cost_usd: embeddingUsage.cost_usd,
      },
    };

    res.json(finalResponse);
  } catch (error) {
    console.error(`[ERROR] ${requestId}: ${error.message}`);
    res.status(error.response?.status || 500).json({
      error: error.message || "Internal server error",
      request_id: requestId,
    });
  }
});

// Graceful shutdown
process.on("SIGTERM", async () => {
  console.log("Shutting down...");
  await redisClient.quit();
  await redisBlocking.quit();
  process.exit(0);
});

module.exports = router;

console.log(`🚀 Proxy Router v2 loaded [Production Mode] + Dual Redis Queue`);
