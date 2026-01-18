"""Telegram Client Manager."""
import logging
import base64
import json
from typing import Dict, Optional, Callable
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Configure Logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class TelegramClientManager:
    """Manages active Telethon sessions."""
    
    def __init__(self):
        self.clients: Dict[str, TelegramClient] = {}
        self.message_handlers: list = []
    
    def register_message_handler(self, handler: Callable):
        self.message_handlers.append(handler)
        logger.info(f"✅ Handler Registered: {handler.__name__}")
        
    async def add_client(self, account_id: str, api_id: int, api_hash: str, session_string: str):
        if account_id in self.clients: 
            logger.info(f"ℹ️ Client {account_id} already active. Skipping add.")
            return
            
        logger.info(f"🔄 Starting Client: {account_id}...")
        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            
            @client.on(events.NewMessage(incoming=True))
            async def handler(event):
                await self._process_update(account_id, event)
                
            await client.connect()
            
            if not await client.is_user_authorized():
                logger.warning(f"⚠️ Client {account_id} NOT authorized. Disconnecting.")
                await client.disconnect()
                return
                
            self.clients[account_id] = client
            logger.info(f"✅ Client STARTED & Connected: {account_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to start client {account_id}: {e}", exc_info=True)
            
    async def remove_client(self, account_id: str):
        logger.info(f"🛑 Removing client: {account_id}")
        client = self.clients.pop(account_id, None)
        if client: await client.disconnect()

    async def disconnect_all(self):
        logger.info("🛑 Disconnecting ALL clients...")
        for client in self.clients.values(): 
            await client.disconnect()

    async def send_message(self, account_id: str, chat_id: str, text: str, media_url: Optional[str] = None):
        logger.info(f"📨 [OUTBOUND] Request: Account={account_id}, Chat={chat_id}, Media={media_url}")
        
        client = self.clients.get(account_id)
        if not client:
            logger.error(f"❌ Client NOT FOUND for ID: {account_id}")
            return None 
        
        if not client.is_connected():
             logger.warning(f"⚠️ Client found but DISCONNECTED: {account_id}. Reconnecting...")
             try: await client.connect()
             except Exception as e: 
                 logger.error(f"❌ Reconnection failed: {e}")
                 return None

        try:
            try: entity = int(chat_id)
            except: entity = chat_id
            
            msg = None
            if media_url:
                logger.info(f"   📤 Uploading & Sending Media to {entity}...")
                msg = await client.send_file(entity, file=media_url, caption=text or "")
            else:
                if not text: 
                    logger.warning("   ⚠️ Text is empty and no media. Skipping.")
                    return None
                logger.info(f"   📤 Sending Text to {entity}...")
                msg = await client.send_message(entity, text)
            
            logger.info(f"   ✅ Sent! MsgID: {msg.id} | ChatID: {msg.chat_id}")
            return msg.id, msg.chat_id
            
        except Exception as e:
            logger.error(f"❌ Telethon Send Error: {e}", exc_info=True)
            raise e
               
    async def _process_update(self, account_id: str, event):
        """
        Normalize Telegram event and DELEGATE to handlers (messaging.py).
        Added Rule: Ignore Group messages unless mentioned.
        """
        try:
            sender = await event.get_sender()
            
            # Context Flags
            is_group = event.is_group
            is_mentioned = event.message.mentioned # [FIX] Capture this explicitly
            
            # Filter: Ignore Groups if not mentioned
            if is_group and not is_mentioned:
                return

            chat_id = str(event.chat_id)
            sender_id = str(sender.id) if sender else chat_id
            
            logger.info(f"📥 [INBOUND] New Message on Account {account_id}")
            logger.info(f"   📍 Context: IsGroup={is_group} | Mentioned={is_mentioned}")
            
            # [FIX] Add 'mentioned' to the data packet
            message_data = {
                "account_id": account_id,
                "chat_id": chat_id,           
                "sender_id": sender_id,       
                "message_id": str(event.message.id),
                "text": event.message.text or "",
                "sender_name": getattr(sender, 'first_name', "Unknown"),
                "is_group": is_group,
                "mentioned": is_mentioned, # <--- CRITICAL ADDITION
                "timestamp": int(event.date.timestamp()),
                "customer_data": {
                    "phone": getattr(sender, 'phone', None),
                    "username": getattr(sender, 'username', "")
                }
            }
            
            if event.message.media:
                message_data["has_media"] = True
            
            if self.message_handlers:
                for handler in self.message_handlers:
                    try:
                        await handler(message_data)
                    except Exception as e:
                        logger.error(f"   ❌ Handler failed: {e}", exc_info=True)
            else:
                logger.warning("   ⚠️ No handlers registered! Message was dropped.")

        except Exception as e:
            logger.error(f"❌ Error processing update: {e}", exc_info=True)

# Singleton
telegram_manager = TelegramClientManager()