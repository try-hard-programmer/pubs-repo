"""
Sharing Service
Handles file/folder sharing with users, groups, and public links
"""
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from uuid import uuid4
import secrets
import logging
from supabase import Client

from app.config import settings
from app.services.email_service import EmailService
from app.services.permission_service import get_permission_service

logger = logging.getLogger(__name__)


class SharingService:
    """Service for managing file shares"""

    def __init__(self, supabase_client: Optional[Client] = None):
        """
        Initialize Sharing Service

        Args:
            supabase_client: Supabase client for database operations
        """
        from supabase import create_client

        # Use SERVICE_ROLE_KEY to bypass RLS for sharing operations
        self.client = supabase_client or create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY  # ← Use service role key
        )
        
        self.permission_service = get_permission_service(self.client)
    
     # Helper method (tambahkan di class)
    async def _send_share_email(self, share_record: Dict, file_data: Dict, shared_by_email: str, note: Optional[str] = None):
        """Send notification email after successful share"""
        email_service = EmailService()
                
        # Generate share URL
        share_id = share_record["id"]
        file_name = file_data.get("name", "Unnamed file")
        org_slug = file_data.get("organization_slug", "app")
                            
        await email_service.send_share_notification(
                to_email=share_record["shared_with_email"],
                shared_by_email=shared_by_email,
                file_name=file_name,
                permission=share_record["access_level"],
                note=note,
                shared_by_name=file_data.get("shared_by_name")  # Optional
            )



    # =====================================================
    # USER SHARING
    # =====================================================


    def share_with_user(
        self,
        file_id: str,
        shared_by: str,
        target_email: str,
        permission: str = "view",
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Share file/folder with specific user

        Args:
            file_id: File or folder UUID
            shared_by: User UUID who is sharing
            target_email: Email of user to share with
            permission: Permission level (view, edit, delete, share, manage)
            expires_at: Optional expiration datetime
            metadata: Optional metadata

        Returns:
            Created share data

        Raises:
            Exception: If sharing fails
        """
        try:
            # 1. Check if sharer has permission to share
            can_share, reason = self.permission_service.can_share_file(shared_by, file_id)
            if not can_share:
                raise Exception(f"No permission to share this file: {reason}")

            # 2. Check Email Registered
            resp = self.client.rpc("get_user_by_email", {"p_email": target_email}).execute()
            rows = getattr(resp, "data", None) or []

            if not rows:
                raise Exception(f"Email not registered: {target_email}")

            # 3. Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            file_data = file_response.data[0]
            get_user_by_id = self.client.rpc("get_auth_user_by_id", {"p_user_id": file_data.get("user_id")}).execute()
            users = getattr(get_user_by_id, "data", None) or []

            if file_data["is_starred"]:
                file_response = self.client.table("files")\
                .update({"is_starred": False})\
                .eq("id", file_id)\
                .execute()
            
             # 4. Lookup target user by email
            resp = self.client.rpc("get_user_by_email", {"p_email": target_email}).execute()
            rows = getattr(resp, "data", None) or []
            target_user_id = rows[0]["id"] if rows else None

            if target_user_id == file_data["user_id"]:
                raise Exception("Cannot share file with self")

            # 5. Check if share already exists
            existing_share = self.client.table("file_shares")\
                .select("*")\
                .eq("file_id", file_id)\
                .eq("shared_with_email", target_email)\
                .execute()

            if existing_share.data:
                # Update existing share
                share_id = existing_share.data[0]["id"]
                update_data = {
                    "access_level": permission,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "metadata": metadata or {},
                    "updated_at": datetime.utcnow().isoformat()
                }

                response = self.client.table("file_shares")\
                    .update(update_data)\
                    .eq("id", share_id)\
                    .execute()

                logger.info(f"✅ Updated share: {share_id}")
                return response.data[0]

            # 6. Create new share
            share_data = {
                "id": str(uuid4()),
                "file_id": file_id,
                "shared_by": shared_by,
                "shared_with_email": target_email,
                "shared_with_user_id": target_user_id,
                "share_type": "user",
                "access_level": permission,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "metadata": metadata or {},
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("file_shares").insert(share_data).execute()

            if not response.data:
                raise Exception("Failed to create share")

            # 7. SEND NOTIFICATION EMAIL
            try:
                asyncio.create_task(
                    self._send_share_email(response.data[0], file_data, users[0].get("email"), metadata.get("note"))
                )
                logger.info(f"✅ Email sent to {target_email}")
            except Exception as email_error:
                logger.warning(f"Failed to send email to {target_email}: {email_error}")
                # DON'T FAIL THE SHARE OPERATION!

            logger.info(f"✅ Shared file {file_id} with {target_email}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to share with user: {e}")
            raise

    # =====================================================
    # GROUP SHARING
    # =====================================================

    def share_with_group(
        self,
        file_id: str,
        shared_by: str,
        group_id: str,
        permission: str = "view",
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Share file/folder with group

        Args:
            file_id: File or folder UUID
            shared_by: User UUID who is sharing
            group_id: Group UUID
            permission: Permission level (view, edit, delete, share, manage)
            metadata: Optional metadata

        Returns:
            Created group permission data

        Raises:
            Exception: If sharing fails
        """
        try:
            # 1. Check if sharer has permission to share
            can_share, reason = self.permission_service.can_share_file(shared_by, file_id)
            if not can_share:
                raise Exception(f"No permission to share this file: {reason}")

            # 2. Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            # 3. Verify group exists
            group_response = self.client.table("groups")\
                .select("*")\
                .eq("id", group_id)\
                .execute()

            if not group_response.data:
                raise Exception("Group not found")

            # 4. Check if group permission already exists
            existing_perm = self.client.table("group_permissions")\
                .select("*")\
                .eq("file_id", file_id)\
                .eq("group_id", group_id)\
                .execute()

            if existing_perm.data:
                # Update existing permission
                perm_id = existing_perm.data[0]["id"]
                update_data = {
                    "permission": permission,
                    "updated_at": datetime.utcnow().isoformat()
                }

                response = self.client.table("group_permissions")\
                    .update(update_data)\
                    .eq("id", perm_id)\
                    .execute()

                logger.info(f"✅ Updated group permission: {perm_id}")
                return response.data[0]

            # 5. Create new group permission
            perm_data = {
                "id": str(uuid4()),
                "file_id": file_id,
                "group_id": group_id,
                "permission": permission,
                "granted_by": shared_by,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("group_permissions").insert(perm_data).execute()

            if not response.data:
                raise Exception("Failed to create group permission")

            logger.info(f"✅ Shared file {file_id} with group {group_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to share with group: {e}")
            raise

    # =====================================================
    # PUBLIC SHARING
    # =====================================================

    def create_public_share(
        self,
        file_id: str,
        created_by: str,
        permission: str = "view",
        expires_in_hours: int = 24,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Create public share link for file

        Args:
            file_id: File UUID
            created_by: User UUID who is creating link
            permission: Permission level (usually 'view')
            expires_in_hours: Hours until link expires (default 24)
            metadata: Optional metadata

        Returns:
            Share data with public URL

        Raises:
            Exception: If share creation fails
        """
        try:
            # 1. Check if creator has permission to share
            can_share, reason = self.permission_service.can_share_file(created_by, file_id)
            if not can_share:
                raise Exception(f"No permission to share this file: {reason}")

            # 2. Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            if not file_response.data:
                raise Exception("File not found")

            # 3. Generate secure token
            share_token = secrets.token_urlsafe(32)

            # 4. Calculate expiration
            expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

            # 5. Create share record
            share_data = {
                "id": str(uuid4()),
                "file_id": file_id,
                "shared_by": created_by,
                "shared_with_email": None,
                "shared_with_user_id": None,
                "share_type": "public",
                "share_token": share_token,
                "access_level": permission,
                "expires_at": expires_at.isoformat(),
                "metadata": metadata or {},
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }

            response = self.client.table("file_shares").insert(share_data).execute()

            if not response.data:
                raise Exception("Failed to create public share")

            share = response.data[0]

            # 6. Generate public URL
            base_url = getattr(settings, 'PUBLIC_SHARE_BASE_URL', 'https://api.syntra.id')
            share_url = f"{base_url}/filemanager/public/{share_token}"

            logger.info(f"✅ Created public share for file {file_id}")

            return {
                **share,
                "share_url": share_url
            }

        except Exception as e:
            logger.error(f"Failed to create public share: {e}")
            raise

    def get_public_share(
        self,
        share_token: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get public share by token

        Args:
            share_token: Share token

        Returns:
            Share data with file info, or None if not found/expired
        """
        try:
            # Get share
            response = self.client.table("file_shares")\
                .select("*, files(*)")\
                .eq("share_token", share_token)\
                .eq("share_type", "public")\
                .execute()

            if not response.data:
                return None

            share = response.data[0]

            # Check expiration
            if share.get("expires_at"):
                expires_at = datetime.fromisoformat(share["expires_at"].replace('Z', '+00:00'))
                if datetime.utcnow() > expires_at.replace(tzinfo=None):
                    logger.warning(f"Public share expired: {share_token}")
                    return None

            return share

        except Exception as e:
            logger.error(f"Failed to get public share: {e}")
            return None

    # =====================================================
    # SHARE MANAGEMENT
    # =====================================================

    def revoke_share(
        self,
        share_id: str,
        revoked_by: str
    ) -> Dict[str, str]:
        """
        Revoke file share

        Args:
            share_id: Share UUID
            revoked_by: User UUID who is revoking

        Returns:
            Revocation result

        Raises:
            Exception: If revocation fails
        """
        try:
            # Get share
            share_response = self.client.table("file_shares")\
                .select("*")\
                .eq("id", share_id)\
                .execute()

            if not share_response.data:
                raise Exception("Share not found")

            share = share_response.data[0]

            # Check if revoker has permission
            can_manage, reason = self.permission_service.can_manage_file(
                revoked_by,
                share["file_id"]
            )

            if not can_manage and share["shared_by"] != revoked_by:
                raise Exception(f"No permission to revoke this share: {reason}")

            # Delete share
            self.client.table("file_shares").delete().eq("id", share_id).execute()

            logger.info(f"✅ Revoked share: {share_id}")

            return {
                "share_id": share_id,
                "status": "revoked"
            }

        except Exception as e:
            logger.error(f"Failed to revoke share: {e}")
            raise

    def update_share(
        self,
        share_id: str,
        updated_by: str,
        access_level: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Update file share

        Args:
            share_id: Share UUID
            updated_by: User UUID who is updating
            access_level: New permission level
            expires_at: New expiration datetime
            metadata: New metadata

        Returns:
            Updated share data

        Raises:
            Exception: If update fails
        """
        try:
            # Get share
            share_response = self.client.table("file_shares")\
                .select("*")\
                .eq("id", share_id)\
                .execute()

            if not share_response.data:
                raise Exception("Share not found")

            share = share_response.data[0]

            # Check if updater has permission
            can_manage, reason = self.permission_service.can_manage_file(
                updated_by,
                share["file_id"]
            )

            if not can_manage and share["shared_by"] != updated_by:
                raise Exception(f"No permission to update this share: {reason}")

            # Build update data
            update_data = {
                "updated_at": datetime.utcnow().isoformat()
            }

            if access_level:
                update_data["access_level"] = access_level

            if expires_at is not None:
                update_data["expires_at"] = expires_at.isoformat() if expires_at else None

            if metadata is not None:
                update_data["metadata"] = metadata

            # Update share
            response = self.client.table("file_shares")\
                .update(update_data)\
                .eq("id", share_id)\
                .execute()

            if not response.data:
                raise Exception("Failed to update share")

            logger.info(f"✅ Updated share: {share_id}")

            return response.data[0]

        except Exception as e:
            logger.error(f"Failed to update share: {e}")
            raise

    def list_shares(
        self,
        file_id: str,
        user_id: str
    ) -> List[Dict[str, Any]]:
        """
        List all shares for a file

        Args:
            file_id: File UUID
            user_id: User UUID (must have manage permission)

        Returns:
            List of shares

        Raises:
            Exception: If listing fails
        """
        try:
            # Check if user has permission
            can_manage, reason = self.permission_service.can_manage_file(user_id, file_id)
            if not can_manage:
                raise Exception(f"No permission to list shares: {reason}")

            # Get shares
            response = self.client.table("file_shares")\
                .select("*")\
                .eq("file_id", file_id)\
                .order("created_at", desc=True)\
                .execute()

            return response.data if response.data else []

        except Exception as e:
            logger.error(f"Failed to list shares: {e}")
            raise

    def list_shared_with_me(
        self,
        user_id: str,
        user_email: str
    ) -> List[Dict[str, Any]]:
        """
        List files shared with user

        Args:
            user_id: User UUID
            user_email: User email

        Returns:
            List of shared files with share info
        """
        try:
            # Get shares by user_id or email
            response = self.client.table("file_shares")\
                .select("""
                id,
                file_id,
                shared_by,
                shared_with_user_id,
                shared_with_email,
                share_type,
                share_token,
                access_level,
                expires_at,
                created_at,
                updated_at,
                metadata,
                file:files (
                    id, name, organization_id, user_id, folder_id, parent_path, storage_path,
                    size, mime_type, extension, is_folder, is_trashed, is_starred,
                    embedding_status, embedded_at, embedding_error, file_version,
                    created_at, updated_at, created_by, updated_by, metadata
                )
                """)\
                .or_(f"shared_with_user_id.eq.{user_id},shared_with_email.eq.{user_email}")\
                .order("created_at", desc=True)\
                .execute()

            return response.data if response.data else []

        except Exception as e:
            logger.error(f"Failed to list shared files: {e}")
            return []


# Singleton instance
_sharing_service: Optional[SharingService] = None


def get_sharing_service(client: Optional[Client] = None) -> SharingService:
    """Get or create SharingService singleton"""
    global _sharing_service
    if _sharing_service is None:
        _sharing_service = SharingService(client)
    return _sharing_service
