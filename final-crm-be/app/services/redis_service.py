import logging
import redis.asyncio as redis
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# [FIX] Import the centralized settings
from app.config.settings import settings

logger = logging.getLogger(__name__)

# Global Connection Pool
# We initialize this at module level, but it uses values from settings which are loaded at startup.
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
async def acquire_lock(lock_name: str, expire: int = 5) -> AsyncGenerator[bool, None]:
    """
    Distributed Lock using Redis SET NX.
    """
    client = get_redis()
    have_lock = False
    lock_key = f"lock:{lock_name}"
    
    try:
        have_lock = await client.set(lock_key, "locked", ex=expire, nx=True)
        yield have_lock
    finally:
        if have_lock:
            await client.delete(lock_key)