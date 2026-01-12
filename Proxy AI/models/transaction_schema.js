// /Users/aviana/Projects/streamtify/aigent-proxy-service/models/transaction_schema.js

const mongoose = require('mongoose');

// Define a sub-schema for the 'detail' field
const detailSchema = new mongoose.Schema({
    model: {
        type: String,
        required: true,
    },
    prompt: {
        type: Object, // Or you can use Mixed type
        required: true,
    },
    completion: {
        type: Object, // Or you can use Mixed type
        required: true,
    },
    function_code: {
        type: String,
        default: null
    }
}, { _id: false }); // prevent auto generate _id field

// Define the main schema for a transaction
const transactionSchema = new mongoose.Schema({
    id: {
        type: String, // Or Number, depending on how you generate IDs
        required: true,
        unique: true,
        trim: true
    },
    platform_id: {
        type: String,
        required: true,
        trim: true,
        index: true,
    },
    session: {
        type: String,
        required: true,
        trim: true
    },
    usage: {
        type: Number,
        required: true,
    },
    total_credit: {
        type: Number,
        required: true,
    },
    detail: {
        type: detailSchema, // Embed the detail sub-schema here
        required: true,
    },
}, {
    timestamps: true // This will add createdAt and updatedAt fields
});

const Transaction = mongoose.model('transactions', transactionSchema);

module.exports = Transaction;