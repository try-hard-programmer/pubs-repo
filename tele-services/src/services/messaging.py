"""Messaging Service."""
import logging
import time
from src.database import db
from src.services.webhook_sender import forward_to_main_service

logger = logging.getLogger(__name__)

async def handle_incoming_message(message_data: dict) -> None:
    """
    1. Save to SQLite (Local History)
    2. Convert to "WhatsApp-Style" Format
    3. Forward to Main AI Service
    """
    try:
        # --- 1. Save to Local DB (Keep using the simple format here) ---
        await db.get_or_create_conversation(
            telegram_account_id=message_data["account_id"],
            chat_id=message_data["chat_id"],
            chat_name=message_data.get("sender_name"),
            customer_data=message_data.get("customer_data")
        )
        
        await db.save_message(
            telegram_account_id=message_data["account_id"],
            chat_id=message_data["chat_id"],
            message_id=message_data["message_id"],
            direction="incoming",
            text=message_data["text"],
            status="received"
        )
        
        
        # --- 2. Prepare Payload (The "Translation" Step) ---
        # We convert the Telegram data into the structure your Boss provided
        
        payload = {
            "dataType": "message",
            # The 'sessionId' is your Agent/Account ID
            "sessionId": message_data["account_id"], 
            "data": {
                "message": {
                    "_data": {
                        # 'body' is the message text
                        "body": message_data["text"],
                        # 'from' is the sender's ID (Telegram User ID)
                        "from": str(message_data["chat_id"]),
                        # 'notifyName' is the sender's display name
                        "notifyName": message_data.get("sender_name", "Unknown"),
                        "type": "chat",
                        "t": int(time.time()),  # Current timestamp
                        "id": {
                            "fromMe": False,
                            "id": str(message_data["message_id"]),
                            "remote": str(message_data["chat_id"])
                        },
                        "phone":str(message_data["customer_data"]["phone"])
                    }
                }
            }
        }

        # --- 3. Forward the CONVERTED payload ---
        await forward_to_main_service(payload)

    except Exception as e:
        logger.error(f"Error handling incoming message: {e}", exc_info=True)