"""
Webhook Authentication Middleware
Validates webhook requests using API Key/Secret authentication
"""
from fastapi import Request, HTTPException, status, Header
import logging
import os

logger = logging.getLogger(__name__)


async def validate_webhook_secret(request: Request) -> bool:
    """
    Validate webhook request using secret key from header.

    Checks X-API-Key header against WEBHOOK_SECRET_KEY environment variable.

    Args:
        request: FastAPI Request object

    Returns:
        True if valid, raises HTTPException if invalid

    Raises:
        HTTPException: 401 if secret is missing or invalid
    """
    # Get secret from header
    secret_from_header = request.headers.get("X-API-Key")

    # Get expected secret from environment
    expected_secret = os.getenv("WEBHOOK_SECRET_KEY")

    # Check if webhook secret is configured
    if not expected_secret:
        logger.error("WEBHOOK_SECRET_KEY environment variable is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook authentication is not properly configured"
        )

    # Check if secret was provided in request
    if not secret_from_header:
        logger.warning(f"Webhook request from {request.client.host} missing X-API-Key header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header"
        )

    # Validate secret
    if secret_from_header != expected_secret:
        logger.warning(
            f"Invalid webhook secret from {request.client.host}. "
            f"Provided: {secret_from_header[:10]}..."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret key"
        )

    logger.debug(f"✅ Webhook request authenticated from {request.client.host}")
    return True


def get_webhook_secret(x_api_key: str = Header(..., alias="X-API-Key", description="API Key for webhook authentication")) -> str:
    """
    Dependency function to get and validate webhook secret from X-API-Key header.

    Usage in FastAPI endpoints:
        @router.post("/webhook/...")
        async def webhook_endpoint(
            secret: str = Depends(get_webhook_secret),
            ...
        ):

    Args:
        x_api_key: API key from X-API-Key header (injected by FastAPI)

    Returns:
        Validated API key string

    Raises:
        HTTPException: If API key is missing or invalid
    """
    # Get expected secret from environment
    expected_secret = os.getenv("WEBHOOK_SECRET_KEY")

    # Check if webhook secret is configured
    if not expected_secret:
        logger.error("WEBHOOK_SECRET_KEY environment variable is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook authentication is not properly configured"
        )

    # Check if secret was provided
    if not x_api_key:
        logger.warning("Webhook request missing X-API-Key header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header"
        )

    # Validate secret
    if x_api_key != expected_secret:
        logger.warning(f"Invalid webhook secret. Provided: {x_api_key[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret key"
        )

    logger.debug("✅ Webhook request authenticated")
    return x_api_key
