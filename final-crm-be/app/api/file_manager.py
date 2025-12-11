"""
File Manager API Endpoints

Provides comprehensive file and folder management with:
- Folder operations (CRUD)
- File operations (upload, download, CRUD)
- Sharing (user, group, public)
- Permissions management
- Browse and search
- Activity logs
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Query, Request
from fastapi.responses import StreamingResponse
from typing import Optional, List
import logging
import math
from io import BytesIO

from app.models.file_manager import (
    # Folder models
    FolderCreate, FolderUpdate, FolderMove, FolderResponse,
    # File models
    FileUploadRequest, FileUpdate, FileMove, FileResponse, FileWithPermissions,
    # Sharing models
    ShareCreate, ShareUpdate, ShareResponse, PublicShareResponse,
    # Browse & search
    BrowseRequest, BrowseResponse, SearchRequest, SearchResponse,
    # Activity
    ActivityResponse, ActivityListRequest, ActivityListResponse,
    # Permission
    CheckPermissionRequest, CheckPermissionResponse, GetPermissionsResponse,
    # Batch operations
    BatchDeleteRequest, BatchMoveRequest, BatchOperationResponse,
    # Statistics
    StorageStatsResponse, FileTypeStatsResponse,
    # Errors
    ErrorResponse
)
from app.services.file_manager_service import get_file_manager_service
from app.services.permission_service import get_permission_service
from app.services.sharing_service import get_sharing_service
from app.auth.dependencies import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/filemanager", tags=["file-manager"])


# =====================================================
# HELPER FUNCTIONS
# =====================================================

def _add_file_url(item: dict) -> dict:
    """
    Add signed URL to file/folder item

    Args:
        item: File or folder dict from database

    Returns:
        Item with url field added
    """
    # Only add URL for files (not folders)
    if not item.get("is_folder", False):
        from app.services.storage_service import get_storage_service
        from supabase import create_client
        from app.config import settings

        try:
            client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
            storage_service = get_storage_service(client)

            # Calculate the correct folder path based on storage_path
            # storage_path format: "folder_name/file_id" or just "file_id" for root
            storage_path = item.get("storage_path", "")

            # Extract folder path from storage_path
            if "/" in storage_path:
                # File is in a folder - extract the folder part
                folder_path = "/" + storage_path.rsplit("/", 1)[0] + "/"
            else:
                # File is in root
                folder_path = "/"

            # Generate signed URL (valid for 1 hour)
            url = storage_service.get_file_url(
                organization_id=item["organization_id"],
                file_id=item["id"],
                folder_path=folder_path,
                expires_in=3600  # 1 hour
            )
            item["url"] = url
        except Exception as e:
            logger.warning(f"Failed to generate URL for file {item['id']}: {e}")
            item["url"] = None
    else:
        # Folders don't have URLs
        item["url"] = None

    return item


# =====================================================
# FOLDER ENDPOINTS
# =====================================================

@router.post("/folders", response_model=FolderResponse)
async def create_folder(
    folder_data: FolderCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new folder

    **Requirements:**
    - User must belong to an organization

    **Args:**
    - name: Folder name (cannot contain / or \\)
    - parent_folder_id: Parent folder ID (null for root)
    - metadata: Optional metadata dictionary

    **Returns:**
    - Created folder data

    **Errors:**
    - 400: Invalid request or circular reference
    - 401: Unauthorized
    - 500: Server error
    """
    logger.info(f"Create folder request from {current_user.email}")

    try:
        # Get user's organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(
                status_code=400,
                detail="User must belong to an organization"
            )

        # Create folder
        fm_service = get_file_manager_service()
        folder = fm_service.create_folder(
            user_id=current_user.user_id,
            organization_id=user_org.id,
            name=folder_data.name,
            parent_folder_id=folder_data.parent_folder_id,
            metadata=folder_data.metadata
        )

        # Add URL (will be None for folders)
        folder_with_url = _add_file_url(folder)

        return FolderResponse(**folder_with_url)

    except Exception as e:
        logger.error(f"Failed to create folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/folders/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get folder details

    **Permissions Required:** view
    """
    logger.info(f"Get folder {folder_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            folder_id,
            "view"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get folder
        from supabase import create_client
        from app.config import settings
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        response = client.table("files")\
            .select("*")\
            .eq("id", folder_id)\
            .execute()

        if not response.data:
            raise HTTPException(status_code=404, detail="Folder not found")

        # Add URL (will be None for folders)
        folder_data = _add_file_url(response.data[0])

        children_response = client.table("files")\
            .select("*", count="exact")\
            .eq("folder_id", folder_id)\
            .eq("is_folder", False)\
            .execute()
        # Gunakan response.count bukan len(response.data)
        children_count = children_response.count if children_response.count is not None else 0

        folder_children_response = client.table("files")\
            .select("*", count="exact")\
            .eq("folder_id", folder_id)\
            .eq("is_folder", True)\
            .execute()
        # ✅ PENTING: Gunakan folder_children_response, bukan folder_children_count
        folder_children_count = folder_children_response.count if folder_children_response.count is not None else 0

        has_subfolders = folder_children_count > 0

        folder_data["children_count"] = children_count
        folder_data["folder_children_count"] = folder_children_count
        folder_data["has_subfolders"] = has_subfolders

        return FolderResponse(**folder_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/folders/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: str,
    folder_data: FolderUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update folder name or metadata

    **Permissions Required:** edit
    """
    logger.info(f"Update folder {folder_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            folder_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Update folder
        fm_service = get_file_manager_service()
        folder = fm_service.update_folder(
            folder_id=folder_id,
            user_id=current_user.user_id,
            name=folder_data.name,
            metadata=folder_data.metadata
        )

        # Add URL (will be None for folders)
        folder_with_url = _add_file_url(folder)

        return FolderResponse(**folder_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/folders/{folder_id}/move", response_model=FolderResponse)
async def move_folder(
    folder_id: str,
    move_data: FolderMove,
    current_user: User = Depends(get_current_user)
):
    """
    Move folder to different parent

    **Permissions Required:** edit

    **Note:** Cannot move folder into its own subfolder (circular reference prevention)
    """
    logger.info(f"Move folder {folder_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            folder_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Move folder
        fm_service = get_file_manager_service()
        folder = fm_service.move_folder(
            folder_id=folder_id,
            new_parent_folder_id=move_data.new_parent_folder_id,
            user_id=current_user.user_id
        )

        # Add URL (will be None for folders)
        folder_with_url = _add_file_url(folder)

        return FolderResponse(**folder_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to move folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: str,
    permanent: bool = Query(False, description="Permanent delete (not recoverable)"),
    current_user: User = Depends(get_current_user)
):
    """
    Delete folder (soft delete or permanent)

    **Permissions Required:** delete

    **Args:**
    - permanent: If true, permanently delete (not recoverable). If false, move to trash.

    **Returns:**
    - Deletion result with status
    """
    logger.info(f"Delete folder {folder_id} (permanent={permanent}) by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            folder_id,
            "delete"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Delete folder
        fm_service = get_file_manager_service()
        result = fm_service.delete_folder(
            folder_id=folder_id,
            user_id=current_user.user_id,
            organization_id=user_org.id,
            permanent=permanent
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/folders/{folder_id}/restore")
async def restore_folder(
    folder_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Restore folder from trash (cascade restore all children)

    **Permissions Required:** edit

    **Returns:**
    - Restore result with statistics (restored_files count)
    """
    logger.info(f"Restore folder {folder_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            folder_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Restore folder
        fm_service = get_file_manager_service()
        result = fm_service.restore_folder(
            folder_id=folder_id,
            user_id=current_user.user_id,
            organization_id=user_org.id
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restore folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# FILE ENDPOINTS
# =====================================================

@router.post("/files", response_model=FileResponse)
async def upload_file(
    file: UploadFile = File(...),
    parent_folder_id: Optional[str] = Form(None, description="Parent folder ID"),
    enable_embedding: bool = Form(True, description="Enable automatic embedding"),
    metadata: Optional[str] = Form(None, description="JSON metadata"),
    current_user: User = Depends(get_current_user)
):
    """
    Upload a file with optional embedding

    **Requirements:**
    - User must belong to an organization
    - File size limit depends on server configuration
    - Content-Type must be multipart/form-data

    **Form Parameters:**
    - file: The file to upload (required)
    - parent_folder_id: Parent folder ID (optional, null for root)
    - enable_embedding: Enable automatic embedding (default: true)
    - metadata: JSON metadata string (optional)

    **Embedding:**
    - If embedding fails, the entire upload is rolled back
    - Supported formats: PDF, DOCX, TXT, MD, CSV, XLSX, Images

    **Returns:**
    - Uploaded file data with embedding status and signed URL

    **Errors:**
    - 400: Invalid request or embedding failed
    - 401: Unauthorized
    - 413: File too large
    - 500: Server error
    """
    logger.info(f"Upload file '{file.filename}' from {current_user.email}")

    try:
        # Get user's organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(
                status_code=400,
                detail="User must belong to an organization"
            )

        # Read file content
        file_content = await file.read()

        # Parse metadata if provided
        file_metadata = {}
        if metadata:
            import json
            try:
                file_metadata = json.loads(metadata)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON metadata")

        # Upload file
        fm_service = get_file_manager_service()
        file_data, error = fm_service.create_file(
            user_id=current_user.user_id,
            organization_id=user_org.id,
            name=file.filename,
            file_content=file_content,
            mime_type=file.content_type or "application/octet-stream",
            parent_folder_id=parent_folder_id,
            metadata=file_metadata,
            enable_embedding=enable_embedding
        )

        if error:
            raise HTTPException(status_code=400, detail=error)

        # Add signed URL
        file_data_with_url = _add_file_url(file_data)

        return FileResponse(**file_data_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{file_id}", response_model=FileWithPermissions)
async def get_file(
    file_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get file details with user's permissions

    **Permissions Required:** view
    """
    logger.info(f"Get file {file_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "view"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get file
        from supabase import create_client
        from app.config import settings
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

        response = client.table("files")\
            .select("*")\
            .eq("id", file_id)\
            .eq("is_folder", False)\
            .execute()

        if not response.data:
            raise HTTPException(status_code=404, detail="File not found")

        file_data = response.data[0]

        # Get user's permissions
        perms = perm_service.get_user_permissions(current_user.user_id, file_id)

        # Add signed URL
        file_data_with_url = _add_file_url(file_data)

        return FileWithPermissions(
            **file_data_with_url,
            user_permissions=perms["permissions"]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Download file content

    **Permissions Required:** view

    **Returns:**
    - File binary content with appropriate Content-Type header
    """
    logger.info(f"Download file {file_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "view"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get file metadata
        from supabase import create_client
        from app.config import settings
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

        response = client.table("files")\
            .select("*")\
            .eq("id", file_id)\
            .eq("is_folder", False)\
            .execute()

        if not response.data:
            raise HTTPException(status_code=404, detail="File not found")

        file_data = response.data[0]

        # Download from storage
        from app.services.storage_service import get_storage_service
        storage_service = get_storage_service(client)

        file_content = storage_service.download_file(
            organization_id=file_data["organization_id"],
            file_id=file_id,
            folder_path=file_data.get("parent_path", "/")
        )

        # Return file as streaming response
        return StreamingResponse(
            BytesIO(file_content),
            media_type=file_data.get("mime_type", "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{file_data["name"]}"'
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download file: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/files/{file_id}/favorite")
async def favorite_file(
    file_id: str,
    is_starred: bool = Query(False, description="True to star, False to unstar"),  # Dari query
    current_user: User = Depends(get_current_user)
):
    """
    Toggle star/favorite status of a file
    
    Mark or unmark a file as starred/favorite for quick access.
    
    **Permissions Required:** read/write
    
    **Args:**
    - file_id: ID of the file to star/unstar
    - star: True to mark as favorite, False to remove from favorites
    
    **Returns:**
    - Updated file object with new star status
    """

    logger.info(f"Update file {file_id} (star ={is_starred}) by {current_user.email}")
    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "edit"
        )
        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")
        
        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")
        
        # Delete file
        fm_service = get_file_manager_service()
        result = fm_service.favorite_file(
            file_id=file_id,
            user_id=current_user.user_id,
            is_starred=is_starred
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to star file: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/files/{file_id}", response_model=FileResponse)
async def update_file(
    file_id: str,
    file_data: FileUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update file metadata

    **Permissions Required:** edit

    **Note:** Use separate endpoint to update file content
    """
    logger.info(f"Update file {file_id} by {current_user.email}")
    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Update file
        fm_service = get_file_manager_service()
        file = fm_service.update_file(
            file_id=file_id,
            user_id=current_user.user_id,
            name=file_data.name,
            metadata=file_data.metadata,
            re_embed=file_data.re_embed
        )

        # Add signed URL
        file_with_url = _add_file_url(file)

        return FileResponse(**file_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files/{file_id}/move", response_model=FileResponse)
async def move_file(
    file_id: str,
    move_data: FileMove,
    current_user: User = Depends(get_current_user)
):
    """
    Move file to different folder

    **Permissions Required:** edit
    """
    logger.info(f"Move file {file_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Move file
        fm_service = get_file_manager_service()
        file = fm_service.move_file(
            file_id=file_id,
            new_parent_folder_id=move_data.new_parent_folder_id,
            user_id=current_user.user_id,
            organization_id=user_org.id
        )

        # Add signed URL
        file_with_url = _add_file_url(file)

        return FileResponse(**file_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to move file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    permanent: bool = Query(False, description="Permanent delete (not recoverable)"),
    current_user: User = Depends(get_current_user)
):
    """
    Delete file (soft delete or permanent)

    **Permissions Required:** delete

    **Args:**
    - permanent: If true, permanently delete including embeddings. If false, move to trash.

    **Returns:**
    - Deletion result with status
    """
    logger.info(f"Delete file {file_id} (permanent={permanent}) by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "delete"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Delete file
        fm_service = get_file_manager_service()
        result = fm_service.delete_file(
            file_id=file_id,
            user_id=current_user.user_id,
            organization_id=user_org.id,
            permanent=permanent
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files/{file_id}/restore", response_model=FileResponse)
async def restore_file(
    file_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Restore file from trash

    **Permissions Required:** edit

    **Returns:**
    - Restored file data with ChromaDB metadata updated
    """
    logger.info(f"Restore file {file_id} by {current_user.email}")

    try:
        # Check permission
        perm_service = get_permission_service()
        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            file_id,
            "edit"
        )

        if not has_perm:
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")

        # Get organization
        from app.services.organization_service import get_organization_service
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Restore file
        fm_service = get_file_manager_service()
        result = fm_service.restore_file(
            file_id=file_id,
            user_id=current_user.user_id,
            organization_id=user_org.id
        )

        # Add signed URL
        result_with_url = _add_file_url(result)

        return FileResponse(**result_with_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restore file: {e}")
        raise HTTPException(status_code=500, detail=str(e))




# =====================================================
# SHARING ENDPOINTS
# =====================================================

@router.post("/shares", response_model=ShareResponse)
async def create_share(
    share_data: ShareCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Share file with user or group

    **Permissions Required:** share

    **Share Types:**
    - user: Share with specific user by email
    - group: Share with group (all members get access)
    - public: Create public share link (use /shares/public endpoint)

    **Permission Levels:**
    - view: Can view/download only
    - edit: Can view + edit
    - delete: Can view + edit + delete
    - share: Can view + edit + delete + share
    - manage: Full control

    **Returns:**
    - Created share data
    """
    logger.info(f"Create share by {current_user.email}")
    try:
        sharing_service = get_sharing_service()

        if share_data.share_type.value == "user":
            # Share with user
            share = sharing_service.share_with_user(
                file_id=share_data.file_id,
                shared_by=current_user.user_id,
                target_email=share_data.shared_with_email,
                permission=share_data.access_level.value,
                expires_at=share_data.expires_at,
                metadata=share_data.metadata
            )

        elif share_data.share_type == "group":
            # Share with group
            share = sharing_service.share_with_group(
                file_id=share_data.file_id,
                shared_by=current_user.user_id,
                group_id=share_data.group_id,
                permission=share_data.access_level.value,
                metadata=share_data.metadata
            )

        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid share_type. Use /shares/public for public shares"
            )

        return ShareResponse(**share)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create share: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/shares/public", response_model=PublicShareResponse)
async def create_public_share(
    file_id: str = Query(..., description="File ID to share publicly"),
    permission: str = Query("view", description="Permission level"),
    expires_in_hours: int = Query(24, description="Expiration in hours"),
    current_user: User = Depends(get_current_user)
):
    """
    Create public share link

    **Permissions Required:** share

    **Returns:**
    - Share data with public URL

    **Security:**
    - Links expire after specified hours
    - Can be revoked at any time
    - Anyone with link can access (within expiration)
    """
    logger.info(f"Create public share for file {file_id} by {current_user.email}")

    try:
        sharing_service = get_sharing_service()

        share = sharing_service.create_public_share(
            file_id=file_id,
            created_by=current_user.user_id,
            permission=permission,
            expires_in_hours=expires_in_hours
        )

        return PublicShareResponse(**share)

    except Exception as e:
        logger.error(f"Failed to create public share: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/public/{share_token}")
async def access_public_share(share_token: str):
    """
    Access file via public share link

    **No Authentication Required**

    **Returns:**
    - File binary content

    **Errors:**
    - 404: Share not found or expired
    """
    logger.info(f"Access public share: {share_token}")

    try:
        sharing_service = get_sharing_service()
        share = sharing_service.get_public_share(share_token)

        if not share:
            raise HTTPException(status_code=404, detail="Share not found or expired")

        file_data = share["files"]

        # Download file
        from supabase import create_client
        from app.config import settings
        from app.services.storage_service import get_storage_service

        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        storage_service = get_storage_service(client)

        file_content = storage_service.download_file(
            organization_id=file_data["organization_id"],
            file_id=file_data["id"],
            folder_path=file_data.get("parent_path", "/")
        )

        return StreamingResponse(
            BytesIO(file_content),
            media_type=file_data.get("mime_type", "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{file_data["name"]}"'
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to access public share: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/shares", response_model=List[ShareResponse])
async def list_shares(
    file_id: Optional[str] = Query(None, description="Filter by file ID"),
    current_user: User = Depends(get_current_user)
):
    """
    List shares

    **Scenarios:**
    - If file_id provided: List all shares for that file (requires manage permission)
    - If file_id not provided: List files shared with current user

    **Returns:**
    - List of shares
    """
    logger.info(f"List shares by {current_user.email}")

    try:
        sharing_service = get_sharing_service()

        if file_id:
            # List shares for specific file
            shares = sharing_service.list_shares(file_id, current_user.user_id)
        else:
            # List files shared with me
            shares = sharing_service.list_shared_with_me(
                current_user.user_id,
                current_user.email
            )
        shares_with_urls = []
        for s in shares:
            # Pastikan berbentuk dict agar mudah dimodifikasi
            s_dict = s.copy() if isinstance(s, dict) else s.__dict__.copy()

            # Tambahkan URL ke dalam field file
            file_data = s_dict.get("file", {})
            if file_data:
                url_data = _add_file_url(file_data)
                # Jika fungsi mengembalikan dict, ambil nilai URL-nya
                if isinstance(url_data, dict):
                    file_data["url"] = url_data.get("url")
                else:
                    file_data["url"] = url_data
                s_dict["file"] = file_data

            shares_with_urls.append(s_dict)

        # Konversi hasil ke ShareResponse
        result = [ShareResponse(**s) for s in shares_with_urls]
        return result

    except Exception as e:
        logger.error(f"Failed to list shares: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/shares/{share_id}", response_model=ShareResponse)
async def update_share(
    share_id: str,
    share_data: ShareUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update file share

    **Permissions Required:** manage (or be the one who created the share)

    **Returns:**
    - Updated share data
    """
    print(share_id,share_data)
    logger.info(f"Update share {share_id} by {current_user.email}")

    try:
        sharing_service = get_sharing_service()

        share = sharing_service.update_share(
            share_id=share_id,
            updated_by=current_user.user_id,
            access_level=share_data.access_level.value if share_data.access_level else None,
            expires_at=share_data.expires_at,
            metadata=share_data.metadata
        )

        return ShareResponse(**share)

    except Exception as e:
        logger.error(f"Failed to update share: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/shares/{share_id}")
async def revoke_share(
    share_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Revoke file share

    **Permissions Required:** manage (or be the one who created the share)

    **Returns:**
    - Revocation result
    """
    logger.info(f"Revoke share {share_id} by {current_user.email}")

    try:
        sharing_service = get_sharing_service()

        result = sharing_service.revoke_share(
            share_id=share_id,
            revoked_by=current_user.user_id
        )

        return result

    except Exception as e:
        logger.error(f"Failed to revoke share: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# BROWSE & SEARCH ENDPOINTS
# =====================================================

@router.get("/browse", response_model=BrowseResponse)
async def browse_folder(
    folder_id: Optional[str] = Query(None, description="Folder ID (null for root)"),
    is_starred: Optional[bool] = Query(None, description="Filter by starred status: false=non-starred, true=starred only, null=all"),
    is_trashed: Optional[bool] = Query(False, description="Filter by trash status: false=non-trash, true=trash only, null=all"),
    sort_by: str = Query("name", description="Sort field"),
    sort_order: str = Query("asc", description="Sort order"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    current_user: User = Depends(get_current_user)
):
    """
    Browse folder contents

    **Returns:**
    - Paginated list of files and folders

    **Sort Options:**
    - name, created_at, updated_at, size

    **Sort Order:**
    - asc, desc
    """
    logger.info(f"Browse folder {folder_id} by {current_user.email}")
    try:
        # Get organization
        from app.services.organization_service import get_organization_service
        from supabase import create_client
        from app.config import settings

        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        # Query files - use SERVICE_ROLE_KEY to bypass RLS (permission checked at API layer)
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        query = client.table("files")\
            .select("*", count="exact")\
            .eq("organization_id", user_org.id)
            
        # Filter by trash status first
        # is_trashed=False (default): show non-trashed items
        # is_trashed=True: show only trashed items
        # is_trashed=None: show all items (no filter)
        if is_trashed is not None:
            query = query.eq("is_trashed", is_trashed)
            if folder_id :
                query = query.eq("folder_id", folder_id)
            # else:
            #     query = query.is_("folder_id", "null")

        # Filter by starred status
        # is_trashed=False (default): show non-trashed items
        # is_trashed=True: show only trashed items
        # is_trashed=None: show all items (no filter)
        if is_starred is not None:
            query = query.eq("is_starred", is_starred)

        # Only apply folder filter when NOT showing trash
        # When showing trash (is_trashed=true), show ALL trashed files regardless of folder
        # This ensures files deleted inside folders appear in trash
        if is_trashed != True:
            # Note: Database column is 'folder_id', but API uses 'parent_folder_id' for clarity
            if folder_id:
                query = query.eq("folder_id", folder_id)
            else:
                query = query.is_("folder_id", "null")

        # Sort
        query = query.order(sort_by, desc=(sort_order == "desc"))

        # Paginate
        offset = (page - 1) * page_size
        query = query.range(offset, offset + page_size - 1)

        response = query.execute()

        total_items = response.count if hasattr(response, 'count') else len(response.data)
        total_pages = math.ceil(total_items / page_size)

        # Filter item parent pada list
        list_ids = {item["id"] for item in response.data}

        filtered_items = []
        for item in response.data:
            parent_id = item.get("folder_id")

            # jika tidak punya parent → tampilkan
            if parent_id is None:
                filtered_items.append(item)
                continue

            # jika parent_id ADA di list_ids → jangan tampilkan
            if parent_id in list_ids:
                continue

            # jika parent_id tidak ada di list → tampilkan
            filtered_items.append(item)

        # replace hasil query
        response.data = filtered_items

        # Get folder path
        folder_path = "/"
        if folder_id:
            folder_response = client.table("files")\
                .select("parent_path, name")\
                .eq("id", folder_id)\
                .execute()

            if folder_response.data:
                folder = folder_response.data[0]
                parent = folder.get("parent_path", "/")
                name = folder["name"]
                folder_path = f"{parent.rstrip('/')}/{name}/"

        # Add signed URLs to items
        items_with_urls = [_add_file_url(item) for item in response.data]
         # === Ambil file_id untuk query file_shares ===
        file_ids = [item["id"] for item in items_with_urls]

        shared_lookup = {}
        if file_ids:
            shared_response = (
                client.table("file_shares")
                .select("*")
                .in_("file_id", file_ids)
                .eq("shared_by", current_user.user_id)
                .order("share_type", desc=True)
                .execute()
            )

            shared_data = shared_response.data or []

            # Grouping berdasarkan file_id
            for share in shared_data:
                fid = share["file_id"]
                if fid not in shared_lookup:
                    shared_lookup[fid] = []
                shared_lookup[fid].append(share)

        # === Gabungkan hasil share ke setiap file ===
        enriched_items = []
        for item in items_with_urls:
            fid = item["id"]
            file_shares = shared_lookup.get(fid, [])
            item["is_shared"] = len(file_shares) > 0
            item["shared"] = file_shares if file_shares else None
            enriched_items.append(item)
        print(BrowseResponse(
            folder_id=folder_id,
            folder_path=folder_path,
            items=[FileResponse(**item) for item in enriched_items],
            total_items=total_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        ))

        # === Return final result ===
        return BrowseResponse(
            folder_id=folder_id,
            folder_path=folder_path,
            items=[FileResponse(**item) for item in enriched_items],
            total_items=total_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to browse folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search", response_model=SearchResponse)
async def search_files(
    query: str = Query(..., description="Search query"),
    folder_id: Optional[str] = Query(None, description="Search within folder"),
    file_types: Optional[str] = Query(None, description="Comma-separated MIME types"),
    is_trashed: Optional[bool] = Query(False, description="Filter by trash status: false=non-trash, true=trash only, null=all"),
    embedding_search: bool = Query(False, description="Use semantic search"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    current_user: User = Depends(get_current_user)
):
    """
    Search files

    **Search Modes:**
    - embedding_search=false: Search by filename (fast, simple)
    - embedding_search=true: Semantic search by content (slower, more accurate)

    **Returns:**
    - Paginated search results
    """
    logger.info(f"Search files: '{query}' by {current_user.email}")

    try:
        # Get organization
        from app.services.organization_service import get_organization_service
        from supabase import create_client
        from app.config import settings

        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

        if embedding_search:
            # Semantic search via ChromaDB
            from app.services.chromadb_service import ChromaDBService
            chroma_service = ChromaDBService()

            results = chroma_service.query_documents(
                query=query,
                organization_id=user_org.id,
                email=current_user.email,
                top_k=page_size
            )

            # Get file IDs from results
            file_ids = list(set([r.get("file_id") for r in results if r.get("file_id")]))

            if file_ids:
                db_query = client.table("files")\
                    .select("*")\
                    .in_("id", file_ids)\
                    .eq("organization_id", user_org.id)

                # Filter by trash status
                if is_trashed is not None:
                    db_query = db_query.eq("is_trashed", is_trashed)

                response = db_query.execute()
                items = response.data
            else:
                items = []

        else:
            # Simple filename search
            db_query = client.table("files")\
                .select("*", count="exact")\
                .eq("organization_id", user_org.id)\
                .ilike("name", f"%{query}%")

            # Filter by trash status first
            if is_trashed is not None:
                db_query = db_query.eq("is_trashed", is_trashed)

            # Only apply folder filter when NOT showing trash
            # When showing trash (is_trashed=true), show ALL trashed files regardless of folder
            if is_trashed != True and folder_id:
                db_query = db_query.eq("folder_id", folder_id)

            if file_types:
                types_list = [t.strip() for t in file_types.split(",")]
                db_query = db_query.in_("mime_type", types_list)

            # Paginate
            offset = (page - 1) * page_size
            db_query = db_query.range(offset, offset + page_size - 1)

            response = db_query.execute()
            items = response.data

        total_items = len(items)
        total_pages = math.ceil(total_items / page_size)

        # Add signed URLs to items
        items_with_urls = [_add_file_url(item) for item in items]

        return SearchResponse(
            query=query,
            items=[FileResponse(**item) for item in items_with_urls],
            total_items=total_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to search files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# PERMISSION ENDPOINTS
# =====================================================

@router.post("/permissions/check", response_model=CheckPermissionResponse)
async def check_permission(
    perm_data: CheckPermissionRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Check if user has specific permission on file

    **Returns:**
    - Permission check result with reason
    """
    try:
        perm_service = get_permission_service()

        has_perm, reason = perm_service.check_permission(
            current_user.user_id,
            perm_data.file_id,
            perm_data.permission.value
        )

        return CheckPermissionResponse(
            file_id=perm_data.file_id,
            permission=perm_data.permission.value,
            has_permission=has_perm,
            reason=reason
        )

    except Exception as e:
        logger.error(f"Failed to check permission: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/permissions/{file_id}", response_model=GetPermissionsResponse)
async def get_permissions(
    file_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get all permissions user has on file

    **Returns:**
    - List of permissions, ownership status, admin status
    """
    try:
        perm_service = get_permission_service()

        perms = perm_service.get_user_permissions(current_user.user_id, file_id)

        return GetPermissionsResponse(**perms)

    except Exception as e:
        logger.error(f"Failed to get permissions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# STATISTICS ENDPOINTS
# =====================================================

@router.get("/stats/storage", response_model=StorageStatsResponse)
async def get_storage_stats(
    current_user: User = Depends(get_current_user)
):
    """
    Get storage statistics for user's organization

    **Returns:**
    - Total files, folders, size, embedding stats
    """
    logger.info(f"Get storage stats for {current_user.email}")

    try:
        # Get organization
        from app.services.organization_service import get_organization_service
        from supabase import create_client
        from app.config import settings

        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)

        if not user_org:
            raise HTTPException(status_code=400, detail="User must belong to an organization")

        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

        # Get stats
        response = client.table("files")\
            .select("is_folder, size, embedding_status")\
            .eq("organization_id", user_org.id)\
            .eq("is_trashed", False)\
            .execute()

        items = response.data

        total_files = sum(1 for item in items if not item["is_folder"])
        total_folders = sum(1 for item in items if item["is_folder"])
        total_size_bytes = sum(item.get("size", 0) or 0 for item in items if not item["is_folder"])

        embedded_files = sum(
            1 for item in items
            if not item["is_folder"] and item.get("embedding_status") == "completed"
        )
        pending_embeddings = sum(
            1 for item in items
            if not item["is_folder"] and item.get("embedding_status") in ["pending", "processing"]
        )
        failed_embeddings = sum(
            1 for item in items
            if not item["is_folder"] and item.get("embedding_status") == "failed"
        )

        return StorageStatsResponse(
            organization_id=user_org.id,
            total_files=total_files,
            total_folders=total_folders,
            total_size_bytes=total_size_bytes,
            total_size_mb=round(total_size_bytes / (1024 * 1024), 2),
            total_size_gb=round(total_size_bytes / (1024 * 1024 * 1024), 2),
            embedded_files=embedded_files,
            pending_embeddings=pending_embeddings,
            failed_embeddings=failed_embeddings
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get storage stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# HEALTH CHECK
# =====================================================

@router.get("/health")
async def file_manager_health():
    """
    Health check for file manager service

    **Returns:**
    - Service health status
    """
    return {
        "status": "healthy",
        "service": "file_manager",
        "features": [
            "folders",
            "files",
            "sharing",
            "permissions",
            "embeddings",
            "search"
        ]
    }
