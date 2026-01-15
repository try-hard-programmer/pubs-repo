//routers/function_router.js
const express = require('express');
const router = express.Router();

// Import your model
const FunctionMarket = require('../models/function_markets_schemal');

router.get('/', async (req, res) => {
    const page = parseInt(req.query.page) || 1; // Default to page 1
    const perPage = parseInt(req.query.per_page) || 10; // Default to 10 items per page
    const { search, categories, platforms, creator } = req.query;

    const projections = {
        _id: 0,
        function_name: 1,
        code: 1,
        title: 1,
        metadata: 1,
        creator: 1,
        description: 1,
        version: 1,
        channel: 1,
        categories: 1,
        platforms: 1,
        createdAt: 1,
        updatedAt: 1,
    };

    try {
        let query = {};
        const andConditions = [];

        if (search) {
            andConditions.push({
                $or: [
                    { code: { $regex: search, $options: 'i' } },
                    { title: { $regex: search, $options: 'i' } },
                    { description: { $regex: search, $options: 'i' } },
                ]
            });
        }

        // Filters for categories, platforms, and creator
        if (categories) {
            if (Array.isArray(categories)) {
                andConditions.push({ categories: { $in: categories } });
            } else {
                andConditions.push({ categories: { $in: [categories] } });
            }
        }
        if (platforms) {
            if (Array.isArray(platforms)) {
                andConditions.push({ platforms: { $in: platforms } });
            } else {
                andConditions.push({ platforms: { $in: [platforms] } });
            }
        }
        if (creator) {
            andConditions.push({ creator: creator });
        }

        if (andConditions.length > 0) {
            query = { $and: andConditions };
        }

        const totalDocuments = await FunctionMarket.countDocuments(query);
        const totalPages = Math.ceil(totalDocuments / perPage);

        const functions = await FunctionMarket.find(query, projections, { lean: true })
            .sort({ createdAt: -1 })
            .skip((page - 1) * perPage)
            .limit(perPage)
            .lean();

        res.json({
            current_page: page,
            total_pages: totalPages,
            content: functions,
        });
    } catch (error) {
        console.error('Error getting functions:', error);
        res.status(500).json({ success: false, message: 'Error getting functions', error: error.message });
    }
});

router.get('/list-platforms', async (req, res) => {
    const page = parseInt(req.query.page) || 1; // Default to page 1
    const perPage = parseInt(req.query.per_page) || 10; // Default to 10 items per page
    try {
        const distinctPlatforms = await FunctionMarket.distinct('platforms');
        const totalDocuments = distinctPlatforms.length;
        const totalPages = Math.ceil(totalDocuments / perPage);

        const platforms = distinctPlatforms.slice((page - 1) * perPage, page * perPage);
        res.json({
            current_page: page,
            total_pages: totalPages,
            content: platforms,
        });
    } catch (error) {
        console.error('Error getting platforms:', error);
        res.status(500).json({ success: false, message: 'Error getting platforms', error: error.message });
    }
});

router.get('/list-categories', async (req, res) => {
    const page = parseInt(req.query.page) || 1; // Default to page 1
    const perPage = parseInt(req.query.per_page) || 10; // Default to 10 items per page
    try {
        const distinctPlatforms = await FunctionMarket.distinct('categories');
        const totalDocuments = distinctPlatforms.length;
        const totalPages = Math.ceil(totalDocuments / perPage);

        const platforms = distinctPlatforms.slice((page - 1) * perPage, page * perPage);
        res.json({
            current_page: page,
            total_pages: totalPages,
            content: platforms,
        });
    } catch (error) {
        console.error('Error getting platforms:', error);
        res.status(500).json({ success: false, message: 'Error getting platforms', error: error.message });
    }
});

router.get('/list-creators', async (req, res) => {
    const page = parseInt(req.query.page) || 1; // Default to page 1
    const perPage = parseInt(req.query.per_page) || 10; // Default to 10 items per page
    try {
        const distinctPlatforms = await FunctionMarket.distinct('creator');
        const totalDocuments = distinctPlatforms.length;
        const totalPages = Math.ceil(totalDocuments / perPage);

        const platforms = distinctPlatforms.slice((page - 1) * perPage, page * perPage);
        res.json({
            current_page: page,
            total_pages: totalPages,
            content: platforms,
        });
    } catch (error) {
        console.error('Error getting platforms:', error);
        res.status(500).json({ success: false, message: 'Error getting platforms', error: error.message });
    }
});


router.get('/:code', async (req, res) => {
    const { code } = req.params;
    try {
        const functionData = await FunctionMarket.findOne({ code }).lean();
        if (!functionData) {
            return res.status(404).json({ success: false, message: 'Function not found' });
        }
        res.json(functionData);
    } catch (error) {
        console.error('Error getting function:', error);
        res.status(500).json({ success: false, message: 'Error getting function', error: error.message });
    }
});

// Add an endpoint to save the function data
router.post('/', async (req, res) => {
    try {
        // Assuming the request body contains the function data
        const functionData = req.body;
        // Validate the data against the schema
        const newFunction = new FunctionMarket(functionData);
        await newFunction.save();
        res.status(201).json({ success: true, message: 'Function data saved successfully', data: newFunction });
    } catch (error) {
        console.error('Error saving function data:', error);
        res.status(500).json({ success: false, message: 'Error saving function data', error: error.message });
    }
});



module.exports = router;