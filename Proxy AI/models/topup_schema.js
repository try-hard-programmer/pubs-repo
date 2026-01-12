// /Users/aviana/Projects/streamtify/aigent-proxy-service/models/topup_schema.js

const mongoose = require('mongoose');

const topupSchema = new mongoose.Schema({
    platform_id: {
        type: String,
        required: true,
        trim: true,
        index: true,
    },
    total_credit: {
        type: Number,
        required: true,
    },
    price: {
        type: Number,
        required: true,
    },
    validity_date: {
        type: Date,
        required: true,
    },
    created_at: {
        type: Date,
        default: Date.now, // Automatically set the current date/time on creation
    },
});

const Topup = mongoose.model('topups', topupSchema);

module.exports = Topup;