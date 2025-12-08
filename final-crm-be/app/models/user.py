"""
User Model for JWT Authentication

Represents user data extracted from Supabase JWT token
"""
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class User(BaseModel):
    """
    User model populated from JWT token claims.

    This model represents the authenticated user based on the decoded JWT token
    from Supabase authentication.
    """

    # Core user fields
    user_id: str = Field(..., description="Unique user identifier (sub claim from JWT)")
    email: str = Field(..., description="User's email address")
    display_name: str = Field(..., description="User's display name")

    # Authentication metadata
    aud: Optional[str] = Field(None, description="Audience claim - typically 'authenticated'")
    role: Optional[str] = Field(None, description="User role from JWT")

    # Session metadata
    session_id: Optional[str] = Field(None, description="Session identifier")

    # Token metadata
    exp: Optional[int] = Field(None, description="Token expiration timestamp")
    iat: Optional[int] = Field(None, description="Token issued at timestamp")

    # Additional user metadata from JWT
    user_metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional user metadata")
    app_metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Application metadata")

    class Config:
        """Pydantic configuration"""
        json_schema_extra = {
            "example": {
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "email": "user@example.com",
                "aud": "authenticated",
                "role": "authenticated",
                "session_id": "session-123",
                "exp": 1735689600,
                "iat": 1735603200,
                "user_metadata": {},
                "app_metadata": {}
            }
        }

    @property
    def is_token_expired(self) -> bool:
        """Check if the token is expired"""
        if not self.exp:
            return True
        return datetime.utcnow().timestamp() > self.exp


class GetMeResponse(BaseModel):
    """
    Response model for /getme endpoint.

    Contains authenticated user information along with their organization details.
    """

    # User information
    user_id: str = Field(..., description="Unique user identifier")
    email: str = Field(..., description="User's email address")
    display_name: str = Field(..., description="User's display name")
    role: Optional[str] = Field(None, description="User role from JWT")

    # User metadata
    user_metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional user metadata")

    # Organization information
    organization_id: Optional[str] = Field(None, description="Organization UUID that user belongs to")
    organization_name: Optional[str] = Field(None, description="Organization name")
    organization_category: Optional[str] = Field(None, description="Organization business category")
    organization_logo_url: Optional[str] = Field(None, description="Organization logo URL")
    is_organization_owner: Optional[bool] = Field(None, description="Whether user is the organization owner")
    joined_organization_at: Optional[datetime] = Field(None, description="When user joined the organization")

    # Token information
    token_expires_at: Optional[datetime] = Field(None, description="Token expiration datetime")
    token_issued_at: Optional[datetime] = Field(None, description="Token issued datetime")

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "email": "user@example.com",
                "display_name": "John Doe",
                "role": "authenticated",
                "user_metadata": {
                    "full_name": "John Doe",
                    "phone": "+628123456789"
                },
                "organization_id": "org-uuid-123",
                "organization_name": "Acme Corporation",
                "organization_category": "technology",
                "organization_logo_url": "https://example.com/logo.png",
                "is_organization_owner": True,
                "joined_organization_at": "2025-10-10T10:00:00Z",
                "token_expires_at": "2025-12-31T23:59:59Z",
                "token_issued_at": "2025-10-29T10:00:00Z"
            }
        }
