"""
Organization Models

Pydantic models for business/organization management.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class BusinessCategory(str, Enum):
    """Business category enumeration"""
    TECHNOLOGY = "technology"
    FINANCE = "finance"
    HEALTHCARE = "healthcare"
    EDUCATION = "education"
    RETAIL = "retail"
    MANUFACTURING = "manufacturing"
    CONSULTING = "consulting"
    REAL_ESTATE = "real_estate"
    HOSPITALITY = "hospitality"
    TRANSPORTATION = "transportation"
    MEDIA = "media"
    AGRICULTURE = "agriculture"
    CONSTRUCTION = "construction"
    ENERGY = "energy"
    TELECOMMUNICATIONS = "telecommunications"
    OTHER = "other"


class AppRole(str, Enum):
    """Application role enumeration for organization-scoped permissions"""
    SUPER_ADMIN = "super_admin"  # Organization owner, full control
    ADMIN = "admin"              # Can manage members and content
    MODERATOR = "moderator"      # Can moderate content
    USER = "user"                # Basic access


# Request Models

class OrganizationCreate(BaseModel):
    """Schema for creating a new organization"""
    name: str = Field(..., min_length=1, max_length=255, description="Business name")
    legal_name: Optional[str] = Field(None, description="Legal business name")
    category: BusinessCategory = Field(..., description="Business category")
    description: Optional[str] = Field(None, max_length=500, description="Short business description")
    owner_id: str = Field(..., description="User ID of the organization owner")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Acme Corporation",
                "legal_name": "Acme Corporation Inc.",
                "category": "technology",
                "description": "Leading technology solutions provider",
                "owner_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            }
        }


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    legal_name: Optional[str] = None
    category: Optional[BusinessCategory] = None
    description: Optional[str] = Field(None, max_length=500)
    logo_url: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Acme Corporation Updated",
                "description": "Updated description"
            }
        }


# Response Models

class Organization(BaseModel):
    """Schema for organization response"""
    id: str = Field(..., description="Organization UUID")
    name: str = Field(..., description="Business name")
    legal_name: Optional[str] = Field(None, description="Legal business name")
    category: BusinessCategory = Field(..., description="Business category")
    description: Optional[str] = Field(None, description="Business description")
    logo_url: Optional[str] = Field(None, description="URL to organization logo")
    owner_id: str = Field(..., description="Owner user UUID")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    is_active: bool = Field(True, description="Active status")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "org-uuid-123",
                "name": "Acme Corporation",
                "legal_name": "Acme Corporation Inc.",
                "category": "technology",
                "description": "Leading technology solutions provider",
                "logo_url": "https://example.com/logo.png",
                "owner_id": "user-uuid-456",
                "created_at": "2025-10-10T10:00:00Z",
                "updated_at": "2025-10-10T10:00:00Z",
                "is_active": True,
                "metadata": {}
            }
        }


class OrganizationWithOwnership(Organization):
    """Schema for organization with ownership flag"""
    is_owner: bool = Field(..., description="Whether current user is the owner")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "org-uuid-123",
                "name": "Acme Corporation",
                "legal_name": "Acme Corporation Inc.",
                "category": "technology",
                "description": "Leading technology solutions provider",
                "logo_url": None,
                "owner_id": "user-uuid-456",
                "created_at": "2025-10-10T10:00:00Z",
                "updated_at": "2025-10-10T10:00:00Z",
                "is_active": True,
                "metadata": {},
                "is_owner": True
            }
        }


class OrganizationMember(BaseModel):
    """Schema for organization member"""
    user_id: str = Field(..., description="User UUID")
    organization_id: str = Field(..., description="Organization UUID")
    joined_at: datetime = Field(..., description="Join timestamp")
    is_owner: bool = Field(False, description="Whether member is owner")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "user_id": "user-uuid-123",
                "organization_id": "org-uuid-456",
                "joined_at": "2025-10-10T10:00:00Z",
                "is_owner": False,
                "metadata": {}
            }
        }


class OrganizationMemberDetail(BaseModel):
    """Schema for organization member with user details"""
    user_id: str = Field(..., description="User UUID")
    email: Optional[str] = Field(None, description="User email")
    joined_at: datetime = Field(..., description="Join timestamp")
    is_owner: bool = Field(False, description="Whether member is owner")
    role: Optional[str] = Field(None, description="User role in organization")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "user_id": "user-uuid-123",
                "email": "user@example.com",
                "joined_at": "2025-10-10T10:00:00Z",
                "is_owner": False,
                "role": "user"
            }
        }


# List Response Models

class OrganizationCreateResponse(BaseModel):
    """Response after creating organization"""
    organization_id: str = Field(..., description="Created organization UUID")
    message: str = Field(default="Organization created successfully")

    class Config:
        json_schema_extra = {
            "example": {
                "organization_id": "org-uuid-123",
                "message": "Organization created successfully"
            }
        }


class OrganizationMemberListResponse(BaseModel):
    """Schema for list of organization members"""
    members: List[OrganizationMemberDetail]
    total: int = Field(..., description="Total number of members")
    organization: Organization = Field(..., description="Organization information")

    class Config:
        json_schema_extra = {
            "example": {
                "members": [],
                "total": 5,
                "organization": {}
            }
        }


# User Hierarchy Models (for parent/child relationships)

class UserParent(BaseModel):
    """Schema for user's parent (upline)"""
    parent_id: str = Field(..., description="Parent user UUID")
    email: Optional[str] = Field(None, description="Parent user email")

    class Config:
        json_schema_extra = {
            "example": {
                "parent_id": "parent-uuid-123",
                "email": "parent@example.com"
            }
        }


class UserChild(BaseModel):
    """Schema for user's child (downline)"""
    user_id: str = Field(..., description="Child user UUID")
    email: Optional[str] = Field(None, description="Child user email")
    created_at: datetime = Field(..., description="Relationship creation timestamp")
    role: Optional[str] = Field(None, description="User role")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "user_id": "child-uuid-123",
                "email": "child@example.com",
                "created_at": "2025-10-10T10:00:00Z",
                "role": "user"
            }
        }


class UserChildrenResponse(BaseModel):
    """Response for user's children list"""
    children: List[UserChild]
    total: int = Field(..., description="Total number of children")

    class Config:
        json_schema_extra = {
            "example": {
                "children": [],
                "total": 3
            }
        }


# ============================================================================
# Role Management Models
# ============================================================================

class UserRole(BaseModel):
    """Schema for user role in organization"""
    id: str = Field(..., description="Role UUID")
    user_id: str = Field(..., description="User UUID")
    organization_id: str = Field(..., description="Organization UUID")
    role: AppRole = Field(..., description="Role name")
    assigned_by: Optional[str] = Field(None, description="User who assigned this role")
    created_at: datetime = Field(..., description="Role assignment timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "role-uuid-123",
                "user_id": "user-uuid-456",
                "organization_id": "org-uuid-789",
                "role": "admin",
                "assigned_by": "owner-uuid-000",
                "created_at": "2025-10-10T10:00:00Z",
                "updated_at": "2025-10-10T10:00:00Z"
            }
        }


class RoleAssignRequest(BaseModel):
    """Request to assign role to user in organization"""
    user_id: str = Field(..., description="User UUID to assign role to")
    role: AppRole = Field(..., description="Role to assign")

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "user-uuid-123",
                "role": "admin"
            }
        }


class RoleAssignResponse(BaseModel):
    """Response after role assignment"""
    role_id: str = Field(..., description="Role assignment UUID")
    message: str = Field(default="Role assigned successfully")

    class Config:
        json_schema_extra = {
            "example": {
                "role_id": "role-uuid-123",
                "message": "Role assigned successfully"
            }
        }


class UserRoleInfo(BaseModel):
    """User's role information in organization"""
    user_id: str = Field(..., description="User UUID")
    organization_id: str = Field(..., description="Organization UUID")
    organization_name: str = Field(..., description="Organization name")
    role: AppRole = Field(..., description="User's role")
    is_owner: bool = Field(..., description="Whether user is owner")
    joined_at: datetime = Field(..., description="Join timestamp")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "user_id": "user-uuid-123",
                "organization_id": "org-uuid-456",
                "organization_name": "Acme Corporation",
                "role": "super_admin",
                "is_owner": True,
                "joined_at": "2025-10-10T10:00:00Z"
            }
        }


class UserOrganizationsWithRolesResponse(BaseModel):
    """Response with all user's organizations and their roles"""
    organizations: List[UserRoleInfo]
    total: int = Field(..., description="Total number of organizations")

    class Config:
        json_schema_extra = {
            "example": {
                "organizations": [],
                "total": 2
            }
        }


class OrganizationMemberWithRole(BaseModel):
    """Organization member with role information"""
    user_id: str = Field(..., description="User UUID")
    email: Optional[str] = Field(None, description="User email")
    role: AppRole = Field(..., description="User's role in organization")
    is_owner: bool = Field(False, description="Whether member is owner")
    joined_at: datetime = Field(..., description="Join timestamp")
    assigned_by: Optional[str] = Field(None, description="Who assigned the role")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "user_id": "user-uuid-123",
                "email": "user@example.com",
                "role": "admin",
                "is_owner": False,
                "joined_at": "2025-10-10T10:00:00Z",
                "assigned_by": "owner-uuid-456"
            }
        }


class OrganizationMembersWithRolesResponse(BaseModel):
    """Response with organization members and their roles"""
    members: List[OrganizationMemberWithRole]
    total: int = Field(..., description="Total number of members")
    organization: Organization = Field(..., description="Organization information")

    class Config:
        json_schema_extra = {
            "example": {
                "members": [],
                "total": 5,
                "organization": {}
            }
        }


class InvitationRequest(BaseModel):
    """Schema for sending an invitation"""
    email: str = Field(..., description="Email address to invite")
    invited_by: str = Field(..., description="User UUID of the inviter (upline)")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "invited_by": "uuid-of-the-inviter"
            }
        }

class InvitationData(BaseModel):
    invitation_id: str

class InvitationResponse(BaseModel):
    """Response after sending invitation"""
    success: bool = Field(True, description="Operation success status")
    message: str = Field(..., description="Status message")
    data: InvitationData

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Invitation sent successfully",
                "data": {
                    "invitation_id": "invitation-uuid-123"
                }
            }
        }