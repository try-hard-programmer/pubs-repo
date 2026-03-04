//routers/function_router.js
const express = require('express');
const router = express.Router();
const dayjs = require('dayjs');

// Import your model
const Topup = require('../models/topup_schema');
const Product = require('../models/product_schema');
const Partner = require('../models/partner_schema');
const {verifySignature} = require("../helpers/signature");
const diko = require("../helpers/diko_forwarder");

// Add an endpoint to save the function data
router.post('/payment', async (req, res) => {
    try {
        const { destination, supplierCode, transactionId } = req.body;
        const terminal_key = req.headers['x-terminal-key'];
        const secret = process.env.TERMINAL_KEY;

        if (!terminal_key) {
            return res.status(401).json({ success: false, message: 'Unauthorized - missing key' });
        }

        // Define secret key (you can get from .env)

        if(terminal_key !== secret){
            return res.status(401).json({ success: false, message: 'Unauthorized - invalid key' });
        }

        // find partner by platform_id (destination)
        const partner = await Partner.findOne({ "platform_id": destination }).lean();
        if (!partner) {
            console.log('Partner not found for platform_id:', destination);
            return res.status(404).json({ success: false, message: 'Partner not found' });
        }

        // find product by code
        const product = await Product.findOne({ "code": supplierCode }).lean();
        if (!product) {
            console.log('Product not found for code:', supplierCode);
            return res.status(404).json({ success: false, message: 'Product not found' });
        }

        const current_validity_date = dayjs(partner.validity_date).toDate();

        // validity date = current_date + product validity days
        const new_validity_date = dayjs().add(product.validity, 'day').toDate();

        // check if new validity date is greater than current validity date
        let validity_date = current_validity_date;
        if (current_validity_date < new_validity_date) {
            validity_date = new_validity_date;
        }

        // create topup
        const new_top_up = new Topup({
            platform_id: destination,
            total_credit: product.credit,
            price: product.price,
            validity_date: dayjs(new_validity_date).toDate(),
            created_at: new Date()
        })
        const top_up = await new_top_up.save();

        // update partner validity date
        await Partner.findOneAndUpdate({ "platform_id": destination }, {
            validity_date: dayjs(validity_date).toDate(),
            $inc: { total_credit: product.credit }
        });

        const responseData = {
            success: true,
            transactionId,
            price: product.price,
            amount: product.price,
            totalAmount: product.price,
            serialNumber: top_up._id,
            message: "Success",
            rawResponse: JSON.stringify(new_top_up),
            status: 2
        }

        // send notif callback
        diko.forward(responseData)


        res.json(responseData);

    } catch (error) {
        console.error('Error saving Partner data:', error);
        res.status(500).json({ success: false, message: 'Error saving Partner data', error: error.message });
    }
});

// / New route for payment success page
// New route for payment success page
router.get('/payment/success', (req, res) => {
    const html = `
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Payment Successful!</title>
        <style>
            body {
                font-family: 'Arial', sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                background-color: #f4f6f8; /* Light grey background */
                margin: 0;
                color: #333; /* Dark grey text */
            }
            .container {
                background-color: #fff;
                padding: 40px;
                border-radius: 12px; /* Rounded corners */
                box-shadow: 0 8px 16px rgba(0, 0, 0, 0.1); /* More pronounced shadow */
                text-align: center;
                max-width: 500px; /* Limit the container width */
                width: 90%; /* Responsive width */
            }
            h1 {
                color: #28a745; /* Green for success */
                margin-bottom: 20px;
                font-size: 2.5rem; /* Larger heading */
            }
            p {
                font-size: 1.1rem; /* Slightly larger paragraph text */
                line-height: 1.6;
                margin-bottom: 30px;
            }
            .checkmark {
                color: #28a745;
                font-size: 6rem;
                line-height: 1;
                margin-bottom: 20px;
            }
            .close-message {
                font-size: 0.9rem;
                color: #666; /* Lighter text for the closing message */
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="checkmark">✓</div>
            <h1>Payment Successful!</h1>
            <p>Your payment has been processed successfully. You can now close this window.</p>
            <p class="close-message">This window will automatically close in a few seconds.</p>
        </div>
        <script>
            window.onload = function() {
                setTimeout(function() {
                    window.close();
                }, 1000); // Close after 3 seconds
            };
        </script>
    </body>
    </html>
    `;
    res.send(html);
});

// New route for payment failed page
router.get('/payment/failed', (req, res) => {
    const html = `
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Payment Failed</title>
        <style>
            body {
                font-family: 'Arial', sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                background-color: #f4f6f8;
                margin: 0;
                color: #333;
            }
            .container {
                background-color: #fff;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 8px 16px rgba(0, 0, 0, 0.1);
                text-align: center;
                max-width: 500px;
                width: 90%;
            }
            h1 {
                color: #dc3545; /* Red for error */
                margin-bottom: 20px;
                font-size: 2.5rem;
            }
            p {
                font-size: 1.1rem;
                line-height: 1.6;
                margin-bottom: 30px;
            }
            .crossmark {
                color: #dc3545;
                font-size: 6rem;
                line-height: 1;
                margin-bottom: 20px;
            }
            .close-message {
                font-size: 0.9rem;
                color: #666;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="crossmark">✕</div>
            <h1>Payment Failed</h1>
            <p>We're sorry, but there was an error processing your payment. Please try again later or contact support.</p>
            <p class="close-message">This window will automatically close in a few seconds.</p>
        </div>
        <script>
            window.onload = function() {
                setTimeout(function() {
                    window.close();
                }, 1000); // Close after 3 seconds
            };
        </script>
    </body>
    </html>
    `;
    res.send(html);
});


module.exports = router;

module.exports = router;
