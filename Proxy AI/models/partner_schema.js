// /Users/aviana/Projects/streamtify/aigent-proxy-service/models/partner_schema.js

const mongoose = require('mongoose');

const partnerSchema = new mongoose.Schema({
    id: {
        type: String,
        required: true,
        unique: true, // Ensure id is unique
        index: true, // Index for faster queries
    },
    platform_id: {
        type: String,
        required: true,
        trim: true,
        index: true,
        unique: true
    },
    validity_date: {
        type: Date,
        required: true,
    },
    total_credit: {
        type: Number,
        required: true,
        default: 0, // Default to 0 if not provided
    },
    registered_at: {
        type: Date,
        default: Date.now, // Automatically set the current date/time on creation
    },
}, {
    timestamps: { createdAt: 'registered_at', updatedAt: 'updated_at' } // Use custom names for timestamps
});

const Partner = mongoose.model('partners', partnerSchema);

module.exports = Partner;
