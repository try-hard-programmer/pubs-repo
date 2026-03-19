// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with Redis Queue (Dual Connection)
// VERSION: 3.1 - OpenAI Only (Race Condition Fixes)
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
};

// Lua: atomically check queue empty → delete lock
const CLEANUP_SCRIPT = `
  local queueLen = redis.call('LLEN', KEYS[1])
  if queueLen == 0 then
    redis.call('DEL', KEYS[2])
    return 1
  end
  return 0
`;

// Lua: atomically check no active worker → set lock
// Prevents two workers from spawning on the same queue
const ACQUIRE_WORKER_SCRIPT = `
  local current = redis.call('GET', KEYS[1])
  if current then
    return 0
  end
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
`;

// 1. General Client (Non-blocking: RPUSH, GET, SET)
const redisClient = new Redis(redisConfig);

// 2. Worker Client (Blocking: BLPOP only)
const redisBlocking = new Redis(redisConfig);

redisClient.on("error", (err) => console.error("Redis (Main) Error:", err));
redisBlocking.on("error", (err) => console.error("Redis (Block) Error:", err));

redisClient.on("connect", () => console.log("✓ Redis (Main) Connected"));
redisBlocking.on("connect", () => console.log("✓ Redis (Block) Connected"));

const userWorkers = {};

// ==========================================
// 2. API CONFIGURATIONS
// ==========================================

const API_CONFIGS = {
  openai: {
    baseUrl: "https://api.openai.com/v1",
    chatModel: "gpt-4o-mini",
    visionModel: "gpt-4o-mini",
    embeddingModel: "text-embedding-3-small",
    maxTokens: 8192,
    supportsVision: true,
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
// 3. COST LOGGER
// ==========================================

const USD_TO_IDR = process.env.USD_TO_IDR
  ? parseFloat(process.env.USD_TO_IDR)
  : 16900;

function toRupiah(usd) {
  const idr = usd * USD_TO_IDR;
  return new Intl.NumberFormat("id-ID", {
    style: "currency",
    currency: "IDR",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(idr);
}

function logCost(route, requestId, details = {}) {
  const {
    orgId = "-",
    queryType = "-",
    inputTokens = 0,
    outputTokens = 0,
    totalTokens = 0,
    costUsd = 0,
    responseMs = 0,
    extra = "",
  } = details;

  const costIdr = toRupiah(costUsd);
  const usdStr = `$${costUsd.toFixed(8)}`;

  console.log(
    `[COST] [${route}] [${requestId}] org=${orgId} | type=${queryType}` +
      (inputTokens || outputTokens
        ? ` | tokens=in:${inputTokens}+out:${outputTokens}`
        : totalTokens
          ? ` | tokens=${totalTokens}`
          : "") +
      ` | cost=${usdStr} (${costIdr}) | ${responseMs}ms` +
      (extra ? ` | ${extra}` : ""),
  );
}

// ==========================================
// 4. UTILITY FUNCTIONS
// ==========================================

const getApiKey = () => process.env.OPENAI_API_KEY;

const authenticate = (req, res, next) => {
  if (!process.env.SERVICE_API_KEY) return next();
  const apiKey = req.headers["x-service-key"];
  if (apiKey !== process.env.SERVICE_API_KEY) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
};

function detectQueryType(messages, files) {
  const lastMessage = messages[messages.length - 1]?.content || "";

  const hasInlineImage =
    Array.isArray(lastMessage) &&
    lastMessage.some((m) => m.type === "image_url");

  const hasFiles = files && files.length > 0;

  if (hasFiles && files[0]?.type === "image") return "image_analysis";
  if (hasInlineImage) return "image_analysis";
  if (hasFiles && files[0]?.type === "pdf") return "document_analysis";

  const textLen = Array.isArray(lastMessage)
    ? lastMessage.find((m) => m.type === "text")?.text?.length || 0
    : lastMessage.length;

  if (textLen < 50) return "basic_query";
  if (textLen > 200) return "complex_query";
  return "basic_query";
}

function calculateTokenCost(input, output) {
  const rates = { input: 0.15 / 1e6, output: 0.6 / 1e6 };
  return input * rates.input + output * rates.output;
}

const EMBEDDING_RATES_USD = {
  "text-embedding-3-small": 0.02 / 1e6,
  "text-embedding-3-large": 0.13 / 1e6,
  "text-embedding-ada-002": 0.10 / 1e6,
};

function calculateEmbeddingCost(tokens, model) {
  const rate = EMBEDDING_RATES_USD[model] || 0.02 / 1e6;
  return tokens * rate;
}

// ==========================================
// 5. SHARED: SAFE RESULT POLLING
// ==========================================

// Recursive setTimeout — no stacking risk unlike setInterval+async
function waitForResult(jobId, timeoutMs) {
  const startTime = Date.now();
  const resultKey = `result:${jobId}`;
  let pollInterval = 100;
  const maxInterval = 500;

  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        if (Date.now() - startTime > timeoutMs) {
          reject(new Error("Timeout"));
          return;
        }

        const result = await redisClient.get(resultKey);
        if (result) {
          await redisClient.del(resultKey);
          resolve(JSON.parse(result));
          return;
        }

        pollInterval = Math.min(pollInterval * 1.2, maxInterval);
        setTimeout(poll, pollInterval);
      } catch (err) {
        reject(err);
      }
    };

    poll();
  });
}

// ==========================================
// 6. LOGGING FUNCTIONS
// ==========================================

async function logCreditUsage(
  orgId,
  queryType,
  response,
  startTime,
  priority = null,
) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS[queryType];
  const tokenCost = calculateTokenCost(
    response.usage?.prompt_tokens || 0,
    response.usage?.completion_tokens || 0,
  );

  return {
    organization_id: orgId,
    query_type: queryType,
    credits_used: credits,
    response_time_ms: responseTime,
    provider: "openai",
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };
}

async function logEmbeddingUsage(orgId, response, startTime) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS.embedding;
  const tokens =
    response.usage?.total_tokens || response.usage?.prompt_tokens || 0;
  const tokenCost = calculateEmbeddingCost(tokens);

  return {
    organization_id: orgId,
    query_type: "embedding",
    credits_used: credits,
    response_time_ms: responseTime,
    provider: "openai",
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };
}

// ==========================================
// 7. CHAT HANDLER (OpenAI Only — supports tools/MCP)
// ==========================================

async function handleOpenAI(
  messages,
  files = [],
  temperature = 0.7,
  tools = null,
  tool_choice = null,
) {
  const config = API_CONFIGS.openai;

  const hasInlineImages = messages.some(
    (m) =>
      Array.isArray(m.content) && m.content.some((p) => p.type === "image_url"),
  );
  const hasFiles = files && files.length > 0;

  const model =
    hasFiles || hasInlineImages ? config.visionModel : config.chatModel;

  let processedMessages = messages;

  if (hasFiles) {
    const lastUserIndex = messages
      .map((m, i) => ({ role: m.role, index: i }))
      .reverse()
      .find((m) => m.role === "user").index;

    processedMessages = [...messages];
    const lastMessage = processedMessages[lastUserIndex];

    let content = [];
    if (typeof lastMessage.content === "string") {
      content.push({ type: "text", text: lastMessage.content });
    } else if (Array.isArray(lastMessage.content)) {
      content = [...lastMessage.content];
    }

    for (const file of files) {
      if (file.type === "image") {
        content.push({
          type: "image_url",
          image_url: {
            url: file.url || `data:image/jpeg;base64,${file.data}`,
          },
        });
      }
    }
    processedMessages[lastUserIndex] = { ...lastMessage, content };
  }

  const requestBody = {
    model,
    messages: processedMessages,
    temperature,
    max_tokens: config.maxTokens,
  };

  if (tools && tools.length > 0) {
    requestBody.tools = tools;
    if (tool_choice) requestBody.tool_choice = tool_choice;
  }

  const response = await axios.post(
    `${config.baseUrl}/chat/completions`,
    requestBody,
    {
      headers: { Authorization: `Bearer ${getApiKey()}` },
      timeout: 180000,
    },
  );
  return response.data;
}

// ==========================================
// 8. FILE MANAGER HANDLER (OpenAI — no tools, supports response_format)
// ==========================================

async function handleOpenAIFileManager(
  messages,
  files = [],
  temperature = 0.7,
  response_format = null,
) {
  const config = API_CONFIGS.openai;

  const hasInlineImages = messages.some(
    (m) =>
      Array.isArray(m.content) && m.content.some((p) => p.type === "image_url"),
  );
  const hasFiles = files && files.length > 0;
  const model =
    hasFiles || hasInlineImages ? config.visionModel : config.chatModel;

  let processedMessages = [...messages];

  if (hasFiles) {
    const lastUserIndex = messages
      .map((m, i) => ({ role: m.role, index: i }))
      .reverse()
      .find((m) => m.role === "user")?.index;

    if (lastUserIndex !== undefined) {
      const lastMessage = processedMessages[lastUserIndex];
      let content = [];
      if (typeof lastMessage.content === "string") {
        content.push({ type: "text", text: lastMessage.content });
      } else if (Array.isArray(lastMessage.content)) {
        content = [...lastMessage.content];
      }

      for (const file of files) {
        if (file.type === "image") {
          content.push({
            type: "image_url",
            image_url: {
              url: file.url || `data:image/jpeg;base64,${file.data}`,
            },
          });
        }
      }
      processedMessages[lastUserIndex] = { ...lastMessage, content };
    }
  }

  const body = {
    model,
    messages: processedMessages,
    temperature,
    max_tokens: config.maxTokens,
  };
  if (response_format) body.response_format = response_format;

  const response = await axios.post(
    `${config.baseUrl}/chat/completions`,
    body,
    {
      headers: { Authorization: `Bearer ${getApiKey()}` },
      timeout: 180000,
    },
  );

  return response.data;
}

// ==========================================
// 9. EMBEDDING FUNCTIONS
// ==========================================

async function getOpenAIEmbeddings(texts) {
  const config = API_CONFIGS.openai;
  const response = await axios.post(
    `${config.baseUrl}/embeddings`,
    { model: config.embeddingModel, input: texts },
    {
      headers: { Authorization: `Bearer ${getApiKey()}` },
      timeout: 60000,
    },
  );
  return response.data;
}

// ==========================================
// 10. CHAT WORKER SYSTEM
// ==========================================

async function processUserQueue(userId) {
  const queueKey = `queue:${userId}`;
  const workerId = `${process.env.HOSTNAME || process.env.NODE_NAME || process.env.TASK_SLOT || "unknown"}-${Date.now()}`;

  console.log(`[WORKER] [${workerId}] Started for user ${userId}`);

  try {
    while (true) {
      const jobData = await redisBlocking.blpop(queueKey, 5);

      if (!jobData) {
        const queueLen = await redisClient.llen(queueKey);
        if (queueLen === 0) {
          delete userWorkers[userId];
          console.log(`[WORKER] [${workerId}] Stopped - Queue empty`);
          break;
        }
        continue;
      }

      const [, jobStr] = jobData;
      const job = JSON.parse(jobStr);

      console.log(`[WORKER] [${workerId}] Processing: ${job.requestId}`);

      try {
        const llmStart = Date.now();
        const response = await handleOpenAI(
          job.messages,
          job.files,
          job.temperature,
          job.tools,
          job.tool_choice,
        );

        const queryType = detectQueryType(job.messages, job.files);
        const creditUsage = await logCreditUsage(
          job.organization_id,
          queryType,
          response,
          job.startTime,
          job.category,
        );

        const finalResponse = {
          ...response,
          metadata: {
            request_id: job.requestId,
            provider: "openai",
            nameUser: job.nameUser || "Anonymous",
            hasFiles: job.files.length > 0,
            timestamp: new Date().toISOString(),
            query_type: queryType,
            priority: job.category || null,
            credits_used: creditUsage.credits_used,
            response_time_ms: creditUsage.response_time_ms,
            llm_time_ms: Date.now() - llmStart,
            cost_usd: creditUsage.cost_usd,
            worker_id: workerId,
          },
        };

        await redisClient.setex(
          `result:${job.jobId}`,
          300,
          JSON.stringify({ success: true, data: finalResponse }),
        );

        logCost("/chat", job.requestId, {
          orgId: job.organization_id,
          queryType: queryType,
          inputTokens: response.usage?.prompt_tokens || 0,
          outputTokens: response.usage?.completion_tokens || 0,
          costUsd: creditUsage.cost_usd,
          responseMs: creditUsage.response_time_ms,
        });
        console.log(
          `[WORKER] [${workerId}] Completed: ${job.requestId} (${creditUsage.response_time_ms}ms)`,
        );
      } catch (error) {
        console.error(`[WORKER] [${workerId}] Failed: ${error.message}`);
        await redisClient.setex(
          `result:${job.jobId}`,
          300,
          JSON.stringify({ success: false, error: error.message }),
        );
      }
    }
  } catch (error) {
    console.error(`[WORKER] [${workerId}] Crashed: ${error.message}`);
    delete userWorkers[userId];
  }
}

// ==========================================
// 11. FILE MANAGER WORKER SYSTEM
// ==========================================

const FM_LOCK_TTL = 600; // 10 min — enough for large batch bursts
const FM_BLPOP_TIMEOUT = 5; // 5s — reduces Redis chatter vs 1s

async function fileManagerWorker(userId) {
  const queueKey = `queue_filemanager:${userId}`;
  const lockKey = `lock_filemanager:${userId}`;
  const workerId = `fm-${process.env.HOSTNAME || "unknown"}-${Date.now()}`;

  console.log(`[FM-WORKER] [${workerId}] Started for ${userId}`);

  try {
    while (true) {
      // Refresh lock TTL while actively working — prevents expiry mid-batch
      await redisClient.expire(lockKey, FM_LOCK_TTL);

      const jobData = await redisBlocking.blpop(queueKey, FM_BLPOP_TIMEOUT);

      if (!jobData) {
        // No job within timeout — atomically check + cleanup
        const deleted = await redisClient.eval(
          CLEANUP_SCRIPT,
          2,
          queueKey,
          lockKey,
        );
        if (deleted === 1) {
          console.log(`[FM-WORKER] [${workerId}] Stopped (Idle).`);
          break;
        }
        // Queue got a new item between blpop timeout and eval — loop back
        continue;
      }

      const [, jobStr] = jobData;
      const j = JSON.parse(jobStr);
      console.log(`[FM-WORKER] [${workerId}] Processing: ${j.jobId}`);

      try {
        const llmStart = Date.now();
        const response = await handleOpenAIFileManager(
          j.messages,
          j.files,
          j.temperature,
          j.response_format,
        );

        const queryType = detectQueryType(j.messages, j.files);
        const creditUsage = await logCreditUsage(
          j.organization_id,
          queryType,
          response,
          j.startTime,
        );

        const finalResponse = {
          ...response,
          metadata: {
            request_id: j.requestId,
            provider: "openai",
            nameUser: j.nameUser || "Anonymous",
            hasFiles: j.files.length > 0,
            timestamp: new Date().toISOString(),
            query_type: queryType,
            priority: j.category || null,
            credits_used: creditUsage.credits_used,
            response_time_ms: creditUsage.response_time_ms,
            llm_time_ms: Date.now() - llmStart,
            cost_usd: creditUsage.cost_usd,
            worker_id: workerId,
          },
        };

        await redisClient.setex(
          `result:${j.jobId}`,
          300,
          JSON.stringify({ success: true, data: finalResponse }),
        );

        logCost("/chat/filemanager", j.requestId, {
          orgId: j.organization_id,
          queryType: queryType,
          inputTokens: response.usage?.prompt_tokens || 0,
          outputTokens: response.usage?.completion_tokens || 0,
          costUsd: creditUsage.cost_usd,
          responseMs: creditUsage.response_time_ms,
        });
        console.log(
          `[FM-WORKER] [${workerId}] Completed: ${j.jobId} (${creditUsage.response_time_ms}ms)`,
        );
      } catch (error) {
        console.error(`[FM-WORKER] [${workerId}] Job Failed: ${error.message}`);
        await redisClient.setex(
          `result:${j.jobId}`,
          300,
          JSON.stringify({ success: false, error: error.message }),
        );
      }
    }
  } catch (error) {
    console.error(`[FM-WORKER] [${workerId}] Crashed: ${error.message}`);
    await redisClient.del(lockKey).catch(() => {});
  }
}

// ==========================================
// 12. ROUTES
// ==========================================

router.get("/test", (req, res) => {
  res.json({
    message: "Proxy Router v3.1 - OpenAI Only + Queue (Race Fix)",
    timestamp: new Date(),
    redis_status: "Dual Connection Active",
  });
});

// ---------- CHAT (MCP/Tools) ----------

router.post("/chat", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `req-${Date.now()}`;

  console.log(
    `[CHAT] [${requestId}] Incoming Request from ${req.body.organization_id || "anon"}`,
  );

  try {
    const {
      messages,
      files = [],
      temperature = 0.7,
      organization_id,
      category,
      nameUser,
      ticket_id,
      ticket_categories = [],
    } = req.body;

    if (!messages || !Array.isArray(messages))
      return res.status(400).json({ error: "Missing messages array" });

    const userId = organization_id || "default_org";
    const jobId = `${userId}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

    const job = {
      jobId,
      requestId,
      provider: "openai",
      messages,
      files,
      temperature,
      organization_id,
      category,
      nameUser,
      startTime,
      tools: req.body.tools || null,
      tool_choice: req.body.tool_choice || null,
    };

    await redisClient.rpush(`queue:${userId}`, JSON.stringify(job));

    if (!userWorkers[userId]) {
      userWorkers[userId] = processUserQueue(userId);
    }

    const result = await waitForResult(jobId, 180000);

    if (result.success) {
      res.json(result.data);

      if (category?.toLowerCase() === "low" && ticket_id) {
        const aiResponse = result.data.choices[0].message.content;
        updateTicket(ticket_id, category, ticket_categories, aiResponse);
      }
    } else {
      res.status(500).json({ error: result.error });
    }
  } catch (error) {
    if (error.message === "Timeout") {
      console.error(`[ERROR] ${requestId}: Request timed out`);
      if (!res.headersSent) {
        res.status(504).json({
          error: "Request timed out. Please try again.",
          request_id: requestId,
        });
      }
    } else if (error.message !== "Client disconnected") {
      console.error(`[ERROR] ${requestId}: ${error.message}`);
      if (!res.headersSent) {
        res.status(500).json({ error: error.message });
      }
    }
  }
});

// ---------- FILE MANAGER (Single) ----------

router.post("/chat/filemanager", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `req-${Date.now()}`;

  try {
    const {
      messages,
      files = [],
      temperature = 0.7,
      organization_id,
      response_format,
      category,
      nameUser,
      ticket_id,
      ticket_categories = [],
    } = req.body;

    if (!messages || !Array.isArray(messages)) {
      return res.status(400).json({ error: "Missing messages array" });
    }

    const userId = organization_id || "default_org";
    const queueKey = `queue_filemanager:${userId}`;
    const lockKey = `lock_filemanager:${userId}`;
    const jobId = `${userId}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

    const job = {
      jobId,
      requestId,
      provider: "openai",
      messages,
      files,
      temperature,
      organization_id,
      response_format,
      category,
      nameUser,
      startTime,
    };

    await redisClient.rpush(queueKey, JSON.stringify(job));

    // Atomic lock acquisition — prevents dual worker spawn
    const acquired = await redisClient.eval(
      ACQUIRE_WORKER_SCRIPT,
      1,
      lockKey,
      "1",
      String(FM_LOCK_TTL),
    );
    if (acquired === 1) {
      // Fire-and-forget worker — don't await
      fileManagerWorker(userId).catch((err) => {
        console.error(
          `[FM-WORKER] Unhandled crash for ${userId}: ${err.message}`,
        );
        redisClient.del(lockKey).catch(() => {});
      });
    }

    const result = await waitForResult(jobId, 180000);

    if (result.success) {
      res.json(result.data);

      if (category?.toLowerCase() === "low" && ticket_id) {
        const aiResponse = result.data.choices[0].message.content;
        updateTicket(ticket_id, category, ticket_categories, aiResponse);
      }
    } else {
      res.status(500).json({ error: result.error });
    }
  } catch (error) {
    if (error.message === "Timeout") {
      console.error(`[ERROR] ${requestId}: Request timed out`);
      if (!res.headersSent) {
        res.status(504).json({
          error: "Request timed out. Please try again.",
          request_id: requestId,
        });
      }
    } else if (error.message !== "Client disconnected") {
      console.error(`[ERROR] ${requestId}: ${error.message}`);
    }
    if (!res.headersSent) res.status(500).json({ error: error.message });
  }
});

// ---------- FILE MANAGER (Batch) ----------

router.post("/chat/filemanager/batch", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `batch-${Date.now()}`;

  try {
    const { operations, organization_id, category, nameUser } = req.body;

    if (!operations || !Array.isArray(operations) || operations.length === 0) {
      return res
        .status(400)
        .json({ error: "Missing or empty operations array" });
    }

    if (operations.length > 50) {
      return res.status(400).json({ error: "Maximum 50 operations per batch" });
    }

    const userId = organization_id || "default_org";
    const queueKey = `queue_filemanager:${userId}`;
    const lockKey = `lock_filemanager:${userId}`;

    console.log(
      `[FM-BATCH] [${requestId}] ${operations.length} ops from ${userId}`,
    );

    // Enqueue all jobs in a single Redis pipeline
    const jobIds = [];
    const pipeline = redisClient.pipeline();

    for (let i = 0; i < operations.length; i++) {
      const op = operations[i];
      const jobId = `${userId}-${Date.now()}-${i}-${Math.random().toString(36).substr(2, 9)}`;
      jobIds.push(jobId);

      const job = {
        jobId,
        requestId: `${requestId}-${i}`,
        provider: "openai",
        messages: op.messages,
        files: op.files || [],
        temperature: op.temperature || 0.7,
        organization_id,
        response_format: op.response_format || null,
        category,
        nameUser,
        startTime,
      };

      pipeline.rpush(queueKey, JSON.stringify(job));
    }

    await pipeline.exec();

    // Ensure worker is running
    const acquired = await redisClient.eval(
      ACQUIRE_WORKER_SCRIPT,
      1,
      lockKey,
      "1",
      String(FM_LOCK_TTL),
    );
    if (acquired === 1) {
      fileManagerWorker(userId).catch((err) => {
        console.error(
          `[FM-WORKER] Unhandled crash for ${userId}: ${err.message}`,
        );
        redisClient.del(lockKey).catch(() => {});
      });
    }

    // Wait for all results concurrently
    const resultPromises = jobIds.map((jobId) =>
      waitForResult(jobId, 180000).catch((err) => ({
        success: false,
        error: err.message,
        jobId,
      })),
    );

    const results = await Promise.all(resultPromises);

    const batchResponse = results.map((result, i) => ({
      index: i,
      success: result.success !== false,
      data: result.success !== false ? result.data : undefined,
      error: result.success === false ? result.error : undefined,
    }));

    const succeeded = batchResponse.filter((r) => r.success).length;
    const failed = batchResponse.filter((r) => !r.success).length;

    console.log(
      `[FM-BATCH] [${requestId}] Done: ${succeeded} ok, ${failed} failed (${Date.now() - startTime}ms)`,
    );

    res.json({
      request_id: requestId,
      total: operations.length,
      succeeded,
      failed,
      results: batchResponse,
    });
  } catch (error) {
    console.error(`[FM-BATCH] [${requestId}] Error: ${error.message}`);
    if (!res.headersSent) {
      res.status(500).json({ error: error.message, request_id: requestId });
    }
  }
});

// ---------- EMBEDDINGS ----------

router.post("/embeddings", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `embed-${Date.now()}`;

  try {
    const { texts, input, organization_id } = req.body;
    const textsToEmbed = texts || input;

    console.log(
      `[EMBED] [${requestId}] Processing ${textsToEmbed?.length || 0} texts`,
    );

    if (!textsToEmbed) {
      return res.status(400).json({ error: "Missing texts or input" });
    }

    const response = await getOpenAIEmbeddings(textsToEmbed);

    const usedModel = API_CONFIGS.openai.embeddingModel;
    const totalTokens = response.usage?.total_tokens || response.usage?.prompt_tokens || 0;
    const costUsd = calculateEmbeddingCost(totalTokens, usedModel);
    const costIdr = costUsd * USD_TO_IDR;
    const responseMs = Date.now() - startTime;

    logCost("/embeddings", requestId, {
      orgId: organization_id,
      queryType: "embedding",
      totalTokens,
      costUsd,
      responseMs,
      extra: `texts=${textsToEmbed?.length || 0} model=${usedModel}`,
    });

    const finalResponse = {
      ...response,
      usage: {
        ...(response.usage || {}),
        total_tokens: totalTokens,
      },
      metadata: {
        request_id: requestId,
        provider: "openai",
        model: usedModel,
        timestamp: new Date().toISOString(),
        credits_used: CREDIT_COSTS.embedding,
        response_time_ms: responseMs,
        cost_usd: costUsd,
        cost_idr: costIdr,
      },
    };

    res.json(finalResponse);
  } catch (error) {
    console.error(`[ERROR] [${requestId}] Embed Route: ${error.message}`);
    res.status(error.response?.status || 500).json({
      error: error.message || "Internal server error",
      request_id: requestId,
    });
  }
});

// ==========================================
// 13. TICKET UPDATE WEBHOOK
// ==========================================

async function updateTicket(ticketId, category, listCategory, resultFromAI) {
  if (!ticketId || !category) return null;
  if (category.toLowerCase() !== "low") return null;

  try {
    const systemPrompt = `You are an expert ticket classification system for customer support. Analyze the AI's response to the customer and determine the most appropriate ticket metadata.

Available categories: ${listCategory.join(", ")}

Your task:
1. Create a concise, professional title (max 60 chars) that captures the core issue
2. Select the BEST matching category from the available list
3. Assess urgency and assign priority: low, medium, high, or urgent
4. Provide a brief, clear reason for your classification

Classification Guidelines:
- Title: Focus on the problem/topic, not the solution (e.g., "RC68 Transaction Timeout Issue" not "User asked about error")
- Category: Match based on technical domain (hardware, software, billing, account, etc.)
- Priority:
  * low: Greetings, introductions, general chitchat, no actual issue or question
  * medium: Standard issues, routine problems, how-to questions
  * high: System errors, repeated issues, multiple users affected
  * urgent: Service down, critical errors, security concerns, revenue impact
- Reason: State what issue was identified and why it needs this classification

If no category matches well, use "general".
If the message is just a greeting or introduction with no real issue, set priority to "low".

Respond ONLY with valid JSON (no markdown, no explanation):
{
  "title": "Short descriptive title",
  "category": "chosen_category",
  "priority": "low|medium|high|urgent",
  "reason": "Brief explanation of classification"
}`;

    const messages = [
      { role: "system", content: systemPrompt },
      {
        role: "user",
        content: `Customer Support Interaction:\n${resultFromAI}\n\nCurrent ticket category: ${category}\nAvailable categories: ${listCategory.join(", ")}\n\nAnalyze the interaction and classify this ticket appropriately.`,
      },
    ];

    const config = API_CONFIGS.openai;
    const response = await axios.post(
      `${config.baseUrl}/chat/completions`,
      {
        model: config.chatModel,
        messages,
        temperature: 0.3,
        max_tokens: 500,
        response_format: { type: "json_object" },
      },
      {
        headers: { Authorization: `Bearer ${getApiKey()}` },
        timeout: 30000,
      },
    );

    const classification = JSON.parse(response.data.choices[0].message.content);

    if (!listCategory.includes(classification.category)) {
      classification.category = "general";
      classification.reason = `Original category not in list. Using general.`;
    }

    const payload = {
      ticket_id: ticketId,
      title: classification.title,
      category: classification.category,
      priority: classification.priority,
      reason: classification.reason,
    };

    const webhookResponse = await axios.put(
      `${process.env.WEBHOOK_BASE_URL}/webhook/ai/ticket/update`,
      payload,
      {
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": process.env.WEBHOOK_SECRET,
        },
        timeout: 10000,
      },
    );

    console.log(
      `[TICKET] ✅ ${ticketId} updated: "${classification.title}" - ${classification.category} (${classification.priority})`,
    );
    return webhookResponse.data;
  } catch (error) {
    console.error(`[TICKET] ❌ Failed to update ticket ${ticketId}`);
    console.error(`[TICKET] Error Message:`, error.message);
    console.error(`[TICKET] Error Response Status:`, error.response?.status);
    console.error(
      `[TICKET] Error Response Data:`,
      JSON.stringify(error.response?.data, null, 2),
    );

    if (error.response?.status === 404) {
      console.error(`[TICKET] Ticket ${ticketId} not found`);
      return null;
    }

    if (error.response?.status === 500) {
      console.error(`[TICKET] Server error on webhook side`);
    }

    return null;
  }
}

// ==========================================
// 14. AUDIO TRANSCRIPTION (Whisper)
// ==========================================

router.post("/audio", authenticate, async (req, res) => {
  const startTime = Date.now();
  const { url } = req.body;

  if (!url) {
    return res.status(400).json({ error: "Missing audio url" });
  }

  try {
    console.log(`🎵 Processing audio transcription`);

    const audioResponse = await axios.get(url, {
      responseType: "arraybuffer",
      timeout: 60000,
    });

    const audioBuffer = Buffer.from(audioResponse.data);
    const FormData = require("form-data");
    const form = new FormData();

    form.append("file", audioBuffer, {
      filename: "audio.mp3",
      contentType: audioResponse.headers["content-type"] || "audio/mpeg",
    });
    form.append("model", "whisper-1");

    const response = await axios.post(
      "https://api.openai.com/v1/audio/transcriptions",
      form,
      {
        headers: {
          Authorization: `Bearer ${getApiKey()}`,
          ...form.getHeaders(),
        },
        timeout: 300000,
      },
    );

    let transcription = response.data.text || "";

    if (!transcription || transcription.trim().length === 0) {
      console.log(
        "⚠️ Audio has no spoken words (Instrumental/Silence). Using placeholder.",
      );
      transcription =
        "[Audio processed. No spoken words detected (Music/Instrumental).]";
    }

    const audioDurationMin = req.body.duration_seconds
      ? req.body.duration_seconds / 60
      : null;
    const audioCostUsd = audioDurationMin ? audioDurationMin * 0.006 : 0;

    logCost("/audio", `audio-${startTime}`, {
      queryType: "audio_transcription",
      costUsd: audioCostUsd,
      responseMs: Date.now() - startTime,
      extra: audioDurationMin
        ? `duration=${req.body.duration_seconds}s`
        : "duration=unknown (pass duration_seconds for accurate cost)",
    });

    res.json({ output: { result: transcription } });
  } catch (error) {
    console.error("Audio transcription error:", error.message);
    res.json({
      output: { result: `[Error processing audio: ${error.message}]` },
    });
  }
});

// ==========================================
// 15. IMAGE OCR (GPT-4o Vision)
// ==========================================

router.post("/image/ocr", authenticate, async (req, res) => {
  const startTime = Date.now();
  const { image_url } = req.body;

  if (!image_url) {
    return res.status(400).json({ error: "Missing image_url" });
  }

  try {
    console.log(`🖼️ Processing image OCR`);

    const response = await axios.post(
      "https://api.openai.com/v1/chat/completions",
      {
        model: "gpt-4o-mini",
        messages: [
          {
            role: "user",
            content: [
              {
                type: "text",
                text: "Extract all text found in this image. If the image contains no readable text (like a photo, icon, or drawing), return exactly: '[NO_TEXT_DETECTED]'. Do not add any other explanation.",
              },
              { type: "image_url", image_url: { url: image_url } },
            ],
          },
        ],
        max_tokens: 300,
      },
      {
        headers: {
          Authorization: `Bearer ${getApiKey()}`,
          "Content-Type": "application/json",
        },
        timeout: 60000,
      },
    );

    let content = response.data?.choices?.[0]?.message?.content || "";

    if (
      !content ||
      content.trim() === "" ||
      content.includes("[NO_TEXT_DETECTED]")
    ) {
      console.log("⚠️ Image has no text. Using placeholder to save file.");
      content = "Visual content only. No text detected in this image.";
    }

    const ocrInputTokens = response.data?.usage?.prompt_tokens || 0;
    const ocrOutputTokens = response.data?.usage?.completion_tokens || 0;
    const ocrCostUsd = calculateTokenCost(ocrInputTokens, ocrOutputTokens);

    logCost("/image/ocr", `ocr-${startTime}`, {
      queryType: "image_analysis",
      inputTokens: ocrInputTokens,
      outputTokens: ocrOutputTokens,
      costUsd: ocrCostUsd,
      responseMs: Date.now() - startTime,
    });

    res.json({ content });
  } catch (error) {
    console.error("Image OCR error:", error.message);
    res.json({ content: `Error processing image: ${error.message}` });
  }
});

router.post("/image/ocr/knowledge-base", authenticate, async (req, res) => {
  const startTime = Date.now();
  const { image_url } = req.body;

  if (!image_url) {
    return res.status(400).json({ error: "Missing image_url" });
  }

  try {
    console.log(`📚 Processing Knowledge Base Image OCR`);

    const response = await axios.post(
      "https://api.openai.com/v1/chat/completions",
      {
        model: "gpt-4o-mini",
        messages: [
          {
            role: "user",
            content: [
              {
                type: "text",
                text: "Analyze this image for a document retrieval system. 1. If it is a chart, graph, or diagram, describe the data, trends, and relationships fully. 2. Extract all significant text. 3. If it is a meaningful photograph, briefly describe what it shows. 4. If it is purely decorative (a generic icon, a colored background, a company logo), return exactly: '[DECORATIVE_NO_VALUE]'. Do not add conversational filler.",
              },
              { type: "image_url", image_url: { url: image_url } },
            ],
          },
        ],
        max_tokens: 500,
      },
      {
        headers: {
          Authorization: `Bearer ${getApiKey()}`,
          "Content-Type": "application/json",
        },
        timeout: 60000,
      },
    );

    let content = response.data?.choices?.[0]?.message?.content || "";

    if (
      !content ||
      content.trim() === "" ||
      content.includes("[DECORATIVE_NO_VALUE]")
    ) {
      console.log(
        "⚠️ Image is decorative or empty. Dropping from Knowledge Base.",
      );
      content = "";
    }

    const ocrInputTokens = response.data?.usage?.prompt_tokens || 0;
    const ocrOutputTokens = response.data?.usage?.completion_tokens || 0;
    const ocrCostUsd = calculateTokenCost(ocrInputTokens, ocrOutputTokens);

    logCost("/image/ocr/knowledge-base", `kb-ocr-${startTime}`, {
      queryType: "knowledge_base_ocr",
      inputTokens: ocrInputTokens,
      outputTokens: ocrOutputTokens,
      costUsd: ocrCostUsd,
      responseMs: Date.now() - startTime,
    });

    res.json({ content });
  } catch (error) {
    console.error("KB Image OCR error:", error.message);
    res.json({ content: "" });
  }
});

// ==========================================
// 16. GRACEFUL SHUTDOWN
// ==========================================

process.on("SIGTERM", async () => {
  console.log("Shutting down...");
  await redisClient.quit();
  await redisBlocking.quit();
  process.exit(0);
});

module.exports = router;
console.log(`🚀 Proxy Router v3.1 loaded [OpenAI Only Mode]`);
