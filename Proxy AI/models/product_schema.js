// /Users/aviana/Projects/streamtify/aigent-proxy-service/models/product_schema.js

const mongoose = require('mongoose');

const productSchema = new mongoose.Schema({
    code: {
        type: String,
        required: true,
        unique: true,
        trim: true,
    },
    credit: {
        type: Number,
        required: true,
    },
    validity: {
        type: Number,
        required: true,
    },
    price: {
        type: Number,
        required: true,
    },
});

const Product = mongoose.model('products', productSchema);

module.exports = Product;