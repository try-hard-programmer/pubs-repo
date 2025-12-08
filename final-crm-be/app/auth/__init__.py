"""
Authentication Module

Provides JWT authentication and authorization functionality using Supabase.
"""
from app.auth.jwt_handler import decode_jwt_token, verify_jwt_token
from app.auth.dependencies import get_current_user, get_optional_user

__all__ = [
    "decode_jwt_token",
    "verify_jwt_token",
    "get_current_user",
    "get_optional_user",
]
