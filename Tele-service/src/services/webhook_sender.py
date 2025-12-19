import os
import httpx
import logging
from src.config.config import config

logger = logging.getLogger(__name__)

# FIXED: Read from environment variable, not config.HOST
MAIN_SERVICE_URL = config.MAIN_SERVICE_URL

async def forward_to_main_service(message_data: dict):
    """
    Forwards the standardized message data to the Main Service via Webhook.
    """
    try:
        headers = {
            "X-API-Key": config.WEBHOOK_SECRET,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                MAIN_SERVICE_URL,
                json=message_data,
                headers=headers
            )
            
            if response.status_code != 200:
                logger.warning(f"⚠️ Main service returned {response.status_code}: {response.text}")
            else:
                logger.info(f"🚀 Forwarded message {message_data.get('message_id', 'unknown')} to Main Service")
                
    except Exception as e:
        logger.error(f"Failed to forward message to Main Service: {e}")