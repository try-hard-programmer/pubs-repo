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
        # --- 1. Save to Local DB ---
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
        
        # [FIX] Determine Author vs Chat
        # If message_data has a specific 'sender_id', use it as author.
        # Otherwise, fall back to 'chat_id' (typical for DMs).
        chat_id = str(message_data["chat_id"])
        sender_id = str(message_data.get("sender_id", chat_id))

        payload = {
            "dataType": "message",
            "sessionId": message_data["account_id"], 
            "data": {
                "message": {
                    "_data": {
                        "body": message_data["text"],
                        
                        # [FIX] 'from' is the Chat/Group ID (The "Customer" entity)
                        "from": chat_id,
                        
                        # [FIX] 'author' is the specific User ID (Who said it)
                        "author": sender_id,
                        
                        "notifyName": message_data.get("sender_name", "Unknown"),
                        "type": "chat",
                        "t": int(time.time()),
                        "id": {
                            "fromMe": False,
                            "id": str(message_data["message_id"]),
                            "remote": chat_id
                        },
                        "phone": str(message_data.get("customer_data", {}).get("phone", ""))
                    }
                }
            }
        }

        # --- 3. Forward the CONVERTED payload ---
        await forward_to_main_service(payload)

    except Exception as e:
        logger.error(f"Error handling incoming message: {e}", exc_info=True)

        