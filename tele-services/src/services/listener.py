import logging
import os
import httpx
import base64
from telethon import TelegramClient, events


logger = logging.getLogger(__name__)

# 1. LOAD CONFIG
RAW_URL = os.getenv("MAIN_SERVICE_URL", "http://localhost:8000/webhook/telegram-userbot")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET_KEY", "your_secret_key")

def get_target_url():
    """
    Best Practice: Dynamically resolve the correct endpoint.
    This fixes the URL even if .env points to the wrong webhook path.
    """
    base_url = RAW_URL.rstrip("/")
    
    # If the .env points to a specific webhook (like /webhook/telegram), 
    # strip it back to the API root.
    if "/webhook" in base_url:
        base_url = base_url.split("/webhook")[0]
        
    # Append the specific endpoint that accepts "Boss Format" JSON
    return f"{base_url}/webhook/telegram-userbot"

async def start_listening(client: TelegramClient, agent_id: str):
    """
    Listens for new Telegram messages, converts them to 'WhatsApp Unofficial' JSON,
    and forwards them to the Main Service.
    """
    
    TARGET_URL = get_target_url()
    logger.info(f"🎯 Worker will forward messages to: {TARGET_URL}")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            # --- 1. GROUP FILTER: Ignore if group and NOT mentioned ---
            # event.is_group checks if it's a Chat or Channel
            # event.mentioned checks if the authenticated user (bot) was tagged
            if event.is_group and not event.message.mentioned:
                return

            # 2. EXTRACT DATA
            sender = await event.get_sender()
            sender_id = str(sender.id)
            
            # [FIX] Routing ID: Use Chat ID (Group ID) if group, else Sender ID
            # In Telethon, event.chat_id is the Group ID for groups, and User ID for DMs.
            # This ensures the CRM creates the "Customer" as the Group, not the individual.
            routing_id = str(event.chat_id)
            
            # Name Extraction
            first = getattr(sender, 'first_name', '') or ''
            last = getattr(sender, 'last_name', '') or ''
            notify_name = f"{first} {last}".strip() or getattr(sender, 'username', 'Unknown')

            # 3. PREPARE PAYLOAD (Text vs Media)
            if event.message.media:
                # --- MEDIA HANDLING ---
                logger.info(f"📥 Downloading media from {sender_id} (Chat: {routing_id})...")
                
                # Download to memory (bytes)
                media_bytes = await event.message.download_media(file=bytes)
                b64_data = base64.b64encode(media_bytes).decode('utf-8')
                
                # Determine Mime Type
                mime_type = "application/octet-stream"
                if hasattr(event.message, 'file') and event.message.file:
                    mime_type = event.message.file.mime_type
                
                payload = {
                    "dataType": "media",
                    "sessionId": agent_id,
                    "data": {
                        "messageMedia": {
                            "type": mime_type.split('/')[0], # e.g. "image"
                            "mimetype": mime_type,
                            "data": b64_data,
                            "caption": event.message.text or "",
                            "notifyName": notify_name,
                            "t": int(event.date.timestamp())
                        },
                        "message": { 
                            "_data": { # Helper for routing
                                "from": routing_id,  # <--- [FIX] Used routing_id (Chat ID)
                                "to": agent_id,
                                "author": sender_id, # <--- [FIX] Added original sender for reference
                                "id": { "id": str(event.message.id) }
                            }
                        }
                    }
                }
            else:
                # --- TEXT HANDLING ---
                payload = {
                    "dataType": "message",
                    "sessionId": agent_id, 
                    "data": {
                        "message": {
                            "_data": {
                                "body": event.message.message,
                                "type": "chat", 
                                "from": routing_id, # <--- [FIX] Used routing_id (Chat ID)
                                "to": agent_id,
                                "author": sender_id, # <--- [FIX] Added original sender for reference
                                "notifyName": notify_name,
                                "id": {
                                    "fromMe": False,
                                    "id": str(event.message.id),
                                    "_serialized": f"{event.message.id}_{routing_id}"
                                },
                                "t": int(event.date.timestamp())
                            }
                        }
                    }
                }

            # 4. SEND TO MAIN SERVICE
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": WEBHOOK_SECRET
            }

            async with httpx.AsyncClient(timeout=30.0) as http:
                response = await http.post(TARGET_URL, json=payload, headers=headers)
                
                if response.status_code == 200:
                    logger.info(f"✅ Forwarded msg {event.message.id} from {routing_id}")
                else:
                    logger.error(f"❌ Forward failed ({response.status_code}): {response.text}")

        except Exception as e:
            logger.error(f"⚠️ Listener Error: {e}")

    logger.info(f"🎧 Listener active for Agent: {agent_id}")

