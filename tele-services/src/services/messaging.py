"""Messaging Service."""
import logging
import time
from src.database import db
from src.services.webhook_sender import forward_to_main_service

logger = logging.getLogger(__name__)

async def handle_incoming_message(message_data: dict) -> None:
    """
    1. Save to SQLite (Local History)
    2. Convert to "WhatsApp-Style" Format (Media or Text)
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
            text=message_data["text"], # For media, this might be the caption
            status="received"
        )
        
        # --- 2. Prepare Payload (Translation Step) ---
        
        chat_id = str(message_data["chat_id"])
        sender_id = str(message_data.get("sender_id", chat_id))
        is_group = message_data.get("is_group", False)
        is_mentioned = message_data.get("mentioned", False)

        # Common Routing Data (Used in both Text and Media)
        routing_data = {
            "body": message_data.get("text", ""),
            "from": sender_id, 
            "to": chat_id,     
            "author": sender_id,
            "is_group": is_group,
            "mentioned": is_mentioned, 
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

        # --- 3. Construct Correct Payload Type ---
        
        if message_data.get("has_media") and message_data.get("media_b64"):
            # [CASE A] MEDIA PAYLOAD
            logger.info(f"📸 Forwarding MEDIA Payload: Group={is_group} | Mentioned={is_mentioned}")
            
            payload = {
                "dataType": "media", # <--- CRITICAL: Tells Backend this is an image
                "sessionId": message_data["account_id"], 
                "data": {
                    "messageMedia": {
                        "type": message_data.get("mime_type", "image/jpeg").split('/')[0], 
                        "mimetype": message_data.get("mime_type", "image/jpeg"),
                        "data": message_data["media_b64"],
                        "filename": f"file_{message_data['message_id']}",
                        "caption": message_data.get("text", ""),
                        "notifyName": message_data.get("sender_name", "Unknown"),
                        "t": message_data.get("timestamp", int(time.time()))
                    },
                    "message": {
                        "_data": routing_data # Pass routing data here too
                    }
                }
            }
        else:
            # [CASE B] TEXT PAYLOAD
            logger.info(f"💬 Forwarding TEXT Payload: Group={is_group} | Mentioned={is_mentioned}")
            
            payload = {
                "dataType": "message",
                "sessionId": message_data["account_id"], 
                "data": {
                    "message": {
                        "_data": routing_data
                    }
                }
            }

        # --- 4. Forward ---
        await forward_to_main_service(payload)

    except Exception as e:
        logger.error(f"Error handling incoming message: {e}", exc_info=True)