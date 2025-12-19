import logging
import os
import httpx
from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

# 1. LOAD CONFIG
# We get the raw URL from env, e.g., "http://localhost:8000/api/webhook/telegram"
RAW_URL = os.getenv("MAIN_SERVICE_URL", "http://localhost:8000")
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
    
    # Resolve the URL once at startup
    TARGET_URL = get_target_url()
    logger.info(f"🎯 Worker will forward messages to: {TARGET_URL}")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            # 2. EXTRACT DATA
            sender = await event.get_sender()
            chat = await event.get_chat()
            
            sender_id = str(sender.id)
            
            # Smart Name Extraction
            first = getattr(sender, 'first_name', '') or ''
            last = getattr(sender, 'last_name', '') or ''
            notify_name = f"{first} {last}".strip()
            if not notify_name:
                notify_name = getattr(sender, 'username', 'Unknown')

            # 3. CONSTRUCT "BOSS FORMAT" JSON
            # Matches app.models.webhook.WhatsAppUnofficialWebhookMessage
            payload = {
                "dataType": "message",
                "sessionId": agent_id, 
                "data": {
                    "message": {
                        "_data": {
                            "body": event.message.message,
                            "type": "chat", 
                            "from": sender_id, 
                            "to": agent_id,
                            "notifyName": notify_name,
                            "id": {
                                "fromMe": False,
                                "remote": sender_id,
                                "id": str(event.message.id),
                                "_serialized": f"{event.message.id}_{sender_id}"
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

            async with httpx.AsyncClient() as http:
                response = await http.post(TARGET_URL, json=payload, headers=headers)
                
                if response.status_code == 200:
                    logger.info(f"✅ Forwarded msg {event.message.id} from {sender_id}")
                else:
                    logger.error(f"❌ Forward failed ({response.status_code}): {response.text}")

        except Exception as e:
            logger.error(f"⚠️ Listener Error: {e}")

    logger.info(f"🎧 Listener active for Agent: {agent_id}")