// ==========================================
// FILE: routers/proxy_router_v2.js
// PURPOSE: Multi-agent LLM proxy with Redis Queue (Dual Connection)
// VERSION: 3.0 - OpenAI Only
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

// Update this rate periodically or pull from an FX API
const USD_TO_IDR = process.env.USD_TO_IDR
  ? parseFloat(process.env.USD_TO_IDR)
  : 16300;

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

function calculateEmbeddingCost(tokens) {
  return tokens * (0.02 / 1e6);
}

// ==========================================
// 5. LOGGING FUNCTIONS
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
// 6. CHAT HANDLER (OpenAI Only)
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
          image_url: { url: file.url || `data:image/jpeg;base64,${file.data}` },
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
// 7. EMBEDDING FUNCTIONS
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
// 8. WORKER SYSTEM
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

async function waitForResult(jobId, timeoutMs) {
  const startTime = Date.now();
  const resultKey = `result:${jobId}`;
  let pollInterval = 100;
  const maxInterval = 500;

  return new Promise((resolve, reject) => {
    const poll = async () => {
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
    };

    poll();
  });
}

// ==========================================
// 8. ROUTES
// ==========================================

router.get("/test", (req, res) => {
  res.json({
    message: "Proxy Router v3 - OpenAI Only + Queue",
    timestamp: new Date(),
    redis_status: "Dual Connection Active",
  });
});

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

    const embeddingUsage = await logEmbeddingUsage(
      organization_id,
      response,
      startTime,
    );

    logCost("/embeddings", requestId, {
      orgId: organization_id,
      queryType: "embedding",
      totalTokens:
        response.usage?.total_tokens || response.usage?.prompt_tokens || 0,
      costUsd: embeddingUsage.cost_usd,
      responseMs: embeddingUsage.response_time_ms,
      extra: `texts=${textsToEmbed?.length || 0}`,
    });

    const finalResponse = {
      ...response,
      metadata: {
        request_id: requestId,
        provider: "openai",
        timestamp: new Date().toISOString(),
        credits_used: embeddingUsage.credits_used,
        response_time_ms: embeddingUsage.response_time_ms,
        cost_usd: embeddingUsage.cost_usd,
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
// 10. AUDIO TRANSCRIPTION (Whisper)
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

    // Whisper pricing: $0.006 per minute — we don't know exact duration,
    // so we log $0 with a note. Override by setting audio duration in req.body.
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
// 11. IMAGE OCR (GPT-4o Vision)
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

    // gpt-4o-mini vision: ~$0.15/1M input + $0.6/1M output
    // Image token cost is approx 85 tokens for low-res, 765 for high-res
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
        max_tokens: 500, // Increased to allow full descriptions of charts
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

    // If the vision model deems it decorative, scrub the output
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
    // Return empty on error so we don't inject error logs into the RAG chunks
    res.json({ content: "" });
  }
});

// ==========================================
// 12. FILE MANAGER CHAT
// ==========================================

router.post("/chat/filemanager", authenticate, async (req, res) => {
  const startTime = Date.now();
  const requestId = `req-${Date.now()}`;

  const detectQueryTypeLocal = (messages, files) => {
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
  };

  const logCreditUsageLocal = async (orgId, queryType, response, startTime) => {
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
  };

  const handleOpenAILocal = async (
    messages,
    files = [],
    temperature = 0.7,
    response_format = null,
  ) => {
    const config = API_CONFIGS.openai;

    const hasInlineImages = messages.some(
      (m) =>
        Array.isArray(m.content) &&
        m.content.some((p) => p.type === "image_url"),
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
  };

  const waitForResultLocal = async (jobId, timeoutMs) => {
    const t0 = Date.now();
    return new Promise((resolve, reject) => {
      const interval = setInterval(async () => {
        try {
          if (Date.now() - t0 > timeoutMs) {
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
        } catch (e) {
          clearInterval(interval);
          reject(e);
        }
      }, 100);
    });
  };

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

    const locked = await redisClient.set(lockKey, "1", "EX", 300, "NX");
    if (locked) {
      (async () => {
        try {
          console.log(`[${userId}] FileManager Worker started.`);

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
                console.log(`[${userId}] FileManager Worker stopped (Idle).`);
                break;
              }
              continue;
            }

            const [, jobStr] = jobData;
            const j = JSON.parse(jobStr);
            console.log(`[${userId}] FileManager Processing Job ${j.jobId}`);

            try {
              const response = await handleOpenAILocal(
                j.messages,
                j.files,
                j.temperature,
                j.response_format,
              );

              const queryType = detectQueryTypeLocal(j.messages, j.files);
              const creditUsage = await logCreditUsageLocal(
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
                  cost_usd: creditUsage.cost_usd,
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
              console.log(`[${userId}] FileManager Job ${j.jobId} completed`);
            } catch (error) {
              console.error(
                `[${userId}] FileManager Job Failed: ${error.message}`,
              );
              await redisClient.setex(
                `result:${j.jobId}`,
                300,
                JSON.stringify({ success: false, error: error.message }),
              );
            }
          }
        } catch (error) {
          console.error(`[${userId}] FileManager Worker Crashed:`, error);
          await redisClient.del(lockKey).catch(() => {});
        }
      })();
    }

    const result = await waitForResultLocal(jobId, 180000);

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
    if (error.message !== "Client disconnected") {
      console.error(`[ERROR] ${requestId}: ${error.message}`);
    }
    if (!res.headersSent) res.status(500).json({ error: error.message });
  }
});

module.exports = router;
console.log(`🚀 Proxy Router v3 loaded [OpenAI Only Mode]`);
