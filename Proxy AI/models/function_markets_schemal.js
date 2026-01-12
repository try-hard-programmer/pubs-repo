// models/FunctionDefinition.js
const mongoose = require('mongoose');

// Define the schema for a single parameter
const parameterSchema = new mongoose.Schema({
    name: {
        type: String,
        required: true,
    },
    type: {
        type: String,
        required: true,
    },
    format: {
        type: String,
        default: '',
    },
    required: {
        type: Boolean,
        required: true,
    },
    description: {
        type: String,
        required: true,
    },

}, { _id: false }); // prevent auto generate _id field

// Define the main schema for a function definition
const functionDefinitionSchema = new mongoose.Schema({
    function_name: {
        type: String,
        required: true,
    },
    code: {
        type: String,
        required: true,
        unique: true, // Ensure that each function code is unique
    },
    title: {
        type: String,
        required: true,
    },
    metadata: {
        type: String,
        required: true,
    },
    retrieve: {
        type: Boolean,
        required: true,
    },
    creator: {
        type: String,
        required: true,
    },
    description: {
        type: String,
        required: true,
    },
    long_description: {
        type: String,
        required: false,
    },
    version: {
        type: Number,
        required: true,
    },
    status: {
        type: Boolean,
        required: true,
    },
    channel: {
        type: String,
        required: true,
    },
    categories: {
        type: [String], // Array of strings
        default: []
    },
    platforms: {
        type: [String], // Array of strings
        default: []
    },
    params: [parameterSchema], // Array of parameter objects
}, {
    timestamps: true // This will add createdAt and updatedAt fields
});

const FunctionMarket = mongoose.model('function_markets', functionDefinitionSchema);

module.exports = FunctionMarket;