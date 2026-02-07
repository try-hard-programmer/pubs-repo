// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with Redis Queue (Dual Connection)
// VERSION: 2.5 - Fixed Gemini Multimodal + No RunPod
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

// 1. General Client (Non-blocking: RPUSH, GET, SET)
const redisClient = new Redis(redisConfig);

// 2. Worker Client (Blocking: BLPOP only)
const redisBlocking = new Redis(redisConfig);

redisClient.on("error", (err) => console.error("Redis (Main) Error:", err));
redisBlocking.on("error", (err) => console.error("Redis (Block) Error:", err));

redisClient.on("connect", () => console.log("✓ Redis (Main) Connected"));
redisBlocking.on("connect", () => console.log("✓ Redis (Block) Connected"));

const userWorkers = {};

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
    console.warn(`[WARN] Failed to download image: ${url}`);
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

  // Check for inline images in messages (New Python Payload)
  const hasInlineImage =
    Array.isArray(lastMessage) &&
    lastMessage.some((m) => m.type === "image_url");

  const hasFiles = files && files.length > 0;

  if (hasFiles && files[0]?.type === "image") return "image_analysis";
  if (hasInlineImage) return "image_analysis";
  if (hasFiles && files[0]?.type === "pdf") return "document_analysis";

  // Text length check
  const textLen = Array.isArray(lastMessage)
    ? lastMessage.find((m) => m.type === "text")?.text?.length || 0
    : lastMessage.length;

  if (textLen < 50) return "basic_query";
  if (textLen > 200) return "complex_query";
  return "basic_query";
}

function calculateTokenCost(provider, input, output) {
  const pricing = {
    openai: { input: 0.15 / 1e6, output: 0.6 / 1e6 },
    gemini: { input: 0.075 / 1e6, output: 0.3 / 1e6 },
  };
  const rates = pricing[provider] || pricing.gemini;
  return input * rates.input + output * rates.output;
}

function calculateEmbeddingCost(provider, tokens) {
  const pricing = {
    openai: 0.02 / 1e6,
    gemini: 0.025 / 1e6,
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
  priority = null,
) {
  const responseTime = Date.now() - startTime;
  const credits = CREDIT_COSTS[queryType];
  const tokenCost = calculateTokenCost(
    provider,
    response.usage?.prompt_tokens || 0,
    response.usage?.completion_tokens || 0,
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

async function handleOpenAI(
  messages,
  files = [],
  temperature = 0.7,
  tools = null,
  tool_choice = null,
) {
  const config = API_CONFIGS.openai;
  // Use Vision Model if files present OR inline images detected
  const hasInlineImages = messages.some(
    (m) =>
      Array.isArray(m.content) && m.content.some((p) => p.type === "image_url"),
  );
  const hasFiles = files && files.length > 0;

  const model =
    hasFiles || hasInlineImages ? config.visionModel : config.chatModel;

  let processedMessages = messages;

  // Handle Legacy "files" array (if used)
  if (hasFiles) {
    const lastUserIndex = messages
      .map((m, i) => ({ role: m.role, index: i }))
      .reverse()
      .find((m) => m.role === "user").index;

    processedMessages = [...messages];
    const lastMessage = processedMessages[lastUserIndex];

    // Normalize content to array
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
          image_url: { url: file.url || `data:image/jpeg;base64,${file.data}` },
        });
      }
    }
    processedMessages[lastUserIndex] = { ...lastMessage, content: content };
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
      headers: { Authorization: `Bearer ${getApiKey("openai")}` },
      timeout: 180000,
    },
  );
  return response.data;
}

async function handleGemini(
  messages,
  files = [],
  temperature = 0.7,
  tools = null,
) {
  const config = API_CONFIGS.gemini;
  const apiKey = getApiKey("gemini");

  // Auto-detect if we need vision based on content
  const hasInlineImages = messages.some(
    (m) =>
      Array.isArray(m.content) && m.content.some((p) => p.type === "image_url"),
  );
  const hasFiles = files && files.length > 0;

  // Gemini 2.0 Flash supports everything, but good to be explicit
  const model =
    hasFiles || hasInlineImages ? config.visionModel : config.chatModel;

  const contents = [];

  // Use for...of loop to handle AWAIT for image downloads
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const parts = [];

    // ✅ Handle tool results (OpenAI format → Gemini functionResponse)
    if (msg.role === "tool") {
      contents.push({
        role: "user",
        parts: [
          {
            functionResponse: {
              name: msg.name,
              response: { content: msg.content },
            },
          },
        ],
      });
      continue;
    }

    // ✅ Handle assistant messages with tool_calls (→ Gemini functionCall)
    if (msg.role === "assistant" && msg.tool_calls) {
      const fnParts = msg.tool_calls.map((tc) => ({
        functionCall: {
          name: tc.function.name,
          args: JSON.parse(tc.function.arguments),
        },
      }));
      contents.push({ role: "model", parts: fnParts });
      continue;
    }

    // [FIX] HANDLE MULTIMODAL ARRAYS (The cause of your 400 Error)
    if (Array.isArray(msg.content)) {
      for (const part of msg.content) {
        if (part.type === "text") {
          parts.push({ text: part.text });
        } else if (part.type === "image_url") {
          // Gemini NEEDS Base64. It cannot take "url": "..." directly.
          const imgUrl = part.image_url?.url;
          if (imgUrl) {
            try {
              const { base64, mimeType } = await downloadFileAsBase64(imgUrl);
              parts.push({
                inline_data: {
                  mime_type: mimeType || "image/jpeg",
                  data: base64,
                },
              });
            } catch (e) {
              console.error(`Skipping inline image: ${e.message}`);
            }
          }
        }
      }
    }
    // Handle Simple String
    else if (msg.content) {
      parts.push({ text: msg.content });
    }

    // Handle Legacy "files" array
    if (msg.role === "user" && hasFiles && i === messages.length - 1) {
      for (const file of files) {
        if (file.type === "image") {
          let base64Data;
          if (file.url) {
            const { base64 } = await downloadFileAsBase64(file.url);
            base64Data = base64;
          } else if (file.data) base64Data = file.data;

          if (base64Data) {
            parts.push({
              inline_data: { mime_type: "image/jpeg", data: base64Data },
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

  const requestBody = {
    contents,
    generationConfig: { temperature, maxOutputTokens: config.maxTokens },
  };

  if (tools && tools.length > 0) {
    requestBody.tools = [
      {
        functionDeclarations: tools.map((t) => ({
          name: t.function.name,
          description: t.function.description,
          parameters: t.function.parameters,
        })),
      },
    ];
  }

  try {
    const response = await axios.post(
      `${config.baseUrl}/models/${model}:generateContent?key=${apiKey}`,
      requestBody,
      { headers: { "Content-Type": "application/json" }, timeout: 180000 },
    );

    const candidate = response.data.candidates?.[0];
    const parts = candidate?.content?.parts || [];

    // ✅ Check if Gemini returned a function call
    const functionCallParts = parts.filter((p) => p.functionCall);

    if (functionCallParts.length > 0) {
      // Convert Gemini function calls → OpenAI tool_calls format
      const tool_calls = functionCallParts.map((p, i) => ({
        id: `call_${Date.now()}_${i}`,
        type: "function",
        function: {
          name: p.functionCall.name,
          arguments: JSON.stringify(p.functionCall.args || {}),
        },
      }));

      return {
        choices: [
          {
            message: {
              role: "assistant",
              content: null,
              tool_calls: tool_calls,
            },
          },
        ],
        usage: { prompt_tokens: 0, completion_tokens: 0 },
      };
    }

    // Normal text response
    const text =
      parts[0]?.text || "⚠️ I cannot answer this due to safety filters.";

    return {
      choices: [{ message: { role: "assistant", content: text } }],
      usage: { prompt_tokens: 0, completion_tokens: 0 },
    };
  } catch (error) {
    console.error(
      "Gemini API Error:",
      JSON.stringify(error.response?.data || error.message, null, 2),
    );
    throw error;
  }
}

async function routeWithFallback(
  provider,
  messages,
  files = [],
  temperature = 0.7,
  tools = null,
  tool_choice = null,
) {
  try {
    if (provider === "gemini")
      return await handleGemini(messages, files, temperature, tools);
    return await handleOpenAI(messages, files, temperature, tools, tool_choice);
  } catch (error) {
    const fallback = provider === "openai" ? "gemini" : "openai";
    if (getApiKey(fallback)) {
      if (fallback === "openai")
        return await handleOpenAI(
          messages,
          files,
          temperature,
          tools,
          tool_choice,
        );
      return await handleGemini(messages, files, temperature, tools);
    }
    throw new Error("All providers failed");
  }
}

// ==========================================
// 6. EMBEDDING FUNCTIONS
// ==========================================

async function getOpenAIEmbeddings(texts) {
  const config = API_CONFIGS.openai;
  const response = await axios.post(
    `${config.baseUrl}/embeddings`,
    { model: config.embeddingModel, input: texts },
    {
      headers: { Authorization: `Bearer ${getApiKey("openai")}` },
      timeout: 60000,
    },
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
      { headers: { "Content-Type": "application/json" }, timeout: 60000 },
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

async function routeEmbeddings(texts, provider = null) {
  const p = provider || process.env.EMBEDDING_PROVIDER || "openai";
  if (p === "gemini") return await getGeminiEmbeddings(texts);
  return await getOpenAIEmbeddings(texts);
}

// ==========================================
// 7. WORKER SYSTEM
// ==========================================

async function processUserQueue(userId) {
  const queueKey = `queue:${userId}`;
  const lockKey = `lock:${userId}`;

  try {
    const locked = await redisClient.set(lockKey, "1", "EX", 300, "NX");
    if (!locked) return;

    console.log(`[${userId}] Worker started.`);

    while (true) {
      const jobData = await redisBlocking.blpop(queueKey, 1);

      if (!jobData) {
        const deleted = await redisClient.eval(
          CLEANUP_SCRIPT,
          2,
          queueKey,
          lockKey,
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
          job.temperature,
          job.tools,
          job.tool_choice,
        );

        const queryType = detectQueryType(job.messages, job.files);
        const creditUsage = await logCreditUsage(
          job.organization_id,
          queryType,
          response,
          job.provider,
          job.startTime,
          job.category,
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
          JSON.stringify({ success: true, data: finalResponse }),
        );
        console.log(`[${userId}] Job ${job.jobId} completed`);
      } catch (error) {
        console.error(`[${userId}] Job Failed: ${error.message}`);
        await redisClient.setex(
          `result:${job.jobId}`,
          300,
          JSON.stringify({ success: false, error: error.message }),
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
      if (Date.now() - startTime > timeoutMs) {
        clearInterval(interval);
        reject(new Error("Timeout"));
        return;
      }

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
      ticket_id,
      ticket_categories = [],
    } = req.body;

    if (!messages || !Array.isArray(messages))
      return res.status(400).json({ error: "Missing messages array" });

    const userId = organization_id || "default_org";
    const jobId = `${userId}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
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
      tools: req.body.tools || null,
      tool_choice: req.body.tool_choice || null,
    };

    await redisClient.rpush(`queue:${userId}`, JSON.stringify(job));

    if (!userWorkers[userId]) {
      userWorkers[userId] = processUserQueue(userId);
    }

    const result = await waitForResult(jobId, 180000, req);

    if (result.success) {
      res.json(result.data);

      // Fire and forget - update ticket after response sent
      if (category?.toLowerCase() === "low" && ticket_id) {
        const aiResponse = result.data.choices[0].message.content;
        updateTicket(ticket_id, category, ticket_categories, aiResponse);
      }
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
      startTime,
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

process.on("SIGTERM", async () => {
  console.log("Shutting down...");
  await redisClient.quit();
  await redisBlocking.quit();
  process.exit(0);
});

// ==========================================
// 9. TICKET UPDATE WEBHOOK
// ==========================================

async function updateTicket(ticketId, category, listCategory, resultFromAI) {
  // Guard: Only process if category and ticketId exist
  if (!ticketId || !category) {
    return null;
  }

  // Guard: Only process LOW priority tickets
  if (category.toLowerCase() !== "low") {
    return null;
  }

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

    const userPrompt = `Customer Support Interaction:
${resultFromAI}

Current ticket category: ${category}
Available categories: ${listCategory.join(", ")}

Analyze the interaction and classify this ticket appropriately.`;

    const messages = [
      { role: "system", content: systemPrompt },
      { role: "user", content: userPrompt },
    ];

    const provider = getProvider(null);

    // Call OpenAI with JSON mode
    if (provider === "openai") {
      const config = API_CONFIGS.openai;

      const response = await axios.post(
        `${config.baseUrl}/chat/completions`,
        {
          model: config.chatModel,
          messages: messages,
          temperature: 0.3,
          max_tokens: 500,
          response_format: { type: "json_object" },
        },
        {
          headers: { Authorization: `Bearer ${getApiKey("openai")}` },
          timeout: 30000,
        },
      );

      const classification = JSON.parse(
        response.data.choices[0].message.content,
      );

      // Validate category
      if (!listCategory.includes(classification.category)) {
        classification.category = "general";
        classification.reason = `Original category not in list. Using general.`;
      }

      // Prepare webhook payload
      const payload = {
        ticket_id: ticketId,
        title: classification.title,
        category: classification.category,
        priority: classification.priority,
        reason: classification.reason,
      };

      // Update ticket via webhook
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

      return webhookResponse.data;
    } else if (provider === "gemini") {
      const config = API_CONFIGS.gemini;
      const apiKey = getApiKey("gemini");

      const contents = messages.map((msg) => ({
        role: msg.role === "assistant" ? "model" : "user",
        parts: [{ text: msg.content }],
      }));

      const response = await axios.post(
        `${config.baseUrl}/models/${config.chatModel}:generateContent?key=${apiKey}`,
        {
          contents,
          generationConfig: {
            temperature: 0.3,
            maxOutputTokens: 500,
            responseMimeType: "application/json",
          },
        },
        {
          headers: { "Content-Type": "application/json" },
          timeout: 30000,
        },
      );

      const text =
        response.data.candidates?.[0]?.content?.parts?.[0]?.text || "{}";
      const classification = JSON.parse(text);

      // Validate category
      if (!listCategory.includes(classification.category)) {
        classification.category = "general";
        classification.reason = `Original category not in list. Using general.`;
      }

      // Prepare webhook payload
      const payload = {
        ticket_id: ticketId,
        title: classification.title,
        category: classification.category,
        priority: classification.priority,
        reason: classification.reason,
      };

      // Update ticket via webhook
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
    }
  } catch (error) {
    console.error(`[TICKET] ❌ Failed to update ticket ${ticketId}`);
    console.error(`[TICKET] Error Message:`, error.message);
    console.error(`[TICKET] Error Response Status:`, error.response?.status);
    console.error(
      `[TICKET] Error Response Data:`,
      JSON.stringify(error.response?.data, null, 2),
    );
    console.error(`[TICKET] Full Error:`, error);

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
// 10. AUDIO TRANSCRIPTION (Whisper)
// ==========================================

router.post("/audio", authenticate, async (req, res) => {
  const { url, model } = req.body;

  if (!url) {
    return res.status(400).json({ error: "Missing audio url" });
  }

  try {
    console.log(`🎵 Processing audio transcription`);

    // Download audio file
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
          Authorization: `Bearer ${getApiKey("openai")}`,
          ...form.getHeaders(),
        },
        timeout: 300000,
      },
    );

    // 1. Get the text result
    let transcription = response.data.text || "";

    // 2. 🛡️ SAFETY NET: Handle Instrumental/Silent Audio
    // If Whisper returns empty text (common for music), provide a placeholder.
    // This tricks the Python backend into thinking it found text, so it SAVES the file.
    if (!transcription || transcription.trim().length === 0) {
      console.log(
        "⚠️ Audio has no spoken words (Instrumental/Silence). Using placeholder.",
      );
      transcription =
        "[Audio processed. No spoken words detected (Music/Instrumental).]";
    }

    res.json({
      output: {
        result: transcription,
      },
    });
  } catch (error) {
    console.error("Audio transcription error:", error.message);

    // 3. ERROR SAFETY: Instead of sending 500 (which triggers rollback),
    // send the error message as the "transcription".
    // This ensures the file is SAVED so you can debug it later.
    res.json({
      output: {
        result: `[Error processing audio: ${error.message}]`,
      },
    });
  }
});

// ==========================================
// 11. IMAGE OCR (GPT-4o Vision)
// ==========================================

router.post("/image/ocr", authenticate, async (req, res) => {
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
              {
                type: "image_url",
                image_url: { url: image_url },
              },
            ],
          },
        ],
        max_tokens: 300,
      },
      {
        headers: {
          Authorization: `Bearer ${getApiKey("openai")}`,
          "Content-Type": "application/json",
        },
        timeout: 60000,
      },
    );

    let content = response.data?.choices?.[0]?.message?.content || "";

    // 2. SAFETY NET: If AI returns empty string or the specific tag, give Python something safe
    if (
      !content ||
      content.trim() === "" ||
      content.includes("[NO_TEXT_DETECTED]")
    ) {
      console.log("⚠️ Image has no text. Using placeholder to save file.");
      content = "Visual content only. No text detected in this image.";
    }

    // 3. RETURN CONTENT (Python backend will now save the file instead of deleting it)
    res.json({ content });
  } catch (error) {
    console.error("Image OCR error:", error.message);
    // 4. ERROR HANDLER: Return error as text so the file is saved for debugging
    res.json({ content: `Error processing image: ${error.message}` });
  }
});

module.exports = router;
console.log(`🚀 Proxy Router v2 loaded [Production Mode]`);
