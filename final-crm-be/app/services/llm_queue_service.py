"""
LLM Queue Service (Dynamic Per-Chat Workers)
Architecture: One Async Task per Active Chat backed by Redis.
Prevents race conditions and "Double Posting" by debouncing user input.
Includes Supervisor for crash recovery.
"""
import asyncio
import json
import time
import logging
from typing import Any

# Import the centralized Redis service
from app.services.redis_service import get_redis

# Import the AI Processor
from app.services.dynamic_ai_service_v2 import process_dynamic_ai_response_v2

# Import configuration for creating fresh Supabase clients
from app.config.settings import settings
from supabase import create_client

logger = logging.getLogger(__name__)

class LLMQueueService:
    def __init__(self):
        # CONFIG: How long to wait for "silence" before replying (Debounce)
        self.debounce_window = 5.0 
        self.redis = get_redis()
        self.is_running = True # Flag to control supervisor loop

    async def start_worker(self):
        """
        [NEW] Supervisor Loop & Crash Recovery.
        Called by main.py on startup.
        1. Scans Redis for 'orphaned' chats (pending tasks from before restart).
        2. Spawns workers for them.
        3. Idles to keep the background task alive.
        """
        logger.info("🚀 LLM Queue Supervisor: Starting & Scanning for orphans...")
        
        try:
            # RECOVERY: Find chats that have a pending context but no active worker
            # (This happens if the server crashed while a user was waiting)
            async for key in self.redis.scan_iter(match="queue:ctx:*"):
                # key format: "queue:ctx:{chat_id}"
                chat_id = key.split(":")[-1]
                
                # Check if a worker is already active (unlikely on fresh boot, but good check)
                worker_key = f"worker:active:{chat_id}"
                is_active = await self.redis.get(worker_key)
                
                if not is_active:
                    logger.info(f"❤️‍🩹 Recovering orphaned chat session: {chat_id}")
                    # Respawn the worker
                    asyncio.create_task(self._chat_worker_lifecycle(chat_id))
                    
        except Exception as e:
            logger.error(f"⚠️ Queue Recovery Warning: {e}")

        logger.info("✅ LLM Queue Supervisor: Running")

        # Keep the task alive (as expected by main.py)
        while self.is_running:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                logger.info("🛑 LLM Queue Supervisor Stopping...")
                break

    async def enqueue(self, chat_id: str, message_id: str, supabase_client: Any, priority: str = "medium"):
        """
        Add a request to the Redis queue.
        1. Updates the 'Target Execution Time' for this chat in Redis.
        2. Spawns a dedicated background worker if one isn't already running.
        
        Args:
            chat_id: The chat session UUID.
            message_id: The latest message UUID (so AI replies to the newest context).
            supabase_client: Ignored here (we create a fresh one in the worker).
            priority: Task priority.
        """
        # Calculate when the AI should trigger (Now + 10s)
        target_time = time.time() + self.debounce_window
        
        # 1. Store State in Redis (Persistent Atomic Storage)
        # We use HSET so we can update the 'msg_id' and 'run_at' atomically.
        # This effectively "resets the timer" for the worker.
        data = {
            "run_at": target_time,
            "msg_id": message_id,
            "priority": priority
        }
        await self.redis.hset(f"queue:ctx:{chat_id}", mapping=data)
        
        # 2. Check if a worker is ALREADY alive for this chat
        # We check a simple flag key.
        worker_key = f"worker:active:{chat_id}"
        is_active = await self.redis.get(worker_key)
        
        if not is_active:
            # 3. SPAWN THE WORKER (Fire and Forget)
            # This creates a background task that runs independently of this request.
            logger.info(f"🚀 Spawning New Worker for Chat {chat_id}")
            asyncio.create_task(self._chat_worker_lifecycle(chat_id))
        else:
            # If active, the worker will automatically see the updated 'run_at' time 
            # in the next loop cycle and sleep longer.
            logger.info(f"🔄 Worker exists for {chat_id}. Extended timer to {self.debounce_window}s.")

    async def _chat_worker_lifecycle(self, chat_id: str):
        """
        The Dedicated Worker Lifecycle.
        Cycle: Check Redis Time -> Sleep Difference -> Execute -> Terminate.
        """
        worker_key = f"worker:active:{chat_id}"
        context_key = f"queue:ctx:{chat_id}"
        
        # Mark worker as active (Expires in 60s as a safety net against zombies)
        await self.redis.setex(worker_key, 60, "1")

        try:
            while True:
                # 1. Fetch the latest Context & Target Time
                ctx = await self.redis.hgetall(context_key)
                if not ctx:
                    logger.debug(f"Context empty/finished for {chat_id}, worker exiting.")
                    break
                
                run_at = float(ctx["run_at"])
                now = time.time()
                remaining = run_at - now

                # 2. THE OPTIMIZATION: "Calculated Sleep"
                # If we still have time to wait, we sleep exactly that amount (capped).
                if remaining > 0.1:
                    # Sleep, but cap at 5s to allow for heartbeat/shutdown checks
                    sleep_duration = min(remaining, 5.0) 
                    await asyncio.sleep(sleep_duration)
                    
                    # Heartbeat: Keep the worker key alive while we wait
                    await self.redis.expire(worker_key, 60)
                    continue
                
                # 3. TIME IS UP! EXECUTE AI.
                logger.info(f"⚡ Timer Finished for Chat {chat_id}. Executing AI.")
                
                # Cleanup Redis State BEFORE execution.
                # This ensures that if the user types *while* the AI is generating,
                # enqueue() will see no active worker and spawn a NEW one for the next turn.
                await self.redis.delete(context_key)
                await self.redis.delete(worker_key)
                
                # Run the Heavy AI Logic
                await self._execute_ai_logic(chat_id, ctx)
                
                # 4. TERMINATE
                logger.info(f"💀 Worker for {chat_id} terminating gracefully.")
                break

        except Exception as e:
            logger.error(f"🔥 Worker Crash [{chat_id}]: {e}")
            # Ensure cleanup so a new worker can spawn later
            await self.redis.delete(worker_key)

    async def _execute_ai_logic(self, chat_id: str, ctx: dict):
        """Wrapper to safely run the AI service with a fresh DB connection"""
        try:
            # Create FRESH Supabase client for the background task.
            # We cannot reuse the webhook's client because that request context is closed.
            supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
            
            await process_dynamic_ai_response_v2(
                chat_id=chat_id,
                msg_id=ctx["msg_id"],
                supabase=supabase,
                priority=ctx["priority"]
            )
        except Exception as e:
            logger.error(f"❌ AI Execution Failed [{chat_id}]: {e}")

# Singleton Instance
llm_queue_service = LLMQueueService()

def get_llm_queue():
    return llm_queue_service