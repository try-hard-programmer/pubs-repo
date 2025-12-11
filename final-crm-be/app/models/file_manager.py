"""
Pydantic models for File Manager API
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel, Field, validator
from enum import Enum


# =====================================================
# ENUMS
# =====================================================

class PermissionType(str, Enum):
    """Permission levels for file/folder access"""
    VIEW = "view"
    EDIT = "edit"
    DELETE = "delete"
    SHARE = "share"
    MANAGE = "manage"


class ShareType(str, Enum):
    """Types of file sharing"""
    USER = "user"
    GROUP = "group"
    PUBLIC = "public"


class EmbeddingStatus(str, Enum):
    """Status of file embedding"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FileActivityAction(str, Enum):
    """Actions tracked in audit log"""
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    RESTORED = "restored"
    SHARED = "shared"
    UNSHARED = "unshared"
    DOWNLOADED = "downloaded"
    VIEWED = "viewed"
    MOVED = "moved"
    RENAMED = "renamed"
    PERMISSION_CHANGED = "permission_changed"
    EMBEDDED = "embedded"


# =====================================================
# FOLDER MODELS
# =====================================================

class FolderCreate(BaseModel):
    """Request model for creating a folder"""
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Folder name (cannot contain / or \\)",
        example="My Documents"
    )
    parent_folder_id: Optional[str] = Field(
        None,
        description="Parent folder ID (null for root level)",
        example="550e8400-e29b-41d4-a716-446655440000"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Additional metadata (custom JSON object)",
        example={"description": "Important documents", "color": "blue"}
    )

    @validator('name')
    def validate_name(cls, v):
        """Validate folder name"""
        if '/' in v or '\\' in v:
            raise ValueError('Folder name cannot contain / or \\')
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Project Files",
                "parent_folder_id": None,
                "metadata": {
                    "description": "All project-related files",
                    "team": "Engineering"
                }
            }
        }


class FolderUpdate(BaseModel):
    """Request model for updating a folder"""
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="New folder name")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Updated metadata")

    @validator('name')
    def validate_name(cls, v):
        """Validate folder name"""
        if v and ('/' in v or '\\' in v):
            raise ValueError('Folder name cannot contain / or \\')
        return v.strip() if v else v


class FolderMove(BaseModel):
    """Request model for moving a folder"""
    new_parent_folder_id: Optional[str] = Field(None, description="New parent folder ID (null for root)")


class FolderResponse(BaseModel):
    """Response model for folder data"""
    id: str
    name: str
    organization_id: str
    user_id: str
    parent_folder_id: Optional[str] = Field(None, validation_alias="folder_id", serialization_alias="parent_folder_id")  # DB uses 'folder_id', API uses 'parent_folder_id'
    parent_path: Optional[str]
    is_folder: bool = True
    is_trashed: bool
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]
    updated_by: Optional[str]
    metadata: Optional[Dict[str, Any]]
    url: Optional[str] = Field(None, description="URL for folder (null for folders)")
    children_count: int = Field(
        default=0,
        description="Total number of files inside this folder"
    )
    folder_children_count: int = Field(
        default=0,
        description="Total number of subfolders inside this folder"
    )
    has_subfolders: bool = Field(
        default=False,
        description="Whether folder contains subfolders"
    )

    class Config:
        from_attributes = True
        populate_by_name = True  # Allow both names


# =====================================================
# FILE MODELS
# =====================================================

class FileUploadRequest(BaseModel):
    """Request model for file upload metadata"""
    parent_folder_id: Optional[str] = Field(None, description="Parent folder ID")
    enable_embedding: bool = Field(True, description="Enable automatic embedding")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class FileUpdate(BaseModel):
    """Request model for updating file metadata"""
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="New file name")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Updated metadata")
    re_embed: bool = Field(False, description="Re-process embeddings")

    @validator('name')
    def validate_name(cls, v):
        """Validate file name"""
        if v and ('/' in v or '\\' in v):
            raise ValueError('File name cannot contain / or \\')
        return v.strip() if v else v


class FileMove(BaseModel):
    """Request model for moving a file"""
    new_parent_folder_id: Optional[str] = Field(None, description="New parent folder ID (null for root)")

class FileShare(BaseModel):
    id: str
    file_id: str
    shared_by: str
    shared_with_user_id: Optional[str]
    shared_with_email: Optional[str]
    share_type: str
    share_token: Optional[str]
    access_level: str
    expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    metadata: Optional[Dict[str, Any]]

class FileResponse(BaseModel):
    """Response model for file data"""
    id: str
    name: str
    organization_id: str
    user_id: str
    parent_folder_id: Optional[str] = Field(None, validation_alias="folder_id", serialization_alias="parent_folder_id")  # DB uses 'folder_id', API uses 'parent_folder_id'
    parent_path: Optional[str]
    storage_path: Optional[str]
    size: Optional[int]
    mime_type: Optional[str]
    extension: Optional[str]
    is_folder: bool = False
    is_trashed: bool
    is_starred: bool
    is_shared: bool
    embedding_status: Optional[str]
    embedded_at: Optional[datetime]
    embedding_error: Optional[str]
    file_version: Optional[int]
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]
    updated_by: Optional[str]
    metadata: Optional[Dict[str, Any]]
    url: Optional[str] = Field(None, description="Signed URL for file download/preview (valid for 1 hour)")
    is_shared: bool = False
    shared: Optional[List[FileShare]] = None
    class Config:
        from_attributes = True
        populate_by_name = True  # Allow both names


class FileWithPermissions(FileResponse):
    """File response with user's permissions"""
    user_permissions: List[str] = Field(default_factory=list, description="User's permissions on this file")


# =====================================================
# SHARING MODELS
# =====================================================

class ShareCreate(BaseModel):
    """Request model for creating a share"""
    file_id: str = Field(
        ...,
        description="File or folder ID to share",
        example="7c9e6679-7425-40de-944b-e07fc1f90ae7"
    )
    share_type: ShareType = Field(
        ShareType.USER,
        description="Type of share: user, group, or public",
        example="user"
    )

    # For user shares
    shared_with_email: Optional[str] = Field(
        None,
        description="Email of user to share with (required for user shares)",
        example="colleague@example.com"
    )

    # For group shares
    group_id: Optional[str] = Field(
        None,
        description="Group ID to share with (required for group shares)",
        example="group-uuid-123"
    )

    # Permission level
    access_level: PermissionType = Field(
        PermissionType.VIEW,
        description="Permission level: view, edit, delete, share, manage",
        example="view"
    )

    # Expiration
    expires_at: Optional[datetime] = Field(
        None,
        description="Expiration time (optional, ISO 8601 format)",
        example="2025-12-31T23:59:59Z"
    )

    # Metadata
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Additional metadata (optional)",
        example={"note": "Shared for review"}
    )

    @validator('shared_with_email')
    def validate_user_share(cls, v, values):
        """Validate user share has email"""
        if values.get('share_type') == ShareType.USER and not v:
            raise ValueError('shared_with_email is required for user shares')
        return v

    @validator('group_id')
    def validate_group_share(cls, v, values):
        """Validate group share has group_id"""
        if values.get('share_type') == ShareType.GROUP and not v:
            raise ValueError('group_id is required for group shares')
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "file_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "share_type": "user",
                "shared_with_email": "colleague@example.com",
                "access_level": "view",
                "expires_at": None,
                "metadata": {"note": "Please review by Friday"}
            }
        }


class ShareUpdate(BaseModel):
    """Request model for updating a share"""
    access_level: Optional[PermissionType] = Field(None, description="New permission level")
    expires_at: Optional[datetime] = Field(None, description="New expiration time")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Updated metadata")


class FileMinimal(BaseModel):
    id: str
    name: str
    mime_type: Optional[str] = None
    size: Optional[int] = None
    storage_path: Optional[str] = None
    parent_path: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ShareResponse(BaseModel):
    """Response model for share data"""
    id: str
    file_id: str
    shared_by: str
    shared_with_user_id: Optional[str]
    shared_with_email: Optional[str]
    share_type: str
    share_token: Optional[str]
    access_level: str
    expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    metadata: Optional[Dict[str, Any]]
    file: Optional[FileResponse] = Field(
        default=None,
        validation_alias="file",         # ambil dari key "file"
        serialization_alias="file",      # keluarkan sebagai "file"
    )
    class Config:
        from_attributes = True


class PublicShareResponse(ShareResponse):
    """Response for public share with URL"""
    share_url: str = Field(..., description="Public share URL")


# =====================================================
# BROWSE & SEARCH MODELS
# =====================================================

class BrowseRequest(BaseModel):
    """Request model for browsing folder contents"""
    folder_id: Optional[str] = Field(None, description="Folder ID to browse (null for root)")
    include_trashed: bool = Field(False, description="Include trashed items")
    sort_by: str = Field("name", description="Sort field: name, created_at, updated_at, size")
    sort_order: str = Field("asc", description="Sort order: asc or desc")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(50, ge=1, le=200, description="Items per page")


class BrowseResponse(BaseModel):
    """Response model for browse results"""
    folder_id: Optional[str]
    folder_path: Optional[str]
    items: List[FileResponse]
    total_items: int
    page: int
    page_size: int
    total_pages: int


class SearchRequest(BaseModel):
    """Request model for searching files"""
    query: str = Field(..., min_length=1, description="Search query")
    folder_id: Optional[str] = Field(None, description="Search within folder (null for all)")
    file_types: Optional[List[str]] = Field(None, description="Filter by MIME types")
    include_trashed: bool = Field(False, description="Include trashed items")
    embedding_search: bool = Field(False, description="Use semantic search (RAG)")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(50, ge=1, le=200, description="Items per page")


class SearchResponse(BaseModel):
    """Response model for search results"""
    query: str
    items: List[FileResponse]
    total_items: int
    page: int
    page_size: int
    total_pages: int


# =====================================================
# ACTIVITY LOG MODELS
# =====================================================

class ActivityResponse(BaseModel):
    """Response model for file activity"""
    id: str
    file_id: str
    user_id: str
    action: str
    metadata: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    user_agent: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class ActivityListRequest(BaseModel):
    """Request model for listing activities"""
    file_id: Optional[str] = Field(None, description="Filter by file ID")
    user_id: Optional[str] = Field(None, description="Filter by user ID")
    actions: Optional[List[str]] = Field(None, description="Filter by actions")
    start_date: Optional[datetime] = Field(None, description="Start date")
    end_date: Optional[datetime] = Field(None, description="End date")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(50, ge=1, le=200, description="Items per page")


class ActivityListResponse(BaseModel):
    """Response model for activity list"""
    activities: List[ActivityResponse]
    total_items: int
    page: int
    page_size: int
    total_pages: int


# =====================================================
# PERMISSION MODELS
# =====================================================

class CheckPermissionRequest(BaseModel):
    """Request model for checking permissions"""
    file_id: str = Field(..., description="File or folder ID")
    permission: PermissionType = Field(..., description="Permission to check")


class CheckPermissionResponse(BaseModel):
    """Response model for permission check"""
    file_id: str
    permission: str
    has_permission: bool
    reason: Optional[str] = Field(None, description="Reason for decision (owner, share, group, admin)")


class GetPermissionsResponse(BaseModel):
    """Response model for getting all permissions"""
    file_id: str
    permissions: List[str]
    is_owner: bool
    is_admin: bool


# =====================================================
# BATCH OPERATIONS
# =====================================================

class BatchDeleteRequest(BaseModel):
    """Request model for batch delete"""
    file_ids: List[str] = Field(..., min_items=1, max_items=100, description="File/folder IDs to delete")
    permanent: bool = Field(False, description="Permanent delete (not recoverable)")


class BatchMoveRequest(BaseModel):
    """Request model for batch move"""
    file_ids: List[str] = Field(..., min_items=1, max_items=100, description="File/folder IDs to move")
    new_parent_folder_id: Optional[str] = Field(None, description="New parent folder ID")


class BatchOperationResponse(BaseModel):
    """Response model for batch operations"""
    success_count: int
    failed_count: int
    success_ids: List[str]
    failed_ids: List[str]
    errors: Dict[str, str] = Field(default_factory=dict, description="Error messages by file ID")


# =====================================================
# STATISTICS MODELS
# =====================================================

class StorageStatsResponse(BaseModel):
    """Response model for storage statistics"""
    organization_id: str
    total_files: int
    total_folders: int
    total_size_bytes: int
    total_size_mb: float
    total_size_gb: float
    embedded_files: int
    pending_embeddings: int
    failed_embeddings: int


class FileTypeStatsResponse(BaseModel):
    """Response model for file type statistics"""
    mime_type: str
    count: int
    total_size_bytes: int


# =====================================================
# ERROR RESPONSES
# =====================================================

class ErrorResponse(BaseModel):
    """Standard error response"""
    error: str
    detail: Optional[str] = None
    file_id: Optional[str] = None


class ValidationErrorResponse(BaseModel):
    """Validation error response"""
    error: str
    validation_errors: List[Dict[str, Any]]
