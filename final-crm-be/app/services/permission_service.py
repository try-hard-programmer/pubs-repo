"""
Permission Service
Handles permission checking and access control for file manager
"""
from typing import Optional, List, Dict, Any, Tuple
from supabase import Client
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class PermissionService:
    """Service for checking file/folder permissions"""

    def __init__(self, supabase_client: Optional[Client] = None):
        """
        Initialize Permission Service

        Args:
            supabase_client: Supabase client for database operations
        """
        from supabase import create_client

        # Use SERVICE_ROLE_KEY to bypass RLS for permission checks
        self.client = supabase_client or create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY  # ← Use service role key
        )

    # =====================================================
    # PERMISSION CHECKING
    # =====================================================

    def check_permission(
        self,
        user_id: str,
        file_id: str,
        required_permission: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if user has specific permission on file/folder

        Permission Hierarchy:
        1. Owner (created_by == user_id) → ALL permissions
        2. Explicit Share (file_shares) → Granted permission
        3. Group Permission (group_permissions) → Group permission
        4. Organization Admin → ALL permissions
        5. No Access → False

        Args:
            user_id: User UUID
            file_id: File or folder UUID
            required_permission: Permission to check (view, edit, delete, share, manage)

        Returns:
            Tuple of (has_permission: bool, reason: str)
        """
        try:
            # 1. Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            if not file_response.data:
                return False, "File not found"

            file_data = file_response.data[0]

            # 2. Check if user is owner
            if file_data.get("created_by") == user_id or file_data.get("user_id") == user_id:
                return True, "owner"

            # 3. Check if user is organization admin
            is_admin = self._is_organization_admin(user_id, file_data["organization_id"])
            if is_admin:
                return True, "admin"

            # 4. Check explicit file share
            share_response = self.client.table("file_shares")\
                .select("access_level")\
                .eq("file_id", file_id)\
                .eq("shared_with_user_id", user_id)\
                .execute()

            if share_response.data:
                share = share_response.data[0]
                access_level = share["access_level"]

                if self._has_required_permission(access_level, required_permission):
                    return True, f"share ({access_level})"

            # 5. Check group permissions
            group_response = self.client.table("group_permissions")\
                .select("permission, groups!inner(*)")\
                .eq("file_id", file_id)\
                .execute()

            if group_response.data:
                for group_perm in group_response.data:
                    # Check if user is member of this group
                    group_id = group_perm["groups"]["id"]
                    is_member = self._is_group_member(user_id, group_id)

                    if is_member:
                        permission_level = group_perm["permission"]
                        if self._has_required_permission(permission_level, required_permission):
                            return True, f"group ({permission_level})"

            # 6. No access
            return False, "no_access"

        except Exception as e:
            logger.error(f"Permission check failed: {e}")
            return False, f"error: {str(e)}"

    def get_user_permissions(
        self,
        user_id: str,
        file_id: str
    ) -> Dict[str, Any]:
        """
        Get all permissions user has on file

        Args:
            user_id: User UUID
            file_id: File or folder UUID

        Returns:
            Dict with permissions list, is_owner, is_admin
        """
        try:
            # Get file data
            file_response = self.client.table("files")\
                .select("*")\
                .eq("id", file_id)\
                .execute()

            if not file_response.data:
                return {
                    "file_id": file_id,
                    "permissions": [],
                    "is_owner": False,
                    "is_admin": False,
                    "reason": "file_not_found"
                }

            file_data = file_response.data[0]

            # Check owner
            is_owner = file_data.get("created_by") == user_id or file_data.get("user_id") == user_id

            # Check admin
            is_admin = self._is_organization_admin(user_id, file_data["organization_id"])

            # If owner or admin, grant all permissions
            if is_owner or is_admin:
                return {
                    "file_id": file_id,
                    "permissions": ["view", "edit", "delete", "share", "manage"],
                    "is_owner": is_owner,
                    "is_admin": is_admin,
                    "reason": "owner" if is_owner else "admin"
                }

            # Check explicit shares
            permissions_set = set()
            reason = None

            share_response = self.client.table("file_shares")\
                .select("access_level")\
                .eq("file_id", file_id)\
                .eq("shared_with_user_id", user_id)\
                .execute()

            if share_response.data:
                access_level = share_response.data[0]["access_level"]
                permissions_set.update(self._get_permissions_for_level(access_level))
                reason = f"share ({access_level})"

            # Check group permissions
            group_response = self.client.table("group_permissions")\
                .select("permission, groups!inner(id)")\
                .eq("file_id", file_id)\
                .execute()

            if group_response.data:
                for group_perm in group_response.data:
                    group_id = group_perm["groups"]["id"]
                    is_member = self._is_group_member(user_id, group_id)

                    if is_member:
                        permission_level = group_perm["permission"]
                        permissions_set.update(self._get_permissions_for_level(permission_level))
                        if not reason:
                            reason = f"group ({permission_level})"

            return {
                "file_id": file_id,
                "permissions": sorted(list(permissions_set)),
                "is_owner": False,
                "is_admin": False,
                "reason": reason or "no_access"
            }

        except Exception as e:
            logger.error(f"Get permissions failed: {e}")
            return {
                "file_id": file_id,
                "permissions": [],
                "is_owner": False,
                "is_admin": False,
                "error": str(e)
            }

    def can_share_file(
        self,
        user_id: str,
        file_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if user can share file with others

        Args:
            user_id: User UUID
            file_id: File UUID

        Returns:
            Tuple of (can_share: bool, reason: str)
        """
        return self.check_permission(user_id, file_id, "share")

    def can_manage_file(
        self,
        user_id: str,
        file_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if user can manage file permissions

        Args:
            user_id: User UUID
            file_id: File UUID

        Returns:
            Tuple of (can_manage: bool, reason: str)
        """
        return self.check_permission(user_id, file_id, "manage")

    # =====================================================
    # HELPER METHODS
    # =====================================================

    def _is_organization_admin(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Check if user is admin in organization"""
        try:
            response = self.client.table("user_roles")\
                .select("role")\
                .eq("user_id", user_id)\
                .eq("organization_id", organization_id)\
                .in_("role", ["admin", "super_admin"])\
                .execute()

            return bool(response.data)

        except Exception as e:
            logger.error(f"Admin check failed: {e}")
            return False

    def _is_group_member(
        self,
        user_id: str,
        group_id: str
    ) -> bool:
        """Check if user is member of group"""
        try:
            response = self.client.table("group_members")\
                .select("id")\
                .eq("user_id", user_id)\
                .eq("group_id", group_id)\
                .execute()

            return bool(response.data)

        except Exception as e:
            logger.error(f"Group membership check failed: {e}")
            return False

    def _has_required_permission(
        self,
        granted_level: str,
        required_permission: str
    ) -> bool:
        """
        Check if granted permission level includes required permission

        Permission hierarchy:
        - view: can view only
        - edit: can view + edit
        - delete: can view + edit + delete
        - share: can view + edit + delete + share
        - manage: can view + edit + delete + share + manage

        Args:
            granted_level: Granted permission level
            required_permission: Required permission

        Returns:
            True if granted level includes required permission
        """
        permission_hierarchy = {
            "view": ["view"],
            "edit": ["view", "edit"],
            "delete": ["view", "edit", "delete"],
            "share": ["view", "edit", "delete", "share"],
            "manage": ["view", "edit", "delete", "share", "manage"]
        }

        granted_permissions = permission_hierarchy.get(granted_level, [])
        return required_permission in granted_permissions

    def _get_permissions_for_level(self, level: str) -> List[str]:
        """Get list of permissions for a permission level"""
        permission_hierarchy = {
            "view": ["view"],
            "edit": ["view", "edit"],
            "delete": ["view", "edit", "delete"],
            "share": ["view", "edit", "delete", "share"],
            "manage": ["view", "edit", "delete", "share", "manage"]
        }

        return permission_hierarchy.get(level, [])


# Singleton instance
_permission_service: Optional[PermissionService] = None


def get_permission_service(client: Optional[Client] = None) -> PermissionService:
    """Get or create PermissionService singleton"""
    global _permission_service
    if _permission_service is None:
        _permission_service = PermissionService(client)
    return _permission_service
