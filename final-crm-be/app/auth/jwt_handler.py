"""
JWT Token Handler

Handles JWT token validation and decoding for Supabase authentication.
Uses python-jose for JWT operations.
"""
import logging
from typing import Dict, Any, Optional
from jose import jwt, JWTError
from datetime import datetime

from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


class JWTValidationError(Exception):
    """Custom exception for JWT validation errors"""
    pass


def verify_jwt_token(token: str) -> bool:
    """
    Verify if a JWT token is valid.

    Args:
        token: JWT token string

    Returns:
        True if token is valid, False otherwise
    """
    try:
        decode_jwt_token(token)
        return True
    except JWTValidationError:
        return False


def decode_jwt_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a Supabase JWT token.

    This function validates the JWT token using the Supabase JWT secret
    and returns the decoded payload.

    Args:
        token: JWT token string from Authorization header

    Returns:
        Decoded JWT payload as dictionary

    Raises:
        JWTValidationError: If token is invalid, expired, or malformed
    """
    if not settings.is_supabase_configured:
        logger.error("Supabase JWT configuration is missing")
        raise JWTValidationError("Authentication service is not configured")

    if not token:
        raise JWTValidationError("Token is required")

    try:
        # Decode and verify the JWT token
        # Supabase uses HS256 algorithm by default
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",  # Supabase default audience
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
            }
        )

        logger.debug(f"JWT token decoded successfully for user: {payload.get('sub')}")
        return payload

    except jwt.ExpiredSignatureError:
        logger.warning("JWT token has expired")
        raise JWTValidationError("Token has expired")

    except jwt.JWTClaimsError as e:
        logger.warning(f"JWT claims error: {e}")
        raise JWTValidationError("Invalid token claims")

    except JWTError as e:
        logger.warning(f"JWT validation error: {e}")
        raise JWTValidationError("Invalid token")

    except Exception as e:
        logger.error(f"Unexpected error during JWT decoding: {e}")
        raise JWTValidationError("Token validation failed")


def extract_user_from_token(token: str) -> User:
    """
    Extract user information from JWT token.

    Decodes the JWT token and creates a User object with the claims.

    Args:
        token: JWT token string

    Returns:
        User object populated with token claims

    Raises:
        JWTValidationError: If token is invalid or user data cannot be extracted
    """
    try:
        payload = decode_jwt_token(token)

        # Extract user information from JWT claims
        user_id = payload.get("sub")
        email = payload.get("email")

        if not user_id:
            raise JWTValidationError("User ID (sub) not found in token")

        if not email:
            raise JWTValidationError("Email not found in token")

        # Create User object
        user = User(
            user_id=user_id,
            email=email,
            display_name=payload.get("display_name", ""),
            aud=payload.get("aud"),
            role=payload.get("role"),
            session_id=payload.get("session_id"),
            exp=payload.get("exp"),
            iat=payload.get("iat"),
            user_metadata=payload.get("user_metadata", {}),
            app_metadata=payload.get("app_metadata", {}),
        )

        logger.info(f"User extracted from token: {user.email} ({user.user_id})")
        return user

    except JWTValidationError:
        raise

    except Exception as e:
        logger.error(f"Error extracting user from token: {e}")
        raise JWTValidationError("Failed to extract user information from token")


def get_token_expiration(token: str) -> Optional[datetime]:
    """
    Get the expiration datetime of a JWT token.

    Args:
        token: JWT token string

    Returns:
        Expiration datetime or None if not available
    """
    try:
        payload = decode_jwt_token(token)
        exp_timestamp = payload.get("exp")

        if exp_timestamp:
            return datetime.fromtimestamp(exp_timestamp)

        return None

    except JWTValidationError:
        return None
