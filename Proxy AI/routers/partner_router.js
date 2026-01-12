//routers/function_router.js
const express = require('express');
const router = express.Router();

// Import your model
const Partner = require('../models/partner_schema');
const TopUp = require('../models/topup_schema');

// Add an endpoint to save the function data
router.get('/me', async (req, res) => {
    try {
        const { platform_id } = req.query;
        let partner = await Partner.findOne({ "platform_id": platform_id }).lean();
        if (!partner) {
            // create partner
            const newPartner = new Partner({
                id: new Date().getTime(), // Use current timestamp as unique ID
                platform_id: platform_id,
                validity_date: new Date(),
                total_credit: 0,
                registered_at: new Date()
            })
            partner = await newPartner.save();
        }

        res.status(201).json({ success: true, message: 'Get Partner successfully', data: partner });
    } catch (error) {
        console.error('Error saving Partner data:', error);
        res.status(500).json({ success: false, message: 'Error saving Partner data', error: error.message });
    }
});

router.get('/topup-history', async (req, res) => {
    try {
        const { platform_id, page = 1, per_page = 10 } = req.query;

        // check if partner exists
        const partner = await Partner.findOne({ "platform_id": platform_id }).lean();
        if (!partner) {
            return res.status(404).json({ success: false, message: 'Partner not found' });
        }

        const parsedPage = parseInt(page, 10);
        const parsedLimit = parseInt(per_page, 10);

        if (isNaN(parsedPage) || parsedPage < 1) {
            return res.status(400).json({ success: false, message: 'Invalid page number' });
        }

        if (isNaN(parsedLimit) || parsedLimit < 1) {
            return res.status(400).json({ success: false, message: 'Invalid limit number' });
        }

        const skip = (parsedPage - 1) * parsedLimit;

        const totalTopUps = await TopUp.countDocuments({ platform_id });

        const totalPages = Math.ceil(totalTopUps / parsedLimit);

        const topUps = await TopUp.find({ "platform_id": platform_id })
            .sort({ created_at: -1 }) // Sort by created_at in descending order
            .skip(skip)
            .limit(parsedLimit)
            .lean();

        res.status(200).json({
            success: true,
            message: 'Get Topup History successfully',
            content: topUps,
            total_pages: totalPages,
            current_page: parsedPage
        });
    } catch (error) {
        console.error('Error getting Topup History:', error);
        res.status(500).json({ success: false, message: 'Error getting Topup History', error: error.message });
    }
});

module.exports = router;
