"""
File Manager Service
Handles file and folder operations with database + storage synchronization
Includes embedding integration and rollback mechanisms
"""
import re
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4
import logging
from datetime import datetime

from supabase import Client

from app.services.storage_service import get_storage_service, StorageService
from app.services.chromadb_service import ChromaDBService
from app.services.document_processor import DocumentProcessor
from app.utils import split_into_chunks, to_clean_text_from_strs
from app.config import settings

logger = logging.getLogger(__name__)


class FileManagerService:
    """Service for managing files and folders with embedding support"""

    def __init__(self, supabase_client: Optional[Client] = None):
        """
        Initialize File Manager Service

        Args:
            supabase_client: Supabase client for database operations
        """
        from supabase import create_client

        # Use SERVICE_ROLE_KEY to bypass RLS
        # Permission checking is done at API layer, so service can bypass RLS
        self.client = supabase_client or create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY  # ← Use service role key to bypass RLS
        )
        self.storage_service = get_storage_service(self.client)
        self.chromadb_service = ChromaDBService()
        # Pass storage_service to DocumentProcessor to avoid circular dependency
        self.document_processor = DocumentProcessor(storage_service=self.storage_service)

    # =====================================================
    # FOLDER OPERATIONS
    # =====================================================

    def create_folder(
        self,
        user_id: str,
        organization_id: str,
        name: str,
        parent_folder_id: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Create a new folder

        Args:
            user_id: User UUID
            organization_id: Organization UUID
            name: Folder name
            parent_folder_id: Optional parent folder UUID
            metadata: Optional metadata dict

        Returns:
            Created folder data

        Raises:
            Exception: If folder creation fails
        """
        try:
            folder_id = str(uuid4())

            # 1. Calculate parent_path
            parent_path = self._get_parent_path(parent_folder_id) if parent_folder_id else "/"

            # 2. Create folder in database
            folder_data = {
                "id": folder_id,
                "user_id": user_id,
                "organization_id": organization_id,
                "name": name,
                "type": "folder",
                "size": 0,
                "is_folder": True,
                "folder_id": parent_folder_id,
                "parent_path": parent_path,
                "created_by": user_id,
                "updated_by": user_id,
                "metadata": metadata or {},
                "embedding_status": "skipped",  # Folders are not embedded
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("files").insert(folder_data).execute()

            if not response.data:
                raise Exception("Failed to create folder in database")

            created_folder = response.data[0]

            # 3. Create folder marker in storage
            try:
                self.storage_service.create_folder(
                    organization_id=organization_id,
                    folder_id=folder_id,
                    folder_path=parent_path
                )
            except Exception as storage_error:
                # Rollback database entry
                logger.error(f"Storage creation failed, rolling back: {storage_error}")
                self.client.table("files").delete().eq("id", folder_id).execute()
                raise Exception(f"Folder creation failed: {str(storage_error)}")

            logger.info(f"✅ Created folder: {name} (id: {folder_id})")

            return created_folder

        except Exception as e:
            logger.error(f"Failed to create folder: {e}")
            raise

    def update_folder(
        self,
        folder_id: str,
        user_id: str,
        name: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Update folder name or metadata

        Args:
            folder_id: Folder UUID
            user_id: User UUID (for audit)
            name: New folder name
            metadata: New metadata

        Returns:
            Updated folder data
        """
        try:
            update_data = {
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            if name:
                update_data["name"] = name

            if metadata:
                update_data["metadata"] = metadata

            response = self.client.table("files")\
                .update(update_data)\
                .eq("id", folder_id)\
                .eq("is_folder", True)\
                .execute()

            if not response.data:
                raise Exception("Folder not found or update failed")

            logger.info(f"✅ Updated folder: {folder_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to update folder: {e}")
            raise

    def delete_folder(
        self,
        folder_id: str,
        user_id: str,
        organization_id: str,
        permanent: bool = False
    ) -> Dict[str, Any]:
        """
        Delete folder (soft delete or permanent)

        Args:
            folder_id: Folder UUID
            user_id: User UUID (for audit)
            organization_id: Organization UUID
            permanent: If True, permanently delete; if False, move to trash

        Returns:
            Deletion result
        """
        try:
            # 1. Get folder data
            folder_response = self.client.table("files")\
                .select("*")\
                .eq("id", folder_id)\
                .eq("is_folder", True)\
                .execute()

            if not folder_response.data:
                raise Exception("Folder not found")

            folder = folder_response.data[0]

            # 2. Get all children (files and subfolders)
            children_response = self.client.table("files")\
                .select("id, is_folder, organization_id, parent_path")\
                .eq("folder_id", folder_id)\
                .execute()

            children = children_response.data if children_response.data else []

            if permanent:
                # 3a. Permanent deletion with cascade
                deleted_files = 0
                deleted_folders = 0
                deleted_chunks = 0

                logger.info(f"🗑️  Cascade delete folder: {folder_id} ({len(children)} children)")

                # Delete children first (recursive)
                for child in children:
                    if child["is_folder"]:
                        # Recursive folder deletion
                        result = self.delete_folder(
                            folder_id=child["id"],
                            user_id=user_id,
                            organization_id=organization_id,
                            permanent=True
                        )
                        deleted_folders += 1
                        # Accumulate statistics from recursive calls
                        deleted_files += result.get("deleted_files", 0)
                        deleted_folders += result.get("deleted_folders", 0)
                        deleted_chunks += result.get("deleted_chunks", 0)
                    else:
                        # Delete file (includes Storage + ChromaDB + DB)
                        file_result = self.delete_file(
                            file_id=child["id"],
                            user_id=user_id,
                            organization_id=organization_id,
                            permanent=True
                        )
                        deleted_files += 1
                        deleted_chunks += file_result.get("deleted_chunks", 0)

                # Delete folder from Supabase Storage
                try:
                    parent_path = folder.get("parent_path", "/")
                    self.storage_service.delete_folder(
                        organization_id=organization_id,
                        folder_id=folder_id,
                        folder_path=parent_path
                    )
                    logger.info(f"🗑️  Deleted folder from Supabase Storage: {folder_id}")
                except Exception as storage_error:
                    logger.warning(f"⚠️  Storage deletion failed (non-critical): {storage_error}")

                # Delete folder from database
                self.client.table("files").delete().eq("id", folder_id).execute()

                logger.info(f"✅ Permanently deleted folder: {folder_id} ({deleted_files} files, {deleted_folders} folders, {deleted_chunks} chunks)")

                return {
                    "folder_id": folder_id,
                    "status": "permanently_deleted",
                    "deleted_files": deleted_files,
                    "deleted_folders": deleted_folders,
                    "deleted_chunks": deleted_chunks
                }

            else:
                # 3b. Soft delete (move to trash)
                update_data = {
                    "is_trashed": True,
                    "updated_by": user_id,
                    "updated_at": datetime.utcnow().isoformat()
                }

                self.client.table("files")\
                    .update(update_data)\
                    .eq("id", folder_id)\
                    .execute()

                # Update ChromaDB metadata for all child files (cascade soft delete)
                # This ensures trashed files don't appear in agent query results
                trashed_files = 0
                for child in children:
                    if not child["is_folder"]:
                        # Update ChromaDB metadata for file
                        try:
                            self.chromadb_service.update_document_metadata_by_file_id(
                                organization_id=organization_id,
                                file_id=child["id"],
                                metadata_updates={"is_trashed": True}
                            )
                            trashed_files += 1
                        except Exception as chroma_error:
                            logger.warning(f"⚠️  Failed to update ChromaDB metadata for {child['id']}: {chroma_error}")

                logger.info(f"✅ Moved folder to trash: {folder_id} (updated {trashed_files} file embeddings)")

                return {
                    "folder_id": folder_id,
                    "status": "trashed",
                    "trashed_files": trashed_files
                }

        except Exception as e:
            logger.error(f"Failed to delete folder: {e}")
            raise

    def move_folder(
        self,
        folder_id: str,
        new_parent_folder_id: Optional[str],
        user_id: str
    ) -> Dict[str, Any]:
        """
        Move folder to different parent

        Args:
            folder_id: Folder UUID to move
            new_parent_folder_id: New parent folder UUID (None for root)
            user_id: User UUID (for audit)

        Returns:
            Move result
        """
        try:
            # Calculate new parent path
            new_parent_path = self._get_parent_path(new_parent_folder_id) if new_parent_folder_id else "/"

            # Update folder
            update_data = {
                "folder_id": new_parent_folder_id,
                "parent_path": new_parent_path,
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("files")\
                .update(update_data)\
                .eq("id", folder_id)\
                .eq("is_folder", True)\
                .execute()

            if not response.data:
                raise Exception("Folder not found or move failed")

            logger.info(f"✅ Moved folder: {folder_id} to {new_parent_folder_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to move folder: {e}")
            raise

    def restore_folder(
        self,
        folder_id: str,
        user_id: str,
        organization_id: str
    ) -> Dict[str, Any]:
        """
        Restore folder from trash (cascade restore all children)

        Args:
            folder_id: Folder UUID to restore
            user_id: User UUID (for audit)
            organization_id: Organization UUID

        Returns:
            Restore result with statistics
        """
        try:
            # Get folder data
            folder_response = self.client.table("files")\
                .select("*")\
                .eq("id", folder_id)\
                .eq("is_folder", True)\
                .eq("is_trashed", True)\
                .execute()

            if not folder_response.data:
                raise Exception("Folder not found in trash")

            # Get all children (files and subfolders)
            children_response = self.client.table("files")\
                .select("id, is_folder")\
                .eq("folder_id", folder_id)\
                .execute()

            children = children_response.data if children_response.data else []

            # Update folder in database
            update_data = {
                "is_trashed": False,
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            self.client.table("files")\
                .update(update_data)\
                .eq("id", folder_id)\
                .execute()

            # Restore all child files (cascade restore)
            # Update ChromaDB metadata for all child files
            restored_files = 0
            for child in children:
                if not child["is_folder"]:
                    # Update ChromaDB metadata for file
                    try:
                        self.chromadb_service.update_document_metadata_by_file_id(
                            organization_id=organization_id,
                            file_id=child["id"],
                            metadata_updates={"is_trashed": False}
                        )
                        restored_files += 1
                    except Exception as chroma_error:
                        logger.warning(f"⚠️  Failed to update ChromaDB metadata for {child['id']}: {chroma_error}")

            logger.info(f"✅ Restored folder from trash: {folder_id} (restored {restored_files} file embeddings)")

            # Get updated folder data
            restored_folder = self.client.table("files")\
                .select("*")\
                .eq("id", folder_id)\
                .execute()

            return {
                "folder": restored_folder.data[0] if restored_folder.data else {},
                "restored_files": restored_files,
                "status": "restored"
            }

        except Exception as e:
            logger.error(f"Failed to restore folder: {e}")
            raise

    # =====================================================
    # FILE OPERATIONS
    # =====================================================

    def create_file(
        self,
        user_id: str,
        organization_id: str,
        name: str,
        file_content: bytes,
        mime_type: str,
        parent_folder_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        enable_embedding: bool = True
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        Create a new file with optional embedding

        Args:
            user_id: User UUID
            organization_id: Organization UUID
            name: File name
            file_content: File binary content
            mime_type: MIME type
            parent_folder_id: Optional parent folder UUID
            metadata: Optional metadata
            enable_embedding: Whether to embed file content

        Returns:
            Tuple of (file_data, error_message)
            If embedding fails, returns (file_data, error_message)

        Raises:
            Exception: If file creation fails completely
        """
        file_id = None
        storage_uploaded = False
        db_created = False
        embedding_error = None

        try:
            file_id = str(uuid4())

            # 1. Get extension
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""

            # 2. Calculate parent_path
            parent_path = self._get_parent_path(parent_folder_id) if parent_folder_id else "/"

            # 3. Upload to storage first
            try:
                storage_result = self.storage_service.upload_file(
                    organization_id=organization_id,
                    file_id=file_id,
                    file_content=file_content,
                    filename=name,
                    folder_path=parent_path,
                    mime_type=mime_type
                )
                storage_uploaded = True
                storage_path = storage_result["storage_path"]
                file_size = storage_result["size"]

            except Exception as storage_error:
                raise Exception(f"Storage upload failed: {str(storage_error)}")

            # 4. Create file in database
            file_data = {
                "id": file_id,
                "user_id": user_id,
                "organization_id": organization_id,
                "name": name,
                "type": mime_type,
                "mime_type": mime_type,
                "extension": extension,
                "size": file_size,
                "storage_path": storage_path,
                "is_folder": False,
                "folder_id": parent_folder_id,
                "parent_path": parent_path,
                "created_by": user_id,
                "updated_by": user_id,
                "metadata": metadata or {},
                "embedding_status": "pending" if enable_embedding else "skipped",
                "file_version": 1,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("files").insert(file_data).execute()

            if not response.data:
                raise Exception("Failed to create file in database")

            db_created = True
            created_file = response.data[0]

            # 5. Process embedding (if enabled and supported file type)
            if enable_embedding and self._should_embed_file(extension):
                try:
                    embedding_result = self._embed_file(
                        file_id=file_id,
                        file_content=file_content,
                        filename=name,
                        extension=extension,
                        organization_id=organization_id,
                        folder_path=f"{parent_path}",
                        user_id=user_id
                    )

                    # Update file with embedding success
                    self.client.table("files").update({
                        "embedding_status": "completed",
                        "embedded_at": datetime.utcnow().isoformat()
                    }).eq("id", file_id).execute()

                    logger.info(f"✅ File embedded successfully: {file_id}")

                except Exception as embed_error:
                    embedding_error = str(embed_error)
                    logger.error(f"❌ Embedding failed for {file_id}: {embedding_error}")

                    # Update file with embedding failure
                    self.client.table("files").update({
                        "embedding_status": "failed",
                        "embedding_error": embedding_error
                    }).eq("id", file_id).execute()

                    # ROLLBACK: Delete file from database and storage
                    logger.warning(f"🔄 Rolling back file creation due to embedding failure: {file_id}")

                    try:
                        # Delete from database
                        self.client.table("files").delete().eq("id", file_id).execute()
                        db_created = False

                        # Delete from storage
                        self.storage_service.delete_file(
                            organization_id=organization_id,
                            file_id=file_id,
                            folder_path=parent_path
                        )
                        storage_uploaded = False

                        logger.info(f"✅ Rollback completed for {file_id}")

                    except Exception as rollback_error:
                        logger.error(f"❌ Rollback failed: {rollback_error}")

                    # Return error to caller
                    raise Exception(f"File embedding failed: {embedding_error}")

            logger.info(f"✅ Created file: {name} (id: {file_id})")

            return created_file, None

        except Exception as e:
            # Rollback if needed
            if storage_uploaded and not db_created:
                try:
                    parent_path = self._get_parent_path(parent_folder_id) if parent_folder_id else "/"
                    self.storage_service.delete_file(
                        organization_id=organization_id,
                        file_id=file_id,
                        folder_path=parent_path
                    )
                    logger.info(f"✅ Rolled back storage upload for {file_id}")
                except Exception as rollback_error:
                    logger.error(f"❌ Rollback failed: {rollback_error}")

            logger.error(f"Failed to create file: {e}")
            raise

    def update_file(
        self,
        file_id: str,
        user_id: str,
        name: Optional[str] = None,
        file_content: Optional[bytes] = None,
        metadata: Optional[Dict] = None,
        re_embed: bool = False
    ) -> Dict[str, Any]:
        """
        Update file name, content, or metadata

        Args:
            file_id: File UUID
            user_id: User UUID (for audit)
            name: New file name
            file_content: New file content (triggers re-upload and re-embedding)
            metadata: New metadata
            re_embed: Force re-embedding even if content hasn't changed

        Returns:
            Updated file data
        """
        try:
            # Get current file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .eq("is_folder", False)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            current_file = file_response.data[0]

            update_data = {
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            if name:
                update_data["name"] = name

            if metadata:
                update_data["metadata"] = metadata

            # If file content changed, re-upload and re-embed
            if file_content:
                # Upload new version to storage
                storage_result = self.storage_service.upload_file(
                    organization_id=current_file["organization_id"],
                    file_id=file_id,
                    file_content=file_content,
                    filename=name or current_file["name"],
                    folder_path=current_file.get("parent_path", "/"),
                    mime_type=current_file["mime_type"]
                )

                update_data["size"] = storage_result["size"]
                update_data["file_version"] = current_file.get("file_version", 1) + 1
                update_data["embedding_status"] = "pending"

                # Re-embed
                try:
                    self._embed_file(
                        file_id=file_id,
                        file_content=file_content,
                        filename=name or current_file["name"],
                        extension=current_file["extension"],
                        organization_id=current_file["organization_id"],
                        folder_path=current_file.get("parent_path", "/"),
                        user_id=user_id
                    )

                    update_data["embedding_status"] = "completed"
                    update_data["embedded_at"] = datetime.utcnow().isoformat()

                except Exception as embed_error:
                    update_data["embedding_status"] = "failed"
                    update_data["embedding_error"] = str(embed_error)

            # Update database
            response = self.client.table("files")\
                .update(update_data)\
                .eq("id", file_id)\
                .execute()

            if not response.data:
                raise Exception("File update failed")

            logger.info(f"✅ Updated file: {file_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to update file: {e}")
            raise

    def delete_file(
        self,
        file_id: str,
        user_id: str,
        organization_id: str,
        permanent: bool = False
    ) -> Dict[str, Any]:
        """
        Delete file (soft delete or permanent)

        Args:
            file_id: File UUID
            user_id: User UUID (for audit)
            organization_id: Organization UUID
            permanent: If True, permanently delete; if False, move to trash

        Returns:
            Deletion result
        """
        try:
            # Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .eq("is_folder", False)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            file_data = file_response.data[0]

            if permanent:
                deleted_chunks = 0

                # 1. Delete embeddings from ChromaDB (using file_id metadata)
                try:
                    chroma_result = self.chromadb_service.delete_documents_by_file_id(
                        organization_id=organization_id,
                        file_id=file_id
                    )
                    deleted_chunks = chroma_result.get('deleted_chunks', 0)
                    logger.info(f"🗑️  Deleted {deleted_chunks} chunks from ChromaDB for file: {file_id}")
                except Exception as chroma_error:
                    logger.warning(f"⚠️  ChromaDB deletion failed (non-critical): {chroma_error}")

                # 2. Delete from Supabase Storage
                try:
                    self.storage_service.delete_file(
                        organization_id=organization_id,
                        file_id=file_id,
                        folder_path=file_data.get("parent_path", "/")
                    )
                    logger.info(f"🗑️  Deleted file from Supabase Storage: {file_id}")
                except Exception as storage_error:
                    logger.warning(f"⚠️  Storage deletion failed (non-critical): {storage_error}")

                # 3. Delete from database
                self.client.table("files").delete().eq("id", file_id).execute()

                logger.info(f"✅ Permanently deleted file: {file_id} ({deleted_chunks} chunks)")

                return {
                    "file_id": file_id,
                    "status": "permanently_deleted",
                    "deleted_chunks": deleted_chunks
                }

            else:
                # Soft delete (move to trash)
                update_data = {
                    "is_trashed": True,
                    "updated_by": user_id,
                    "updated_at": datetime.utcnow().isoformat()
                }

                self.client.table("files")\
                    .update(update_data)\
                    .eq("id", file_id)\
                    .execute()

                # Update ChromaDB metadata to mark as trashed (exclude from agent queries)
                try:
                    self.chromadb_service.update_document_metadata_by_file_id(
                        organization_id=organization_id,
                        file_id=file_id,
                        metadata_updates={"is_trashed": True}
                    )
                    logger.info(f"📝 Updated ChromaDB metadata is_trashed=True for file: {file_id}")
                except Exception as chroma_error:
                    logger.warning(f"⚠️  Failed to update ChromaDB metadata (non-critical): {chroma_error}")

                logger.info(f"✅ Moved file to trash: {file_id}")

                return {
                    "file_id": file_id,
                    "status": "trashed"
                }

        except Exception as e:
            logger.error(f"Failed to delete file: {e}")
            raise

    def move_file(
        self,
        file_id: str,
        new_parent_folder_id: Optional[str],
        user_id: str,
        organization_id: str
    ) -> Dict[str, Any]:
        """
        Move file to different folder

        Args:
            file_id: File UUID to move
            new_parent_folder_id: New parent folder UUID (None for root)
            user_id: User UUID (for audit)
            organization_id: Organization UUID

        Returns:
            Move result
        """
        try:
            # Get current file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .eq("is_folder", False)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            file_data = file_response.data[0]
            old_parent_path = file_data.get("parent_path", "/")

            # Calculate new parent path
            new_parent_path = self._get_parent_path(new_parent_folder_id) if new_parent_folder_id else "/"

            # Move in storage
            try:
                self.storage_service.move_file(
                    organization_id=organization_id,
                    file_id=file_id,
                    old_folder_path=old_parent_path,
                    new_folder_path=new_parent_path
                )
            except Exception as storage_error:
                logger.warning(f"Storage move failed (non-critical): {storage_error}")

            # Update database
            update_data = {
                "folder_id": new_parent_folder_id,
                "parent_path": new_parent_path,
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("files")\
                .update(update_data)\
                .eq("id", file_id)\
                .execute()

            if not response.data:
                raise Exception("File move failed")

            logger.info(f"✅ Moved file: {file_id} to {new_parent_folder_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to move file: {e}")
            raise

    def restore_file(
        self,
        file_id: str,
        user_id: str,
        organization_id: str
    ) -> Dict[str, Any]:
        """
        Restore file from trash

        Args:
            file_id: File UUID to restore
            user_id: User UUID (for audit)
            organization_id: Organization UUID

        Returns:
            Restored file data
        """
        try:
            # Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .eq("is_folder", False)\
                .eq("is_trashed", True)\
                .execute()

            if not file_response.data:
                raise Exception("File not found in trash")

            # Update database to restore file
            update_data = {
                "is_trashed": False,
                "updated_by": user_id,
                "updated_at": datetime.utcnow().isoformat()
            }

            self.client.table("files")\
                .update(update_data)\
                .eq("id", file_id)\
                .execute()

            # Update ChromaDB metadata to mark as active (include in agent queries)
            try:
                self.chromadb_service.update_document_metadata_by_file_id(
                    organization_id=organization_id,
                    file_id=file_id,
                    metadata_updates={"is_trashed": False}
                )
                logger.info(f"📝 Updated ChromaDB metadata is_trashed=False for file: {file_id}")
            except Exception as chroma_error:
                logger.warning(f"⚠️  Failed to update ChromaDB metadata (non-critical): {chroma_error}")

            logger.info(f"✅ Restored file from trash: {file_id}")

            # Get updated file data
            restored_file = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            return restored_file.data[0] if restored_file.data else {}

        except Exception as e:
            logger.error(f"Failed to restore file: {e}")
            raise

    # =====================================================
    # HELPER METHODS
    # =====================================================

    def _get_parent_path(self, folder_id: Optional[str]) -> str:
        """Get full path of parent folder"""
        if not folder_id:
            return "/"

        response = self.client.table("files")\
            .select("parent_path, name")\
            .eq("id", folder_id)\
            .eq("is_folder", True)\
            .execute()

        if response.data:
            folder = response.data[0]
            parent = folder.get("parent_path", "/")
            name = folder["name"]
            return f"{parent.rstrip('/')}/{name}/"

        return "/"

    def _should_embed_file(self, extension: str) -> bool:
        """Check if file type should be embedded"""
        embeddable_extensions = {
            "pdf", "docx", "doc", "txt", "md", "markdown", "pptx", "ppt",
            "csv", "xlsx", "xls", "json",
            "mp3", "wav", "mp4", "mov", "avi", "mkv", "m4a",  # Audio/Video with transcription
            "jpg", "jpeg", "png", "webp", "bmp", "gif"  # Images with OCR
        }
        return extension.lower() in embeddable_extensions

    def _embed_file(
        self,
        file_id: str,
        file_content: bytes,
        filename: str,
        extension: str,
        organization_id: str,
        folder_path: str,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Process and embed file content

        Raises:
            Exception: If embedding fails
        """
        try:
            # 1. Process document to extract text
            text, _ = self.document_processor.process_document(file_content, filename, folder_path, organization_id, file_id)

            #  please clear text from emoji and entertain only plain text
            text = re.sub(r'[^\x00-\x7F]+', ' ', text)

            print("Clear Text :", text)

            # 2. Split into chunks
            chunks = self._get_chunks_by_type(text, extension)

            if not chunks:
                raise Exception("No text content extracted from file")

            # 3. Add to ChromaDB
            actual_file_id = self.chromadb_service.add_chunks(
                chunks=chunks,
                filename=filename,
                organization_id=organization_id,
                file_id=file_id,
                batch_size=settings.DEFAULT_BATCH_SIZE,
                email=user_id
            )

            # 4. Record embedding in database
            embedding_data = {
                "file_id": file_id,
                "organization_id": organization_id,
                "collection_name": f"org_{organization_id}",
                "chunks_count": len(chunks),
                "embedding_model": "text-embedding-ada-002",
                "embedded_at": datetime.utcnow().isoformat(),
                "metadata": {
                    "filename": filename,
                    "extension": extension,
                    "chunks_count": len(chunks)
                }
            }

            self.client.table("file_embeddings").insert(embedding_data).execute()

            logger.info(f"✅ Embedded file {file_id}: {len(chunks)} chunks")

            return {
                "file_id": actual_file_id,
                "chunks_count": len(chunks),
                "status": "embedded"
            }

        except Exception as e:
            logger.error(f"Failed to embed file: {e}")
            raise

    def _get_chunks_by_type(self, text: str, ext: str) -> list:
        """Get text chunks based on file type"""
        if ext == "pdf":
            return split_into_chunks(
                text, size=500, overlap=75,
                seps=["\n\n", "\n", " ", ""]
            )
        elif ext == "docx":
            return split_into_chunks(
                text, size=400, overlap=60,
                seps=["\n\n", "\n", " ", ""]
            )
        else:
            return split_into_chunks(
                text,
                size=settings.DEFAULT_CHUNK_SIZE,
                overlap=settings.DEFAULT_CHUNK_OVERLAP
            )

    def upload_and_get_public_url(self, bucket_name: str, file) -> str:
        """Upload file to Supabase Storage and get public URL"""
        try:
            file_id = str(uuid4())
            file_name = file.name
            folder_path = "/"
            mime_type = file.content_type

            # Upload file
            self.storage_service.upload_file(
                organization_id="public",
                file_id=file_id,
                file_content=file,
                filename=file_name,
                folder_path=folder_path,
                mime_type=mime_type
            )

            # Get public URL
            public_url = self.storage_service.get_public_url(
                organization_id="public",
                file_id=file_id,
                folder_path=folder_path
            )

            return public_url

        except Exception as e:
            logger.error(f"Failed to upload and get public URL: {e}")
            raise

# Singleton instance
_file_manager_service: Optional[FileManagerService] = None


def get_file_manager_service(client: Optional[Client] = None) -> FileManagerService:
    """Get or create FileManagerService singleton"""
    global _file_manager_service
    if _file_manager_service is None:
        _file_manager_service = FileManagerService(client)
    return _file_manager_service
