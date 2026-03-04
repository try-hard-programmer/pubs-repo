//routers/function_router.js
const express = require("express");
const axios = require("axios");
const {whenChannelReady} = require("../rabbitmq");
const router = express.Router();

// Fungsi untuk menentukan target host berdasarkan model
const getTargetHost = (model, endpoint = "chat") => {
    // model = "gpt-3.5-turbo";
    if (endpoint === "embeddings") {
        return "https://api.openai.com/v1/embeddings";
    }

    switch (model) {
        case "gpt-3.5-turbo":
            return "https://api.openai.com/v1/chat/completions";
        default:
            return "https://api.openai.com/v1/chat/completions";
    }
};


const getAuthorization = (model) => {
    model = "gpt-3.5-turbo";
    switch (model) {
        case "gpt-3.5-turbo":
            return process.env.GPT_35_TURBO_KEY;
        default:
            return process.env.OPENAI_API_KEY;
    }
};

const getModel = (prompt_category, endpoint = "chat") => {
    if (endpoint === "embeddings") {
        return "text-embedding-3-small"; // Default embedding model
    }

    switch (prompt_category) {
        case "prompt_greetings":
            return "gpt-3.5-turbo";
        case "prompt_agent":
            return "gpt-3.5-turbo";
        case "prompt_classification":
            return "gpt-3.5-turbo";
        case "prompt_function_analysis":
            return "gpt-3.5-turbo";
        case "prompt_rag":
            return "gpt-3.5-turbo";
        case "prompt_param_extractor":
            return "gpt-3.5-turbo";
        case "image_analysis":
            return "gpt-4.1-mini";
        default:
            return "gpt-3.5-turbo";
    }
};

// Exchange name
// const EXCHANGE_NAME = "api_request_exchange";
// const EXCHANGE_TYPE = 'topic';

const Partner = require("../models/partner_schema");

router.use("/chat", async (req, res) => {
    const {authorization, "x-prompt-category": xPromptCategory} = req.headers;

    // const model = req.body?.model;
    const model = getModel(xPromptCategory);
    const target = getTargetHost(model);

    // for dev
    req.body["model"] = model;

    console.log("🔵 - ", xPromptCategory, model, target);

    try {
        if (!authorization) {
            return res.status(403).json({success: false, message: "Unauthorized"});
        }

        const platform_id = authorization.split(" ")[1];
        // check partner credit
        const partner = await Partner.findOne({platform_id: platform_id}).lean();
        if (!partner) {
            console.log(platform_id)
            console.log({success: false, message: "Partner not found"});
            return res
                .status(403)
                .json({success: false, message: "Partner not found"});
        }

        if (partner.total_credit <= 0) {
            console.log({success: false, message: "Insufficient credit"});
            return res
                .status(403)
                .json({success: false, message: "Insufficient credit"});
        }

        console.log(`🚀 Forwarding request to [${target}] with model [${model}]`);

        // forward request ke target host
        const forward = await axios.post(target, req.body, {
            headers: {
                "content-type": "application/json",
                Authorization: `Bearer ${getAuthorization(model)}`,
            },
        });

        // log response
        // TODO: set pricing
        console.log("🟢 Response:", JSON.stringify(forward.data));

        // response dari target host ke client
        res.send(forward.data);
    } catch (error) {
        console.error(
            "Error processing request:",
            error.response?.data || error.message
        );
        res.status(500).send({error: "Internal server error"});
    }
});

router.use("/audio", async (req, res) => {
    const {authorization} = req.headers;
    const {url, model} = req.body;

    console.log("🔵 Request Audio - ", JSON.stringify(req.body));

    try {
        if (!authorization) {
            return res.status(403).json({success: false, message: "Unauthorized"});
        }

        if (!url) {
            return res.status(400).json({success: false, message: "Missing audio url"});
        }

        if (!model) {
            return res.status(400).json({success: false, message: "Missing model"});
        }

        const platform_id = authorization.split(" ")[1];

        // check partner credit
        const partner = await Partner.findOne({platform_id: platform_id}).lean();
        if (!partner) {
            console.log({success: false, message: "Partner not found"});
            return res
                .status(403)
                .json({success: false, message: "Partner not found"});
        }

        if (partner.total_credit <= 0) {
            console.log({success: false, message: "Insufficient credit"});
            return res
                .status(403)
                .json({success: false, message: "Insufficient credit"});
        }

        console.log(`🚀 Forwarding request to whisper with model [${model}]`);

        // forward request ke target host
        const forward = await axios.post("https://api.runpod.ai/v2/whisper-v3-large/runsync",
            {
                "input": {
                    "prompt": "",
                    "audio": url
                }
            }, {
                headers: {
                    "content-type": "application/json",
                    Authorization: `Bearer ${process.env.RUNPOD_API_KEY}`,
                },
            });

        // response dari target host ke client
        try {
            delete forward.data.output.cost;
            delete forward.data.workerId;
        } catch (e) {
          console.error("Error deleting fields from response:", e);
        }

        console.log(JSON.stringify(forward.data))

        res.send(forward.data);
    } catch (error) {
        console.error(
            "Error processing request:",
            error.response?.data || error.message
        );
        res.status(500).send({error: "Internal server error"});
    }
})

router.use("/image/ocr", async (req, res) => {
    const {authorization} = req.headers;
    const {image_url} = req.body;

    try {
        if (!authorization) {
            return res.status(403).json({success: false, message: "Unauthorized"});
        }

        const platform_id = authorization.split(" ")[1];

        if(!image_url){
            return res.status(400).json({success: false, message: "Missing image_url"});
        }

        // check partner credit
        const partner = await Partner.findOne({platform_id: platform_id}).lean();
        if (!partner) {
            console.log({success: false, message: "Partner not found"});
            return res
                .status(403)
                .json({success: false, message: "Partner not found"});
        }

        if (partner.total_credit <= 0) {
            console.log({success: false, message: "Insufficient credit"});
            return res
                .status(403)
                .json({success: false, message: "Insufficient credit"});
        }

        console.log(`🚀 Forwarding request to [OPEN AI] with model [gpt-4.1-nano for image]`);

        const reqBody = {
            "model": "gpt-4.1-nano",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract only the text found in the image. Output text only, no extras."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }]

                }
            ],
            "max_tokens": 300
        }

        // forward request ke target host
        try {
            const forward = await axios.post("https://api.openai.com/v1/chat/completions", reqBody, {
                headers: {
                    "content-type": "application/json",
                    Authorization: `Bearer ${process.env.GPT_35_TURBO_KEY}`,
                },
            });

            // Ambil hanya content dari response
            const content = forward.data?.choices?.[0]?.message?.content;
            res.send({ content });
        } catch (error) {
            // Ambil pesan error jika gagal
            const message = error.response?.data?.message || error.message || "Internal server error";
            res.status(500).send({ error: message });
        }
    } catch (error) {
        console.error(
            "Error processing request:",
            error.response?.data || error.message
        );
        res.status(500).send({error: "Internal server error"});
    }
});

router.use("/embeddings", async (req, res) => {
    const {authorization} = req.headers;
    const requestedModel = req.body?.model;

    try {
        if (!authorization) {
            return res.status(403).json({success: false, message: "Unauthorized"});
        }

        // Validasi input body
        if (!req.body?.input) {
            return res.status(400).json({success: false, message: "Missing input field"});
        }

        const platform_id = authorization.split(" ")[1];

        // check partner credit
        const partner = await Partner.findOne({platform_id: platform_id}).lean();
        if (!partner) {
            console.log({success: false, message: "Partner not found"});
            return res
                .status(403)
                .json({success: false, message: "Partner not found"});
        }

        if (partner.total_credit <= 0) {
            console.log({success: false, message: "Insufficient credit"});
            return res
                .status(403)
                .json({success: false, message: "Insufficient credit"});
        }

        // Gunakan model dari request atau default
        const model = requestedModel || getModel(null, "embeddings");
        const target = getTargetHost(model, "embeddings");

        // Set model di request body jika belum ada
        if (!req.body.model) {
            req.body.model = model;
        }

        console.log(`🚀 Forwarding embeddings request to [${target}] with model [${model}]`);

        // forward request ke target host
        const forward = await axios.post(target, req.body, {
            headers: {
                "content-type": "application/json",
                Authorization: `Bearer ${getAuthorization(model)}`,
            },
        });

        // log response
        console.log("🟢 Embeddings Response:", JSON.stringify({
            model: forward.data.model,
            usage: forward.data.usage
        }));

        // response dari target host ke client
        res.send(forward.data);
    } catch (error) {
        console.error(
            "Error processing embeddings request:",
            error.response?.data || error.message
        );
        res.status(error.response?.status || 500).send({
            error: error.response?.data?.error?.message || "Internal server error"
        });
    }
});

module.exports = router;
