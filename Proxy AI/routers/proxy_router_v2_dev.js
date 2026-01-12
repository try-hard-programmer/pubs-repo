// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with credit tracking
// VERSION: 2.0 - Production Ready with Embedding Cost Tracking
// ==========================================

const express = require("express");
const axios = require("axios");
const router = express.Router();

// ==========================================
// API CONFIGURATIONS
// ==========================================

const API_CONFIGS = {
  openai: {
    name: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    chatModel: "gpt-4o-mini",
    visionModel: "gpt-4o-mini",
    embeddingModel: "text-embedding-3-small",
    maxTokens: 4096,
    supportsVision: true,
  },
  gemini: {
    name: "Google Gemini",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta",
    chatModel: "gemini-2.0-flash-exp",
    visionModel: "gemini-2.0-flash-exp",
    embeddingModel: "text-embedding-004",
    maxTokens: 8192,
    supportsVision: true,
  },
  runpod: {
    name: "RunPod",
    baseUrl: process.env.RUNPOD_BASE_URL || "https://api.runpod.ai/v2",
    endpointId: process.env.RUNPOD_ENDPOINT_ID,
    chatModel: "openai-compatible",
    embeddingModel: "sentence-transformers",
    maxTokens: 4096,
    supportsVision: false,
  },
};

// ==========================================
// CREDIT COSTS
// ==========================================

const CREDIT_COSTS = {
  basic_query: 1,
  file_search: 2,
  document_analysis: 3,
  image_analysis: 4,
  complex_query: 5,
  embedding: 0.5, // Embeddings cost less
};

// ==========================================
// UTILITY FUNCTIONS
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
  if (!process.env.SERVICE_API_KEY) {
    return next();
  }

  const apiKey = req.headers["x-service-key"];
  if (apiKey !== process.env.SERVICE_API_KEY) {
    console.error("[AUTH] ❌ Authentication failed");
    return res.status(401).json({ error: "Unauthorized" });
  }

  next();
};

const detectFiles = (files) => {
  if (!files || !Array.isArray(files) || files.length === 0) {
    return null;
  }

  const fileTypes = files.map((f) => f.type);
  if (fileTypes.includes("image")) return "image";
  if (fileTypes.includes("pdf")) return "pdf";
  if (fileTypes.includes("audio")) return "audio";
  if (fileTypes.includes("video")) return "video";
  return "unknown";
};

async function downloadFileAsBase64(url) {
  try {
    const response = await axios.get(url, {
      responseType: "arraybuffer",
      timeout: 30000,
    });
    const base64 = Buffer.from(response.data).toString("base64");
    const mimeType =
      response.headers["content-type"] || "application/octet-stream";
    return { base64, mimeType };
  } catch (error) {
    console.error(`[DOWNLOAD] ❌ Failed: ${error.message}`);
    throw new Error(`Failed to download file from ${url}`);
  }
}

function detectQueryType(messages, files) {
  const lastMessage = messages[messages.length - 1]?.content || "";
  const hasFiles = files && files.length > 0;
  const messageLength = lastMessage.length;

  if (hasFiles && files[0]?.type === "image") return "image_analysis";
  if (hasFiles && files[0]?.type === "pdf") return "document_analysis";
  if (messageLength < 50) return "basic_query";
  if (
    lastMessage.toLowerCase().includes("search") ||
    lastMessage.toLowerCase().includes("find")
  ) {
    return "file_search";
  }
  if (messageLength > 200) return "complex_query";
  return "basic_query";
}

function calculateTokenCost(provider, inputTokens, outputTokens) {
  const pricing = {
    openai: {
      input: 0.15 / 1_000_000,
      output: 0.6 / 1_000_000,
      embedding: 0.02 / 1_000_000, // text-embedding-3-small
    },
    gemini: {
      input: 0.075 / 1_000_000,
      output: 0.3 / 1_000_000,
      embedding: 0.025 / 1_000_000, // text-embedding-004
    },
    runpod: {
      input: 0,
      output: 0,
      embedding: 0,
    },
  };

  const rates = pricing[provider] || pricing.gemini;
  return inputTokens * rates.input + outputTokens * rates.output;
}

function calculateEmbeddingCost(provider, tokens) {
  const pricing = {
    openai: 0.02 / 1_000_000, // $0.00002 per 1K tokens
    gemini: 0.025 / 1_000_000, // $0.000025 per 1K tokens
    runpod: 0,
  };

  const rate = pricing[provider] || pricing.openai;
  return tokens * rate;
}

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

  const usage = {
    organization_id: orgId,
    query_type: queryType,
    priority: priority,
    credits_used: credits,
    response_time_ms: responseTime,
    provider: provider,
    model: response.model,
    input_tokens: response.usage?.prompt_tokens || 0,
    output_tokens: response.usage?.completion_tokens || 0,
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };

  console.log(
    `[CREDITS] ${queryType}: ${credits} credits, $${tokenCost.toFixed(
      6
    )}, ${responseTime}ms`
  );

  // TODO: Insert into database
  // await db.query('INSERT INTO credit_usage (...) VALUES (...)', [...]);

  return usage;
}

async function logEmbeddingUsage(orgId, response, provider, startTime) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS.embedding;
  const tokens =
    response.usage?.total_tokens || response.usage?.prompt_tokens || 0;

  const tokenCost = calculateEmbeddingCost(provider, tokens);

  const usage = {
    organization_id: orgId,
    query_type: "embedding",
    priority: null,
    credits_used: credits,
    response_time_ms: responseTime,
    provider: provider,
    model: response.model || API_CONFIGS[provider].embeddingModel,
    input_tokens: tokens,
    output_tokens: 0,
    cost_usd: tokenCost,
    status: "completed",
    created_at: new Date(),
  };

  console.log(
    `[CREDITS] embedding: ${credits} credits, ${tokens} tokens, $${tokenCost.toFixed(
      6
    )}, ${responseTime}ms`
  );

  // TODO: Insert into database
  // await db.query('INSERT INTO credit_usage (...) VALUES (...)', [...]);

  return usage;
}

// ==========================================
// PROVIDER HANDLERS - CHAT
// ==========================================

async function handleOpenAI(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.openai;
  const hasFiles = files && files.length > 0;
  const model = hasFiles ? config.visionModel : config.chatModel;

  console.log(`[OPENAI] Model: ${model}`);

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
          image_url: {
            url: file.url || `data:image/jpeg;base64,${file.data}`,
          },
        });
      }
    }

    processedMessages[lastUserIndex] = { ...lastMessage, content: content };
  }

  const response = await axios.post(
    `${config.baseUrl}/chat/completions`,
    {
      model: model,
      messages: processedMessages,
      temperature: temperature,
      max_tokens: config.maxTokens,
    },
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getApiKey("openai")}`,
      },
      timeout: parseInt(process.env.REQUEST_TIMEOUT) || 180000,
    }
  );

  console.log(`[OPENAI] ✅ ${response.data.usage.total_tokens} tokens`);
  return response.data;
}

async function handleGemini(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.gemini;
  const hasFiles = files && files.length > 0;
  const model = hasFiles ? config.visionModel : config.chatModel;
  const apiKey = getApiKey("gemini");

  console.log(`[GEMINI] Model: ${model}`);

  const contents = [];

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const parts = [];

    if (msg.content) {
      parts.push({ text: msg.content });
    }

    if (msg.role === "user" && hasFiles && i === messages.length - 1) {
      for (const file of files) {
        if (file.type === "image") {
          let base64Data;

          if (file.url) {
            const { base64 } = await downloadFileAsBase64(file.url);
            base64Data = base64;
          } else if (file.data) {
            base64Data = file.data;
          }

          if (base64Data) {
            parts.push({
              inline_data: {
                mime_type: "image/jpeg",
                data: base64Data,
              },
            });
          }
        }
      }
    }

    contents.push({
      role: msg.role === "assistant" ? "model" : "user",
      parts: parts,
    });
  }

  const response = await axios.post(
    `${config.baseUrl}/models/${model}:generateContent?key=${apiKey}`,
    {
      contents: contents,
      generationConfig: {
        temperature: temperature,
        maxOutputTokens: config.maxTokens,
      },
    },
    {
      headers: { "Content-Type": "application/json" },
      timeout: parseInt(process.env.REQUEST_TIMEOUT) || 180000,
    }
  );

  console.log(
    `[GEMINI] ✅ ${response.data.usageMetadata?.totalTokenCount || 0} tokens`
  );

  return {
    id: `gemini-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: model,
    choices: [
      {
        index: 0,
        message: {
          role: "assistant",
          content: response.data.candidates[0].content.parts[0].text,
        },
        finish_reason: "stop",
      },
    ],
    usage: {
      prompt_tokens: response.data.usageMetadata?.promptTokenCount || 0,
      completion_tokens: response.data.usageMetadata?.candidatesTokenCount || 0,
      total_tokens: response.data.usageMetadata?.totalTokenCount || 0,
    },
  };
}

async function handleRunPod(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.runpod;
  const endpointId = config.endpointId;

  if (!endpointId) {
    throw new Error("RUNPOD_ENDPOINT_ID not configured");
  }

  console.log(`[RUNPOD] Endpoint: ${endpointId}`);

  const response = await axios.post(
    `${config.baseUrl}/${endpointId}/runsync`,
    {
      input: {
        messages: messages,
        temperature: temperature,
        max_tokens: config.maxTokens,
      },
    },
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getApiKey("runpod")}`,
      },
      timeout: parseInt(process.env.REQUEST_TIMEOUT) || 180000,
    }
  );

  console.log(`[RUNPOD] ✅ Response received`);
  return response.data.output || response.data;
}

// ==========================================
// ROUTING - CONTROLLED BY ENV
// ==========================================

function getProvider(requestedProvider = null) {
  // Allow per-request override if enabled
  if (requestedProvider && process.env.ALLOW_PROVIDER_OVERRIDE === "true") {
    if (API_CONFIGS[requestedProvider]) {
      console.log(`[ROUTER] Override: ${requestedProvider}`);
      return requestedProvider;
    }
  }

  // Use PRIMARY_LLM_PROVIDER from .env
  const primary = process.env.PRIMARY_LLM_PROVIDER || "openai";
  console.log(`[ROUTER] Primary: ${primary}`);
  return primary;
}

async function routeRequest(provider, messages, files = [], temperature = 0.7) {
  const fileType = detectFiles(files);

  console.log(`[ROUTE] Provider: ${provider}, Files: ${fileType || "none"}`);

  if (fileType && !API_CONFIGS[provider].supportsVision) {
    console.warn(`[ROUTE] ⚠️ ${provider} doesn't support files, ignoring`);
    files = [];
  }

  switch (provider) {
    case "openai":
      return await handleOpenAI(messages, files, temperature);
    case "gemini":
      return await handleGemini(messages, files, temperature);
    case "runpod":
      return await handleRunPod(messages, files, temperature);
    default:
      throw new Error(`Unknown provider: ${provider}`);
  }
}

async function routeWithFallback(
  provider,
  messages,
  files = [],
  temperature = 0.7
) {
  if (process.env.ENABLE_FALLBACK !== "true") {
    return await routeRequest(provider, messages, files, temperature);
  }

  try {
    return await routeRequest(provider, messages, files, temperature);
  } catch (error) {
    console.error(`[FALLBACK] ❌ ${provider} failed: ${error.message}`);

    const fallbackOrder = ["openai", "gemini", "runpod"].filter(
      (p) => p !== provider && getApiKey(p)
    );

    for (const fallback of fallbackOrder) {
      try {
        console.log(`[FALLBACK] Trying: ${fallback}`);
        return await routeRequest(fallback, messages, files, temperature);
      } catch (fallbackError) {
        console.error(
          `[FALLBACK] ❌ ${fallback} failed: ${fallbackError.message}`
        );
        continue;
      }
    }

    throw new Error("All providers failed");
  }
}

// ==========================================
// EMBEDDINGS - CONTROLLED BY ENV
// ==========================================

async function getOpenAIEmbeddings(texts) {
  const config = API_CONFIGS.openai;
  console.log(`[EMBED] OpenAI...`);

  const response = await axios.post(
    `${config.baseUrl}/embeddings`,
    {
      model: config.embeddingModel,
      input: texts,
    },
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getApiKey("openai")}`,
      },
      timeout: parseInt(process.env.EMBEDDINGS_TIMEOUT) || 60000,
    }
  );

  // Calculate cost
  const tokens = response.data.usage?.total_tokens || 0;
  const cost = calculateEmbeddingCost("openai", tokens);

  console.log(
    `[EMBED] ✅ ${
      response.data.data.length
    } embeddings, ${tokens} tokens, $${cost.toFixed(6)}`
  );

  return response.data;
}

async function getGeminiEmbeddings(texts) {
  const config = API_CONFIGS.gemini;
  const apiKey = getApiKey("gemini");
  const requests = Array.isArray(texts) ? texts : [texts];

  console.log(`[EMBED] Gemini ${requests.length} texts...`);

  const embeddings = [];
  let totalTokens = 0;

  for (const text of requests) {
    const response = await axios.post(
      `${config.baseUrl}/models/${config.embeddingModel}:embedContent?key=${apiKey}`,
      {
        content: {
          parts: [{ text: text }],
        },
      },
      {
        headers: { "Content-Type": "application/json" },
        timeout: parseInt(process.env.EMBEDDINGS_TIMEOUT) || 60000,
      }
    );

    embeddings.push({
      object: "embedding",
      embedding: response.data.embedding.values,
      index: embeddings.length,
    });

    // Estimate tokens (rough: ~1 token per 4 chars)
    totalTokens += Math.ceil(text.length / 4);
  }

  // Calculate cost
  const cost = calculateEmbeddingCost("gemini", totalTokens);

  console.log(
    `[EMBED] ✅ ${
      embeddings.length
    } embeddings, ~${totalTokens} tokens (est), $${cost.toFixed(6)}`
  );

  return {
    object: "list",
    data: embeddings,
    model: config.embeddingModel,
    usage: {
      prompt_tokens: totalTokens,
      total_tokens: totalTokens,
    },
  };
}

async function getRunPodEmbeddings(texts) {
  const config = API_CONFIGS.runpod;
  const endpointId = config.endpointId;

  if (!endpointId) {
    throw new Error("RUNPOD_ENDPOINT_ID not configured");
  }

  console.log(`[EMBED] RunPod...`);

  const response = await axios.post(
    `${config.baseUrl}/${endpointId}/runsync`,
    {
      input: {
        texts: texts,
        model: config.embeddingModel,
      },
    },
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getApiKey("runpod")}`,
      },
      timeout: parseInt(process.env.EMBEDDINGS_TIMEOUT) || 60000,
    }
  );

  console.log(`[EMBED] ✅ RunPod done`);

  // Estimate tokens if not provided
  const texts_array = Array.isArray(texts) ? texts : [texts];
  const estimatedTokens = texts_array.reduce(
    (sum, text) => sum + Math.ceil(text.length / 4),
    0
  );

  return {
    ...(response.data.output || response.data),
    usage: {
      prompt_tokens: estimatedTokens,
      total_tokens: estimatedTokens,
    },
  };
}

async function routeEmbeddings(texts, provider = null) {
  // Use EMBEDDING_PROVIDER from .env
  const embeddingProvider =
    provider || process.env.EMBEDDING_PROVIDER || "openai";

  console.log(`[EMBED] Provider: ${embeddingProvider}`);

  switch (embeddingProvider) {
    case "openai":
      return await getOpenAIEmbeddings(texts);
    case "gemini":
      return await getGeminiEmbeddings(texts);
    case "runpod":
      return await getRunPodEmbeddings(texts);
    default:
      return await getOpenAIEmbeddings(texts);
  }
}

// ==========================================
// ROUTES
// ==========================================

router.get("/test", (req, res) => {
  console.log("✅ Proxy v2 test");
  res.json({
    message: "Proxy Router v2 - Multi-Agent System",
    timestamp: new Date(),
    config: {
      primary_llm: process.env.PRIMARY_LLM_PROVIDER || "openai",
      embedding: process.env.EMBEDDING_PROVIDER || "openai",
      fallback_enabled: process.env.ENABLE_FALLBACK === "true",
    },
  });
});

router.post("/chat", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `req-${Date.now()}`;
  const fs = require("fs").promises;
  const path = require("path");
  const crypto = require("crypto");

  // ==========================================
  // 🎚️ MOCK TOGGLE FOR CHAT
  // ==========================================
  const USE_MOCK = false;

  console.log(
    `\n[v2] 🚀 ${requestId} ${USE_MOCK ? "(MOCK MODE)" : "(REAL API)"}`
  );

  try {
    console.log("📨 RAW REQUEST BODY:", JSON.stringify(req.body, null, 2));
    const {
      messages,
      files = [],
      temperature = 0.7,
      provider: requestedProvider = null,
      nameUser,
      organization_id,
      category,
    } = req.body;

    console.log(`[v2] User: ${nameUser || "Anonymous"}`);
    console.log(`[v2] Org: ${organization_id || "N/A"}`);
    console.log(`[v2] Messages: ${messages?.length || 0}`);

    if (!messages || !Array.isArray(messages)) {
      return res.status(400).json({ error: "Missing messages array" });
    }

    // ✅ CREATE CACHE KEY (hash of messages + files)
    const cacheKey = crypto
      .createHash("md5")
      .update(JSON.stringify({ messages, files, temperature }))
      .digest("hex");
    const cacheDir = path.join(__dirname, "../cache/chat");
    const cacheFile = path.join(cacheDir, `${cacheKey}.json`);

    let response;
    let fromCache = false;
    let usedCacheFile = null;

    // ✅ TRY TO LOAD FROM CACHE IF MOCK ENABLED
    if (USE_MOCK) {
      try {
        // First try exact match
        const cached = await fs.readFile(cacheFile, "utf8");
        response = JSON.parse(cached);
        fromCache = true;
        usedCacheFile = cacheKey.substring(0, 8);
        console.log(`📦 EXACT MATCH CACHE: ${usedCacheFile}...`);
      } catch (err) {
        // If no exact match, use newest cache file
        console.log(`⚠️ No exact match, using newest cache...`);
        try {
          const cacheFiles = await fs.readdir(cacheDir);
          if (cacheFiles.length > 0) {
            // Get file stats with modification time
            const fileStats = await Promise.all(
              cacheFiles
                .filter((f) => f.endsWith(".json"))
                .map(async (f) => ({
                  name: f,
                  time: (await fs.stat(path.join(cacheDir, f))).mtime,
                  path: path.join(cacheDir, f),
                }))
            );

            // Sort by newest first
            const newest = fileStats.sort((a, b) => b.time - a.time)[0];

            const cached = await fs.readFile(newest.path, "utf8");
            response = JSON.parse(cached);
            fromCache = true;
            usedCacheFile = newest.name.replace(".json", "").substring(0, 8);
            console.log(
              `📦 LOADED NEWEST CACHE: ${usedCacheFile}... (${newest.name})`
            );
          } else {
            console.log(`⚠️ No cache files found, falling back to real API...`);
          }
        } catch (cacheError) {
          console.log(`⚠️ Cache read error: ${cacheError.message}`);
        }
      }
    }

    // ✅ CALL REAL API IF NOT USING MOCK OR CACHE MISS
    if (!fromCache) {
      const provider = getProvider(requestedProvider);
      console.log(`🌐 CALLING REAL API: ${provider}`);

      response = await routeWithFallback(
        provider,
        messages,
        files,
        temperature
      );

      // ✅ SAVE TO CACHE FOR FUTURE USE
      try {
        await fs.mkdir(cacheDir, { recursive: true });
        await fs.writeFile(cacheFile, JSON.stringify(response, null, 2));
        console.log(`💾 CACHED: ${cacheKey.substring(0, 8)}...`);
      } catch (cacheError) {
        console.warn(`⚠️ Cache save failed: ${cacheError.message}`);
      }
    }

    const queryType = detectQueryType(messages, files);
    const creditUsage = await logCreditUsage(
      organization_id,
      queryType,
      response,
      fromCache ? "cached" : getProvider(requestedProvider),
      startTime,
      category
    );

    const finalResponse = {
      ...response,
      metadata: {
        request_id: requestId,
        provider: fromCache ? "cached" : getProvider(requestedProvider),
        nameUser: nameUser || "Anonymous",
        hasFiles: files.length > 0,
        timestamp: new Date().toISOString(),
        query_type: queryType,
        priority: category || null,
        credits_used: creditUsage.credits_used,
        response_time_ms: creditUsage.response_time_ms,
        cost_usd: fromCache ? 0 : creditUsage.cost_usd,
        from_cache: fromCache,
        cache_key: usedCacheFile || cacheKey.substring(0, 8),
      },
    };

    const totalTime = Date.now() - startTime;
    console.log(
      `[v2] ✅ ${totalTime}ms ${
        fromCache
          ? "📦 MOCK"
          : "💸 REAL ($" + creditUsage.cost_usd.toFixed(6) + ")"
      }\n`
    );

    res.json(finalResponse);
  } catch (error) {
    console.error(`[v2] ❌ ${error.message}\n`);
    res.status(error.response?.status || 500).json({
      error: error.message || "Internal server error",
      request_id: requestId,
    });
  }
});

router.post("/embeddings", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `embed-${Date.now()}`;
  const fs = require("fs").promises;
  const path = require("path");
  const crypto = require("crypto");

  // ==========================================
  // 🎚️ MOCK TOGGLE FOR EMBEDDINGS
  // ==========================================
  const USE_MOCK = false;

  console.log(
    `\n[v2] 🧮 ${requestId} ${USE_MOCK ? "(MOCK MODE)" : "(REAL API)"}`
  );

  try {
    const { texts, input, provider, organization_id } = req.body;
    const textsToEmbed = texts || input;

    console.log("📥 INPUT DATA:", {
      texts: textsToEmbed ? `${textsToEmbed.length} items` : "null",
      provider: provider || "default",
      organization_id: organization_id || "N/A",
    });

    if (!textsToEmbed) {
      return res.status(400).json({ error: "Missing texts or input" });
    }

    const embeddingProvider =
      provider || process.env.EMBEDDING_PROVIDER || "openai";

    // Create cache key (hash of texts)
    const cacheKey = crypto
      .createHash("md5")
      .update(JSON.stringify(textsToEmbed))
      .digest("hex");
    const cacheDir = path.join(__dirname, "../cache/embeddings");
    const cacheFile = path.join(cacheDir, `${cacheKey}.json`);

    let response;
    let fromCache = false;
    let usedCacheFile = null;

    // ✅ TRY TO LOAD FROM CACHE IF MOCK ENABLED
    if (USE_MOCK) {
      try {
        // First try exact match
        const cached = await fs.readFile(cacheFile, "utf8");
        response = JSON.parse(cached);
        fromCache = true;
        usedCacheFile = cacheKey.substring(0, 8);
        console.log(`📦 EXACT MATCH CACHE: ${usedCacheFile}...`);
      } catch (err) {
        // If no exact match, use newest cache file
        console.log(`⚠️ No exact match, using newest cache...`);
        try {
          const cacheFiles = await fs.readdir(cacheDir);
          if (cacheFiles.length > 0) {
            // Get file stats with modification time
            const fileStats = await Promise.all(
              cacheFiles
                .filter((f) => f.endsWith(".json"))
                .map(async (f) => ({
                  name: f,
                  time: (await fs.stat(path.join(cacheDir, f))).mtime,
                  path: path.join(cacheDir, f),
                }))
            );

            // Sort by newest first
            const newest = fileStats.sort((a, b) => b.time - a.time)[0];

            const cached = await fs.readFile(newest.path, "utf8");
            response = JSON.parse(cached);
            fromCache = true;
            usedCacheFile = newest.name.replace(".json", "").substring(0, 8);
            console.log(
              `📦 LOADED NEWEST CACHE: ${usedCacheFile}... (${newest.name})`
            );
          } else {
            console.log(`⚠️ No cache files found, falling back to real API...`);
          }
        } catch (cacheError) {
          console.log(`⚠️ Cache read error: ${cacheError.message}`);
        }
      }
    }

    // ✅ CALL REAL API IF NOT USING MOCK OR CACHE MISS
    if (!fromCache) {
      console.log(`🌐 CALLING REAL API: ${embeddingProvider}`);
      response = await routeEmbeddings(textsToEmbed, embeddingProvider);

      // 🔍 LOG RAW API RESPONSE
      console.log("📤 RAW API RESPONSE:", {
        hasData: !!response.data,
        dataLength: response.data?.length || 0,
        hasUsage: !!response.usage,
        usage: response.usage,
        model: response.model,
        object: response.object,
        keys: Object.keys(response),
      });

      // Save to cache for future use
      try {
        await fs.mkdir(cacheDir, { recursive: true });
        await fs.writeFile(cacheFile, JSON.stringify(response, null, 2));
        console.log(`💾 CACHED: ${cacheKey.substring(0, 8)}...`);
      } catch (cacheError) {
        console.warn(`⚠️ Cache save failed: ${cacheError.message}`);
      }
    } else {
      // 🔍 LOG CACHED RESPONSE
      console.log("📤 CACHED RESPONSE:", {
        hasData: !!response.data,
        dataLength: response.data?.length || 0,
        hasUsage: !!response.usage,
        usage: response.usage,
        model: response.model,
        object: response.object,
        keys: Object.keys(response),
      });
    }

    // Log embedding usage
    const embeddingUsage = await logEmbeddingUsage(
      organization_id,
      response,
      embeddingProvider,
      startTime
    );

    // Add metadata to response
    const finalResponse = {
      ...response,
      metadata: {
        request_id: requestId,
        provider: embeddingProvider,
        timestamp: new Date().toISOString(),
        credits_used: embeddingUsage.credits_used,
        response_time_ms: embeddingUsage.response_time_ms,
        cost_usd: fromCache ? 0 : embeddingUsage.cost_usd,
        from_cache: fromCache,
        cache_key: usedCacheFile || cacheKey.substring(0, 8),
      },
    };

    // 🔍 LOG FINAL RESPONSE STRUCTURE
    console.log("📦 FINAL RESPONSE STRUCTURE:", {
      hasData: !!finalResponse.data,
      dataLength: finalResponse.data?.length || 0,
      firstEmbeddingDimension: finalResponse.data?.[0]?.embedding?.length || 0,
      hasMetadata: !!finalResponse.metadata,
      hasUsage: !!finalResponse.usage,
      keys: Object.keys(finalResponse),
      metadataKeys: Object.keys(finalResponse.metadata || {}),
    });

    // 🔍 LOG FIRST EMBEDDING SAMPLE (for debugging)
    if (finalResponse.data && finalResponse.data[0]) {
      console.log("🔬 FIRST EMBEDDING SAMPLE:", {
        object: finalResponse.data[0].object,
        index: finalResponse.data[0].index,
        embeddingLength: finalResponse.data[0].embedding?.length,
        embeddingFirst5: finalResponse.data[0].embedding?.slice(0, 5),
        keys: Object.keys(finalResponse.data[0]),
      });
    }

    console.log(
      `[v2] ✅ Done ${
        fromCache
          ? "📦 MOCK"
          : "💸 REAL ($" + embeddingUsage.cost_usd.toFixed(6) + ")"
      }\n`
    );

    // 🔍 LOG WHAT WE'RE SENDING BACK
    console.log("🚀 SENDING RESPONSE:", {
      statusCode: 200,
      bodySize: JSON.stringify(finalResponse).length,
      hasData: !!finalResponse.data,
      hasMetadata: !!finalResponse.metadata,
    });

    res.json(finalResponse);
  } catch (error) {
    console.error(`[v2] ❌ ERROR DETAILS:`, {
      message: error.message,
      stack: error.stack?.split("\n").slice(0, 3),
      response: error.response?.data,
      status: error.response?.status,
    });

    res.status(error.response?.status || 500).json({
      error: error.message || "Internal server error",
      request_id: requestId,
    });
  }
});

module.exports = router;

console.log("🔧 Proxy Router v2 loaded");
