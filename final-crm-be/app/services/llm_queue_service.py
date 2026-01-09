"""
LLM Queue Service (Debounce/Stacking Edition)
Prevents "Double Posting" by waiting for the user to stop typing.
"""
import asyncio
import time
import logging
from typing import Dict, Any

from app.services.dynamic_ai_service_v2 import process_dynamic_ai_response_v2

logger = logging.getLogger(__name__)

class LLMQueueService:
    def __init__(self):
        # The "Stack": Stores the LATEST pending task for each chat
        # Key: chat_id
        # Value: { "msg_id": str, "supabase": obj, "priority": str, "last_activity": float }
        self.pending_chats: Dict[str, Any] = {}
        self.is_running = False
        
        # CONFIG: How long to wait for "silence" before replying
        # If user types again within 10 seconds, we reset the timer (Stacking)
        self.debounce_window = 10.0 

    async def enqueue(self, chat_id: str, message_id: str, supabase_client: Any, priority: str = "medium"):
        """
        Add a request to the stack.
        If a user types again, this OVERWRITES the previous trigger and RESETS the timer.
        """
        now = time.time()
        
        if chat_id in self.pending_chats:
            logger.info(f"🔄 Stacking: Resetting timer for Chat {chat_id} (New Msg: {message_id})")
        else:
            logger.info(f"📥 New Stack: Chat {chat_id} | Waiting {self.debounce_window}s for silence...")

        # Store ONLY the latest state (The AI will read the full history anyway)
        self.pending_chats[chat_id] = {
            "msg_id": message_id, # We only need the latest ID to wake up the AI
            "supabase": supabase_client,
            "priority": priority,
            "last_activity": now
        }

    async def start_worker(self):
        """
        Background worker that watches the stack.
        """
        self.is_running = True
        logger.info("🚀 LLM Stacking/Debounce Worker Started")
        
        while self.is_running:
            try:
                await asyncio.sleep(1.0) # Check stack every second
                
                now = time.time()
                # Find chats that are "Ready" (Silent > debounce_window)
                ready_chat_ids = []
                
                # Snapshot keys to avoid runtime modification errors
                active_chats = list(self.pending_chats.keys())

                for chat_id in active_chats:
                    data = self.pending_chats[chat_id]
                    elapsed = now - data["last_activity"]

                    if elapsed >= self.debounce_window:
                        ready_chat_ids.append(chat_id)

                # Process Ready Chats
                for chat_id in ready_chat_ids:
                    # Pop the data from stack
                    item = self.pending_chats.pop(chat_id, None)
                    if item:
                        logger.info(f"⚡ Stack Released ({self.debounce_window}s silence). Triggering AI for Chat {chat_id}")
                        asyncio.create_task(
                            self._process_safe(chat_id, item)
                        )

            except Exception as e:
                logger.error(f"🔥 Worker Crash: {e}")
                await asyncio.sleep(1)

    async def _process_safe(self, chat_id, item):
        """Wrapper to catch errors without killing the worker"""
        try:
            await process_dynamic_ai_response_v2(
                chat_id=chat_id,
                msg_id=item["msg_id"], # Matches dynamic_ai_service_v2 definition
                supabase=item["supabase"],
                priority=item["priority"]
            )
        except Exception as e:
            logger.error(f"❌ AI Task Failed [Chat {chat_id}]: {e}")

# Singleton Instance
llm_queue_service = LLMQueueService()

def get_llm_queue():
    return llm_queue_service