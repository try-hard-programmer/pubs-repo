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
        
        # Helper to find ID in nested structure
        msg_id = "unknown"
        try:
            # Try finding ID in the new nested structure
            msg_id = message_data.get("data", {}).get("message", {}).get("_data", {}).get("id", {}).get("id")
            # Fallback for older flat structure
            if not msg_id: msg_id = message_data.get("message_id")
        except: pass

        async with httpx.AsyncClient(timeout=30.0) as client: # Increased timeout for media
            response = await client.post(
                MAIN_SERVICE_URL,
                json=message_data,
                headers=headers
            )
            
            if response.status_code != 200:
                logger.warning(f"⚠️ Main service returned {response.status_code}: {response.text}")
            else:
                logger.info(f"🚀 Forwarded message {msg_id} to Main Service")
                
    except Exception as e:
        logger.error(f"Failed to forward message to Main Service: {e}")