"""Telegram Client Manager."""
import logging
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
        """Register function to call on new messages."""
        self.message_handlers.append(handler)
        
    async def add_client(self, account_id: str, api_id: int, api_hash: str, session_string: str):
        if account_id in self.clients:
            return

        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            
            # 1. Attach listeners BEFORE connecting
            @client.on(events.NewMessage(incoming=True))
            async def handler(event):
                await self._process_update(account_id, event)
            
            # 2. [CRITICAL CHANGE] Use connect() instead of start()
            # connect() will NEVER ask for input. It just returns True or False.
            await client.connect()
            
            # 3. Check if we are actually logged in
            if not await client.is_user_authorized():
                logger.error(f"❌ Session for {account_id} is DEAD. Ignoring it.")
                await client.disconnect()
                return # Stop here, don't crash the server

            # 4. Success!
            self.clients[account_id] = client
            logger.info(f"✅ Client started: {account_id}")
            
        except Exception as e:
            logger.error(f"Failed to start client {account_id}: {e}")
            
    async def remove_client(self, account_id: str):
        client = self.clients.pop(account_id, None)
        if client:
            await client.disconnect()

    async def disconnect_all(self):
        for client in self.clients.values():
            await client.disconnect()

    async def send_message(self, account_id: str, chat_id: str, text: str):
        client = self.clients.get(account_id)
        if not client:
            logger.warning(f"⚠️ Cannot send message: Client {account_id} not connected/found.")
            # return None instead of raising immediately to let caller handle it
            return None 
        
        try:
            # 1. Try resolving chat_id to integer (Telegram User IDs are ints)
            entity = int(chat_id)
        except ValueError:
            # 2. If it's a username (string), keep it as string
            entity = chat_id
            
        try:
            logger.info(f"📤 Sending to {entity} via {account_id}...")
            msg = await client.send_message(entity, text)
            return msg.id
        except Exception as e:
            logger.error(f"❌ Telethon Send Error: {e}")
            raise e
        
    async def _process_update(self, account_id: str, event):
        """Normalize Telegram event to dict and call handlers."""
        try:
            sender = await event.get_sender()
            chat_id = str(event.chat_id)
            
            customer_data = {
                "user_id": str(sender.id) if sender else None,
                "first_name": getattr(sender, 'first_name', ""),
                "username": getattr(sender, 'username', ""),
                "phone": getattr(sender, 'phone', None)
            }

            msg_data = {
                "account_id": account_id,
                "chat_id": chat_id,
                "message_id": str(event.message.id),
                "text": event.message.text or "",
                "sender_name": getattr(sender, 'first_name', "Unknown"),
                "customer_data": customer_data
            }

            for handler in self.message_handlers:
                await handler(msg_data)
        except Exception as e:
            logger.error(f"Error processing update: {e}")

# Singleton
telegram_manager = TelegramClientManager()