"""
FastAPI Authentication Dependencies

Provides FastAPI dependency functions for JWT authentication.
"""
import logging
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.auth.jwt_handler import extract_user_from_token, JWTValidationError
from app.models.user import User

logger = logging.getLogger(__name__)

# HTTP Bearer token security scheme
# IMPORTANT: scheme_name must match the security scheme defined in main.py custom_openapi()
security = HTTPBearer(
    scheme_name="BearerAuth",  # Must match OpenAPI securitySchemes key
    description="Enter your JWT token from authentication",
    auto_error=True
)

optional_security = HTTPBearer(
    scheme_name="BearerAuth",  # Must match for optional endpoints
    description="Enter your JWT token (optional)",
    auto_error=False
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> User:
    """
    FastAPI dependency to get the current authenticated user.

    Extracts the JWT token from the Authorization header, validates it,
    and returns the User object.

    Usage:
        @app.get("/protected")
        async def protected_route(user: User = Depends(get_current_user)):
            return {"message": f"Hello {user.email}"}

    Args:
        credentials: HTTP Authorization credentials (Bearer token)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException: 401 if token is missing or invalid
                      403 if token is expired
    """
    if not credentials:
        logger.warning("Authorization header missing")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        user = extract_user_from_token(token)

        # Optional: Check if token is expired (already checked in decode, but can add custom logic)
        if user.is_token_expired:
            logger.warning(f"Expired token used by user: {user.email}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return user

    except JWTValidationError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    except Exception as e:
        logger.error(f"Unexpected error in get_current_user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication failed",
        )


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security)
) -> Optional[User]:
    """
    FastAPI dependency to optionally get the current authenticated user.

    Similar to get_current_user but doesn't raise an error if no token is provided.
    Useful for endpoints that work both with and without authentication.

    Usage:
        @app.get("/public-or-private")
        async def mixed_route(user: Optional[User] = Depends(get_optional_user)):
            if user:
                return {"message": f"Hello {user.email}"}
            return {"message": "Hello anonymous"}

    Args:
        credentials: Optional HTTP Authorization credentials

    Returns:
        User object if token is valid, None if no token provided

    Raises:
        HTTPException: 401 if token is provided but invalid
    """
    if not credentials:
        return None

    token = credentials.credentials

    try:
        user = extract_user_from_token(token)
        return user

    except JWTValidationError as e:
        logger.warning(f"Optional JWT validation failed: {e}")
        # Still raise error if token is provided but invalid
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    except Exception as e:
        logger.error(f"Unexpected error in get_optional_user: {e}")
        return None


async def require_role(required_role: str):
    """
    FastAPI dependency factory to check if user has required role.

    Usage:
        @app.get("/admin")
        async def admin_route(
            user: User = Depends(get_current_user),
            _: None = Depends(require_role("admin"))
        ):
            return {"message": "Admin only"}

    Args:
        required_role: Role required to access the endpoint

    Returns:
        Dependency function that checks user role

    Raises:
        HTTPException: 403 if user doesn't have required role
    """
    async def check_role(user: User = Depends(get_current_user)) -> None:
        if user.role != required_role:
            logger.warning(
                f"User {user.email} attempted to access resource requiring role '{required_role}' "
                f"but has role '{user.role}'"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role} role",
            )
        return None

    return check_role
