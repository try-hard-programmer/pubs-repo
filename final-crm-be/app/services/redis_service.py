import logging
import redis.asyncio as redis
# [FIX] Import exceptions from the main redis package, not asyncio
from redis import exceptions as redis_exceptions
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Global Connection Pool
_pool = redis.ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD,
    decode_responses=True,
    max_connections=100
)

def get_redis() -> redis.Redis:
    """Get a Redis client from the pool."""
    return redis.Redis(connection_pool=_pool)

@asynccontextmanager
async def acquire_lock(lock_name: str, expire: int = 60, wait_time: int = 10) -> AsyncGenerator[bool, None]:
    """
    Robust Distributed Lock (Blocking).
    """
    client = get_redis()
    lock = client.lock(f"lock:{lock_name}", timeout=expire, blocking_timeout=wait_time)
    
    acquired = False
    
    # 1. Isolate the acquisition logic. Only catch errors related to getting the lock.
    try:
        acquired = await lock.acquire()
    except Exception as e:
        logger.error(f"Redis Lock Acquisition Error: {e}")
        yield False
        return

    # 2. Yield to the business logic. Let internal exceptions bubble up normally.
    try:
        yield acquired
    finally:
        # 3. Always release if we own it, even if the business logic crashed.
        if acquired:
            try:
                await lock.release()
            except redis_exceptions.LockError:
                pass