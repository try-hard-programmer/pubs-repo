// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with credit tracking
// VERSION: 2.0 - Production
// ==========================================

const express = require("express");
const axios = require("axios");
const router = express.Router();

// ==========================================
// ENVIRONMENT
// ==========================================

const ENV = process.env.NODE_ENV || "production";
const IS_DEV = ENV === "development";

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
  embedding: 0.5,
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
    console.error(`[AUTH] Unauthorized access attempt`);
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
    console.error(`[DOWNLOAD] Failed to download file: ${error.message}`);
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
    },
    gemini: {
      input: 0.075 / 1_000_000,
      output: 0.3 / 1_000_000,
    },
    runpod: {
      input: 0,
      output: 0,
    },
  };

  const rates = pricing[provider] || pricing.gemini;
  return inputTokens * rates.input + outputTokens * rates.output;
}

function calculateEmbeddingCost(provider, tokens) {
  const pricing = {
    openai: 0.02 / 1_000_000,
    gemini: 0.025 / 1_000_000,
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

  // TODO: Insert into database
  // await db.query('INSERT INTO credit_usage (...) VALUES (...)', [...]);

  return usage;
}

// ==========================================
// RATE LIMITING
// ==========================================

const geminiRateLimiter = {
  lastCall: 0,
  minInterval: IS_DEV ? 1000 : 500,
};

// ==========================================
// PROVIDER HANDLERS - CHAT
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

  return response.data;
}

async function handleGemini(messages, files = [], temperature = 0.7) {
  const config = API_CONFIGS.gemini;
  const hasFiles = files && files.length > 0;
  const model = hasFiles ? config.visionModel : config.chatModel;
  const apiKey = getApiKey("gemini");

  // Rate limiting
  if (IS_DEV) {
    const now = Date.now();
    const timeSinceLastCall = now - geminiRateLimiter.lastCall;

    if (timeSinceLastCall < geminiRateLimiter.minInterval) {
      const waitTime = geminiRateLimiter.minInterval - timeSinceLastCall;
      await new Promise((resolve) => setTimeout(resolve, waitTime));
    }

    geminiRateLimiter.lastCall = Date.now();
  }

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

  return response.data.output || response.data;
}

// ==========================================
// ROUTING
// ==========================================

function getProvider(requestedProvider = null) {
  if (requestedProvider && process.env.ALLOW_PROVIDER_OVERRIDE === "true") {
    if (API_CONFIGS[requestedProvider]) {
      return requestedProvider;
    }
  }

  return process.env.PRIMARY_LLM_PROVIDER || "openai";
}

async function routeRequest(provider, messages, files = [], temperature = 0.7) {
  const fileType = detectFiles(files);

  if (fileType && !API_CONFIGS[provider].supportsVision) {
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
    console.error(`[FALLBACK] ${provider} failed, trying alternatives...`);

    const fallbackOrder = ["openai", "gemini", "runpod"].filter(
      (p) => p !== provider && getApiKey(p)
    );

    for (const fallback of fallbackOrder) {
      try {
        return await routeRequest(fallback, messages, files, temperature);
      } catch (fallbackError) {
        continue;
      }
    }

    throw new Error("All providers failed");
  }
}

// ==========================================
// EMBEDDINGS
// ==========================================

async function getOpenAIEmbeddings(texts) {
  const config = API_CONFIGS.openai;

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

    totalTokens += Math.ceil(text.length / 4);
  }

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
  const embeddingProvider =
    provider || process.env.EMBEDDING_PROVIDER || "openai";

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
  res.json({
    message: "Proxy Router v2 - Production",
    timestamp: new Date(),
    environment: ENV,
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

  try {
    const {
      messages,
      files = [],
      temperature = 0.7,
      provider: requestedProvider = null,
      nameUser,
      organization_id,
      category,
    } = req.body;

    if (!messages || !Array.isArray(messages)) {
      return res.status(400).json({ error: "Missing messages array" });
    }

    const provider = getProvider(requestedProvider);
    const response = await routeWithFallback(
      provider,
      messages,
      files,
      temperature
    );

    const queryType = detectQueryType(messages, files);
    const creditUsage = await logCreditUsage(
      organization_id,
      queryType,
      response,
      provider,
      startTime,
      category
    );

    const finalResponse = {
      ...response,
      metadata: {
        request_id: requestId,
        provider: provider,
        nameUser: nameUser || "Anonymous",
        hasFiles: files.length > 0,
        timestamp: new Date().toISOString(),
        query_type: queryType,
        priority: category || null,
        credits_used: creditUsage.credits_used,
        response_time_ms: creditUsage.response_time_ms,
        cost_usd: creditUsage.cost_usd,
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

module.exports = router;

console.log(`🚀 Proxy Router v2 loaded [${ENV}]`);
