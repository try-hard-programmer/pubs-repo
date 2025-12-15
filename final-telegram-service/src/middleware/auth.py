"""Internal Service Authentication Middleware."""
from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from src.config.config import config
import logging

logger = logging.getLogger(__name__)

# Define the header key (e.g., 'X-Service-Token' or 'Authorization')
# Using 'X-Service-Key' to match "send to this service header"
api_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)

async def verify_secret_key(
    request: Request, 
    api_key: str = Security(api_key_header)
):
    """
    Verifies that the incoming request has the correct internal secret key.
    """
    if not api_key:
        # Fallback: Check 'Authorization' header if X-Service-Key is missing
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            api_key = auth.split(" ")[1]
    
    if api_key != config.TELEGRAM_SECRET_KEY_SERVICE:
        logger.warning(f"⛔ Unauthorized access attempt from {request.client.host}")
        raise HTTPException(
            status_code=403,
            detail="Could not validate credentials"
        )
    
    return True