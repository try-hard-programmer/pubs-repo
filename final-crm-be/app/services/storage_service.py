"""
Supabase Storage Service
Handles file upload, download, and deletion from Supabase Storage
with organization-based bucket hierarchy
"""
from typing import Optional, Dict, Any, List, BinaryIO
import io
import logging
from supabase import Client
from app.config import settings

logger = logging.getLogger(__name__)


class StorageService:
    """Service for managing files in Supabase Storage"""

    def __init__(self, client: Optional[Client] = None):
        """
        Initialize Storage Service

        Args:
            client: Supabase client (if None, will use settings with SERVICE_ROLE_KEY)
        """
        self.client = client or self._get_supabase_client()

    def _get_supabase_client(self) -> Client:
        """
        Get Supabase client from settings using SERVICE_ROLE_KEY to bypass RLS

        IMPORTANT: Storage operations need SERVICE_ROLE_KEY to bypass RLS policies.
        Using SUPABASE_KEY (anon key) will result in 403 Unauthorized errors.
        """
        from supabase import create_client

        # Use SERVICE_ROLE_KEY to bypass RLS for storage operations
        # Storage buckets have RLS policies that require service role access
        supabase_key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY

        if not settings.SUPABASE_SERVICE_KEY:
            logger.warning(
                "⚠️  SUPABASE_SERVICE_KEY not configured! "
                "Using SUPABASE_KEY (anon key) which may fail due to RLS policies. "
                "Please set SUPABASE_SERVICE_KEY in environment variables."
            )

        return create_client(settings.SUPABASE_URL, supabase_key)

    def _get_bucket_name(self, organization_id: str) -> str:
        """
        Get bucket name for organization

        Format: org_{organization_id}

        Args:
            organization_id: Organization UUID

        Returns:
            Bucket name
        """
        return f"org_{organization_id}"

    def _get_storage_path(self, file_id: str, folder_path: Optional[str] = None) -> str:
        """
        Get storage path for file

        Args:
            file_id: File UUID
            folder_path: Optional parent folder path (e.g., "folder1/folder2")

        Returns:
            Storage path (e.g., "folder1/folder2/file_id" or "file_id")
        """
        if folder_path and folder_path != "/":
            # Remove leading/trailing slashes
            folder_path = folder_path.strip("/")
            return f"{folder_path}/{file_id}"
        return file_id

    def ensure_bucket_exists(self, organization_id: str) -> Dict[str, Any]:
        """
        Ensure bucket exists for organization, create if not

        Args:
            organization_id: Organization UUID

        Returns:
            Bucket info dict

        Raises:
            Exception: If bucket creation fails
        """
        bucket_name = self._get_bucket_name(organization_id)

        try:
            # Try to get bucket
            buckets = self.client.storage.list_buckets()
            bucket_exists = any(b.name == bucket_name for b in buckets)

            if not bucket_exists:
                # Create bucket
                self.client.storage.create_bucket(
                    bucket_name,
                    options={
                        "public": False,  # Private bucket
                        # "file_size_limit": 52428800,  # 50MB limit
                        "allowed_mime_types": None  # Allow all types
                    }
                )
                logger.info(f"✅ Created storage bucket: {bucket_name}")

            return {
                "bucket_name": bucket_name,
                "organization_id": organization_id,
                "status": "ready"
            }

        except Exception as e:
            logger.error(f"Failed to ensure bucket exists: {e}")
            raise Exception(f"Storage bucket setup failed: {str(e)}")

    def upload_file(
        self,
        organization_id: str,
        file_id: str,
        file_content: bytes,
        filename: str,
        folder_path: Optional[str] = None,
        mime_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Upload file to Supabase Storage

        Args:
            organization_id: Organization UUID
            file_id: File UUID (used as storage path)
            file_content: File binary content
            filename: Original filename
            folder_path: Optional parent folder path
            mime_type: Optional MIME type

        Returns:
            Upload result dict with path, url, etc.

        Raises:
            Exception: If upload fails
        """
        try:
            # Ensure bucket exists
            self.ensure_bucket_exists(organization_id)

            bucket_name = self._get_bucket_name(organization_id)
            storage_path = self._get_storage_path(file_id, folder_path)

            # Upload file
            response = self.client.storage.from_(bucket_name).upload(
                path=storage_path,
                file=file_content,
                file_options={
                    "content-type": mime_type or "application/octet-stream",
                    "cache-control": "3600",
                    "upsert": "false"  # Don't overwrite existing files
                }
            )

            logger.info(f"✅ Uploaded file to storage: {bucket_name}/{storage_path}")

            # Get public URL (signed URL for private buckets)
            public_url = self.get_file_url(organization_id, file_id, folder_path)

            return {
                "bucket_name": bucket_name,
                "storage_path": storage_path,
                "public_url": public_url,
                "size": len(file_content),
                "filename": filename,
                "status": "uploaded"
            }

        except Exception as e:
            logger.error(f"Failed to upload file: {e}")
            raise Exception(f"File upload failed: {str(e)}")

    def create_folder(
        self,
        organization_id: str,
        folder_id: str,
        folder_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create folder marker in storage

        Note: Supabase Storage doesn't have true folders, but we create
        an empty file with trailing slash to mark folder presence

        Args:
            organization_id: Organization UUID
            folder_id: Folder UUID
            folder_path: Parent folder path

        Returns:
            Folder creation result
        """
        try:
            self.ensure_bucket_exists(organization_id)

            bucket_name = self._get_bucket_name(organization_id)
            storage_path = self._get_storage_path(folder_id, folder_path) + "/.folder"

            # Upload empty marker file
            self.client.storage.from_(bucket_name).upload(
                path=storage_path,
                file=b"",  # Empty file
                file_options={
                    "content-type": "application/x-directory",
                    "upsert": "false"
                }
            )

            logger.info(f"✅ Created folder in storage: {bucket_name}/{storage_path}")

            return {
                "bucket_name": bucket_name,
                "storage_path": storage_path,
                "status": "created"
            }

        except Exception as e:
            logger.error(f"Failed to create folder: {e}")
            raise Exception(f"Folder creation failed: {str(e)}")

    def download_file(
        self,
        organization_id: str,
        file_id: str,
        folder_path: Optional[str] = None
    ) -> bytes:
        """
        Download file from storage

        Args:
            organization_id: Organization UUID
            file_id: File UUID
            folder_path: Optional parent folder path

        Returns:
            File binary content

        Raises:
            Exception: If download fails
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            storage_path = self._get_storage_path(file_id, folder_path)

            # Download file
            response = self.client.storage.from_(bucket_name).download(folder_path)

            logger.info(f"✅ Downloaded file from storage: {bucket_name}/{folder_path}")

            return response

        except Exception as e:
            logger.error(f"Failed to download file: {e}")
            raise Exception(f"File download failed: {str(e)}")

    def delete_file(
        self,
        organization_id: str,
        file_id: str,
        folder_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Delete file from storage

        Args:
            organization_id: Organization UUID
            file_id: File UUID
            folder_path: Optional parent folder path

        Returns:
            Deletion result

        Raises:
            Exception: If deletion fails
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            storage_path = self._get_storage_path(file_id, folder_path)

            # Delete file
            self.client.storage.from_(bucket_name).remove([storage_path])

            logger.info(f"✅ Deleted file from storage: {bucket_name}/{storage_path}")

            return {
                "bucket_name": bucket_name,
                "storage_path": storage_path,
                "status": "deleted"
            }

        except Exception as e:
            logger.error(f"Failed to delete file: {e}")
            raise Exception(f"File deletion failed: {str(e)}")

    def delete_folder(
        self,
        organization_id: str,
        folder_id: str,
        folder_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Delete folder and all its contents from storage

        Args:
            organization_id: Organization UUID
            folder_id: Folder UUID
            folder_path: Parent folder path

        Returns:
            Deletion result

        Raises:
            Exception: If deletion fails
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            folder_storage_path = self._get_storage_path(folder_id, folder_path)

            # List all files in folder
            files = self.client.storage.from_(bucket_name).list(folder_storage_path)

            # Collect all paths to delete
            paths_to_delete = []

            for file_obj in files:
                file_path = f"{folder_storage_path}/{file_obj['name']}"
                paths_to_delete.append(file_path)

            # Add folder marker
            paths_to_delete.append(f"{folder_storage_path}/.folder")

            # Delete all files
            if paths_to_delete:
                self.client.storage.from_(bucket_name).remove(paths_to_delete)

            logger.info(f"✅ Deleted folder from storage: {bucket_name}/{folder_storage_path} ({len(paths_to_delete)} items)")

            return {
                "bucket_name": bucket_name,
                "storage_path": folder_storage_path,
                "deleted_count": len(paths_to_delete),
                "status": "deleted"
            }

        except Exception as e:
            logger.error(f"Failed to delete folder: {e}")
            raise Exception(f"Folder deletion failed: {str(e)}")

    def move_file(
        self,
        organization_id: str,
        file_id: str,
        old_folder_path: Optional[str],
        new_folder_path: Optional[str]
    ) -> Dict[str, Any]:
        """
        Move file to different folder

        Note: Supabase Storage doesn't have move operation,
        so we download -> upload -> delete

        Args:
            organization_id: Organization UUID
            file_id: File UUID
            old_folder_path: Current folder path
            new_folder_path: New folder path

        Returns:
            Move result

        Raises:
            Exception: If move fails
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            old_path = self._get_storage_path(file_id, old_folder_path)
            new_path = self._get_storage_path(file_id, new_folder_path)
            # # Download file
            # file_content = self.download_file(organization_id, file_id, old_folder_path)

            # # Upload to new location
            # self.client.storage.from_(bucket_name).upload(
            #     path=new_path,
            #     file=file_content,
            #     file_options={"upsert": "true"}
            # )

            # # Delete old file
            # self.client.storage.from_(bucket_name).remove([old_path])

            self.client.storage.from_(bucket_name).move(old_path, new_path);

            logger.info(f"✅ Moved file in storage: {old_path} → {new_path}")

            return {
                "bucket_name": bucket_name,
                "old_path": old_path,
                "new_path": new_path,
                "status": "moved"
            }

        except Exception as e:
            logger.error(f"Failed to move file: {e}")
            raise Exception(f"File move failed: {str(e)}")

    def get_file_url(
        self,
        organization_id: str,
        file_id: str,
        folder_path: Optional[str] = None,
        expires_in: int = 3600
    ) -> str:
        """
        Get signed URL for file access (for private buckets)

        Args:
            organization_id: Organization UUID
            file_id: File UUID
            folder_path: Optional parent folder path
            expires_in: URL expiration time in seconds (default 1 hour)

        Returns:
            Signed URL

        Raises:
            Exception: If URL generation fails
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            storage_path = self._get_storage_path(file_id, folder_path)

            # Get signed URL
            response = self.client.storage.from_(bucket_name).create_signed_url(
                storage_path,
                expires_in
            )

            return response["signedURL"]

        except Exception as e:
            logger.error(f"Failed to get file URL: {e}")
            # Return fallback URL
            return f"/api/files/{file_id}/download"

    def get_public_url(
        self,
        organization_id: str,
        file_id: str,
        folder_path: Optional[str] = None
    ) -> str:
        """
        Get public URL for file (alias for get_file_url with signed URL)

        This method is used for compatibility with document_processor.py
        where we need a public-accessible URL for audio transcription API.

        Args:
            organization_id: Organization UUID
            file_id: File UUID
            folder_path: Optional parent folder path

        Returns:
            Signed URL (valid for 1 hour)
        """
        # Use get_file_url to get signed URL
        return self.get_file_url(organization_id, file_id, folder_path, expires_in=3600)

    def list_folder_contents(
        self,
        organization_id: str,
        folder_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List contents of a folder

        Args:
            organization_id: Organization UUID
            folder_path: Folder path (None for root)

        Returns:
            List of file/folder objects
        """
        try:
            bucket_name = self._get_bucket_name(organization_id)
            path = folder_path.strip("/") if folder_path else ""

            # List files
            files = self.client.storage.from_(bucket_name).list(path)

            return files

        except Exception as e:
            logger.error(f"Failed to list folder contents: {e}")
            return []


# Singleton instance
_storage_service: Optional[StorageService] = None


def get_storage_service(client: Optional[Client] = None) -> StorageService:
    """
    Get or create StorageService singleton

    Args:
        client: Optional Supabase client

    Returns:
        StorageService instance
    """
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService(client)
    return _storage_service
