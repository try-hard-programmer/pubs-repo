"""Telegram Client Manager."""
import logging
import base64
import json
from typing import Dict, Optional, Callable
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

class TelegramClientManager:
    """Manages active Telethon sessions."""
    
    def __init__(self):
        self.clients: Dict[str, TelegramClient] = {}
        self.message_handlers: list = []
    
    def register_message_handler(self, handler: Callable):
        self.message_handlers.append(handler)
        
    async def add_client(self, account_id: str, api_id: int, api_hash: str, session_string: str):
        if account_id in self.clients: return
        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            @client.on(events.NewMessage(incoming=True))
            async def handler(event):
                await self._process_update(account_id, event)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return
            self.clients[account_id] = client
            logger.info(f"✅ Client started: {account_id}")
        except Exception as e:
            logger.error(f"Failed to start client {account_id}: {e}")
            
    async def remove_client(self, account_id: str):
        client = self.clients.pop(account_id, None)
        if client: await client.disconnect()

    async def disconnect_all(self):
        for client in self.clients.values(): await client.disconnect()

    async def send_message(self, account_id: str, chat_id: str, text: str, media_url: Optional[str] = None):
        """
        Sends a message with DEBUG logging to find Error 500 cause.
        """
        # DEBUG LOGS START
        logger.info(f"📨 SEND REQUEST: Account={account_id}, Chat={chat_id}, TextLen={len(text) if text else 0}")
        
        client = self.clients.get(account_id)
        if not client:
            logger.error(f"❌ Client NOT FOUND for ID: {account_id}")
            logger.error(f"   Available Clients: {list(self.clients.keys())}")
            return None 
        
        if not client.is_connected():
             logger.error(f"❌ Client found but DISCONNECTED: {account_id}")
             try: await client.connect()
             except: return None

        try:
            try: entity = int(chat_id)
            except: entity = chat_id
            
            if media_url:
                logger.info(f"📤 Sending Media to {entity}...")
                msg = await client.send_file(entity, file=media_url, caption=text or "")
            else:
                if not text: 
                    logger.warning("⚠️ Text is empty and no media. Skipping.")
                    return None
                logger.info(f"📤 Sending Text to {entity}...")
                msg = await client.send_message(entity, text)
            
            logger.info(f"✅ Send Success! ID: {msg.id}")
            return msg.id, msg.chat_id
            
        except Exception as e:
            logger.error(f"❌ Telethon Send Error: {e}")
            # Don't raise, just return None so API handles it gracefully? 
            # Or raise so we see the 500. Let's raise to see the trace.
            raise e
               
    async def _process_update(self, account_id: str, event):
        """
        Normalize Telegram event and SEND DIRECTLY.
        """
        try:
            from src.services.webhook_sender import forward_to_main_service
            
            sender = await event.get_sender()
            chat_id = str(event.chat_id)
            
            # Defaults
            media_payload = {}
            data_type = "message" 
            
            # 1. MEDIA DETECTION
            if event.message.media:
                try:
                    logger.info(f"📥 Downloading media for message {event.message.id}...")
                    file_bytes = await event.message.download_media(file=bytes)
                    
                    if file_bytes:
                        b64_data = base64.b64encode(file_bytes).decode('utf-8')
                        
                        mime_type = "application/octet-stream"
                        if hasattr(event.message.media, 'document'):
                             mime_type = event.message.media.document.mime_type
                        elif hasattr(event.message.media, 'photo'):
                             mime_type = "image/jpeg"
                        
                        msg_type_label = "file"
                        if "image" in mime_type: msg_type_label = "image"
                        elif "video" in mime_type: msg_type_label = "video"
                        elif "audio" in mime_type: msg_type_label = "audio"
                        
                        data_type = "media"
                        media_payload = {
                            "mimetype": mime_type,
                            "data": b64_data, 
                            "caption": event.message.text or "",
                            "type": msg_type_label
                        }
                        logger.info(f"✅ Media processed. Type: {msg_type_label}, Bytes: {len(file_bytes)}")
                except Exception as e:
                    logger.error(f"❌ Media download failed: {e}")
                    data_type = "message"

            # 2. CONSTRUCT PAYLOAD
            identity_data = {
                 "id": {"id": str(event.message.id), "fromMe": event.message.out},
                 "from": str(sender.id) if sender else "Unknown",
                 "to": account_id,
                 "notifyName": getattr(sender, 'first_name', "Unknown"),
                 "body": event.message.text or "",
                 "t": int(event.date.timestamp()),
                 "phone": getattr(sender, 'phone', None)
            }

            if data_type == "media":
                backend_data = {**media_payload, "message": { "_data": identity_data }}
            else:
                backend_data = {"message": { "_data": identity_data }}

            # 3. HYBRID PAYLOAD
            customer_data = {
                "user_id": str(sender.id) if sender else None,
                "first_name": getattr(sender, 'first_name', ""),
                "username": getattr(sender, 'username', ""),
                "phone": getattr(sender, 'phone', None)
            }

            final_payload = {
                "sessionId": account_id,
                "dataType": data_type,
                "data": backend_data,
                "account_id": account_id,
                "chat_id": chat_id,
                "message_id": str(event.message.id),
                "text": event.message.text or "",
                "sender_name": getattr(sender, 'first_name', "Unknown"),
                "customer_data": customer_data
            }

            # Direct Send
            await forward_to_main_service(final_payload)

        except Exception as e:
            logger.error(f"Error processing update: {e}")

# Singleton
telegram_manager = TelegramClientManager()