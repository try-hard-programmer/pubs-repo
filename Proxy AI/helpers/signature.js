// /Users/aviana/Projects/streamtify/aigent-proxy-service/helpers/signature.js

const crypto = require('crypto');

/**
 * Generates a SHA256 signature for a given payload and secret.
 *
 * @param {object} payload - The data to be signed.
 * @param {string} secret - The secret key used for signing.
 * @returns {string} The SHA256 signature of the payload.
 */
const generateSignature = (payload, secret) => {
    // 1. Sort keys alphabetically
    const sortedPayload = {};
    Object.keys(payload).sort().forEach(key => {
        sortedPayload[key] = payload[key];
    });

    // 2. Stringify the sorted payload
    const stringifiedPayload = JSON.stringify(sortedPayload);

    // 3. Create the signature using SHA256
    return crypto.createHmac('sha256', secret)
        .update(stringifiedPayload)
        .digest('hex');
};

/**
 * Verifies if a given signature is valid for a payload and secret.
 *
 * @param {object} payload - The data that was signed.
 * @param {string} signature - The signature to verify.
 * @param {string} secret - The secret key used for signing.
 * @returns {boolean} True if the signature is valid, false otherwise.
 */
const verifySignature = (payload, signature, secret) => {
    // Generate the signature from payload
    const generatedSignature = generateSignature(payload, secret);

    // Compare the generated signature with the provided signature
    return generatedSignature === signature;
};

module.exports = { generateSignature, verifySignature };