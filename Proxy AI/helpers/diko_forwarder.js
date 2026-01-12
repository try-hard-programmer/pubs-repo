const axios = require("axios");
const controller = {}

controller.forward = async ({transactionId, price, serialNumber, message, rawResponse, status}) => {

    // check validation refClient, price, message, rawResponse, status is required
    if (!transactionId || !message || !rawResponse || status == null) {
        console.log('🔴 Invalid payload' + JSON.stringify({
            transactionId,
            price,
            serialNumber,
            message,
            rawResponse,
            status
        }))
        return
    }

    const payload = {
        transactionId,
        price,
        serialNumber,
        message,
        rawResponse,
        status
    }

   setTimeout(async () => {
       try {
           console.log(`⬆️🟣 Forward transaction request: ${JSON.stringify(payload)}`)
           const fwd = await axios.post(process.env.DIKO_HOST + '/api/v1/terminals/callback/prepaid-postpaid', payload)
           console.log(`⬇️🟣 Forward transaction Response: ${JSON.stringify(fwd.data)}`)
       } catch (e) {
           console.log(e)
       }
   }, 1000)

}

module.exports = controller