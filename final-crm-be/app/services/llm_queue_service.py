"""
LLM Queue Service
Handles batching of AI requests to control rate limits and concurrency.
"""
import asyncio
import time
import logging
from typing import List, Dict, Any

# Only import what is strictly used
from app.services.dynamic_ai_service_v2 import process_dynamic_ai_response_v2

logger = logging.getLogger(__name__)

class LLMQueueService:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self.batch_interval = 2.0  # Seconds
        self.processing_timeout = 30.0 # Seconds

    async def enqueue(self, chat_id: str, message_id: str, supabase_client: Any, priority: str = "medium"):
        """
        Add a request to the processing queue.
        """
        request_item = {
            "chat_id": chat_id,
            "message_id": message_id,
            "supabase": supabase_client,
            "priority": priority,
            "timestamp": time.time(),
        }
        await self.queue.put(request_item)
        logger.info(f"📥 Queued: Chat {chat_id} | Msg {message_id} | QSize: {self.queue.qsize()}")

    async def start_worker(self):
        """
        Background worker that processes the queue in batches.
        """
        self.is_running = True
        logger.info("🚀 LLM Batch Worker Started")
        
        while self.is_running:
            try:
                # 1. Wait for window time (Buffer mechanism)
                await asyncio.sleep(self.batch_interval)

                # 2. Drain the queue
                batch_items = []
                while not self.queue.empty():
                    try:
                        item = self.queue.get_nowait()
                        batch_items.append(item)
                    except asyncio.QueueEmpty:
                        break
                
                if not batch_items:
                    continue

                logger.info(f"⚡ Processing Batch: {len(batch_items)} items")

                # 3. Process Batch concurrently
                asyncio.create_task(self._process_batch(batch_items))
                
            except Exception as e:
                logger.error(f"🔥 Worker Crash: {e}")
                await asyncio.sleep(1)

    async def _process_batch(self, items: List[Dict]):
        """
        Execute AI logic in parallel using asyncio.gather.
        """
        tasks = []
        now = time.time()
        valid_items = []

        for item in items:
            # 4. Timeout Handling
            if now - item["timestamp"] > self.processing_timeout:
                logger.error(f"⏰ Timeout Dropped: Chat {item['chat_id']} (Age: {now - item['timestamp']:.2f}s)")
                continue

            valid_items.append(item)
            tasks.append(
                process_dynamic_ai_response_v2(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    supabase=item["supabase"],
                    priority=item["priority"]
                )
            )

        if tasks:
            # Execute all tasks concurrently (Promise.all equivalence)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 5. Log results
            for i, res in enumerate(results):
                chat_id = valid_items[i]['chat_id']
                if isinstance(res, Exception):
                    logger.error(f"❌ Batch Item Failed [Chat {chat_id}]: {res}")
                else:
                    logger.info(f"✅ Batch Item Done [Chat {chat_id}]")

# Singleton Instance
llm_queue_service = LLMQueueService()

def get_llm_queue():
    return llm_queue_service