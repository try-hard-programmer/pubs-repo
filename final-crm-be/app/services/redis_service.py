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
    
    Args:
        lock_name: The unique key for the lock.
        expire: How long (seconds) to hold the lock before auto-releasing (Safety net).
        wait_time: How long (seconds) to wait/spin for the lock before giving up.
    """
    client = get_redis()
    # redis-py's lock() handles the spinning/backoff logic efficiently
    lock = client.lock(f"lock:{lock_name}", timeout=expire, blocking_timeout=wait_time)
    
    acquired = False
    try:
        # Attempt to acquire. This BLOCKS up to `wait_time` seconds.
        acquired = await lock.acquire()
        yield acquired
    except redis_exceptions.LockError:
        # Catch the correct exception class if acquisition fails
        yield False
    except Exception as e:
        logger.error(f"Redis Lock Error: {e}")
        yield False
    finally:
        if acquired:
            try:
                # Only release if we actually own it
                await lock.release()
            except redis_exceptions.LockError:
                # Lock might have expired already; ignore
                pass