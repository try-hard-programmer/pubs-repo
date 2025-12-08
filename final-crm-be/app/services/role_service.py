"""
Role Service

Service layer for managing user roles in organizations.
"""
import logging
from typing import List, Optional
from supabase import create_client, Client

from app.config import settings
from app.models.organization import (
    AppRole, UserRole, UserRoleInfo,
    OrganizationMemberWithRole
)

logger = logging.getLogger(__name__)


class RoleService:
    """Service for managing roles in organizations"""

    def __init__(self):
        """Initialize Supabase client"""
        if not settings.is_supabase_configured:
            logger.warning("Supabase not configured. Role features will not work.")
            self._client: Optional[Client] = None
        else:
            # Use service role key for backend operations (bypasses RLS)
            supabase_key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY

            if settings.SUPABASE_SERVICE_KEY:
                logger.info("Using Supabase service role key for roles")
            else:
                logger.warning("Using Supabase anon key for roles - ensure RLS is disabled")

            self._client: Client = create_client(
                settings.SUPABASE_URL,
                supabase_key
            )

    @property
    def client(self) -> Client:
        """Get Supabase client with error handling"""
        if self._client is None:
            raise RuntimeError("Supabase client not initialized. Check configuration.")
        return self._client

    # ============================================================================
    # Role Assignment Operations
    # ============================================================================

    async def assign_role(
        self,
        user_id: str,
        organization_id: str,
        role: AppRole,
        assigned_by: str
    ) -> str:
        """
        Assign role to user in organization.

        Args:
            user_id: UUID of user to assign role to
            organization_id: UUID of organization
            role: Role to assign
            assigned_by: UUID of user assigning the role

        Returns:
            Role ID

        Raises:
            RuntimeError: If assignment fails
        """
        try:
            # Call database function
            response = self.client.rpc(
                "assign_role_in_organization",
                {
                    "p_user_id": user_id,
                    "p_organization_id": organization_id,
                    "p_role": role.value,
                    "p_assigned_by": assigned_by
                }
            ).execute()

            if not response.data:
                raise RuntimeError("Failed to assign role")

            role_id = response.data
            logger.info(f"Assigned {role.value} role to user {user_id} in org {organization_id}")
            return role_id

        except Exception as e:
            logger.error(f"Error assigning role: {e}")
            raise RuntimeError(f"Failed to assign role: {str(e)}")

    async def get_user_role(
        self,
        user_id: str,
        organization_id: str
    ) -> AppRole:
        """
        Get user's role in organization.

        Args:
            user_id: UUID of user
            organization_id: UUID of organization

        Returns:
            User's role (defaults to 'user' if not assigned)
        """
        try:
            response = self.client.rpc(
                "get_user_role_in_organization",
                {
                    "p_user_id": user_id,
                    "p_organization_id": organization_id
                }
            ).execute()

            if not response.data:
                return AppRole.USER

            role_str = response.data
            return AppRole(role_str)

        except Exception as e:
            logger.error(f"Error getting user role: {e}")
            return AppRole.USER

    async def check_permission(
        self,
        user_id: str,
        organization_id: str,
        required_role: AppRole
    ) -> bool:
        """
        Check if user has required permission level.

        Args:
            user_id: UUID of user
            organization_id: UUID of organization
            required_role: Minimum required role

        Returns:
            True if user has permission, False otherwise
        """
        try:
            response = self.client.rpc(
                "check_permission",
                {
                    "p_user_id": user_id,
                    "p_organization_id": organization_id,
                    "p_required_role": required_role.value
                }
            ).execute()

            return bool(response.data)

        except Exception as e:
            logger.error(f"Error checking permission: {e}")
            return False

    async def remove_role(
        self,
        user_id: str,
        organization_id: str,
        removed_by: str
    ) -> bool:
        """
        Remove user's custom role (reverts to default 'user' role).

        Args:
            user_id: UUID of user
            organization_id: UUID of organization
            removed_by: UUID of user removing the role

        Returns:
            True if successful

        Raises:
            RuntimeError: If removal fails
        """
        try:
            response = self.client.rpc(
                "remove_role_from_organization",
                {
                    "p_user_id": user_id,
                    "p_organization_id": organization_id,
                    "p_removed_by": removed_by
                }
            ).execute()

            logger.info(f"Removed custom role from user {user_id} in org {organization_id}")
            return True

        except Exception as e:
            logger.error(f"Error removing role: {e}")
            raise RuntimeError(f"Failed to remove role: {str(e)}")

    # ============================================================================
    # Query Operations
    # ============================================================================

    async def get_user_organizations_with_roles(
        self,
        user_id: str
    ) -> List[UserRoleInfo]:
        """
        Get all organizations user belongs to with their roles.

        Args:
            user_id: UUID of user

        Returns:
            List of organizations with role information
        """
        try:
            response = self.client.rpc(
                "get_user_organizations_with_roles",
                {"p_user_id": user_id}
            ).execute()

            organizations = []
            for org_data in response.data:
                organizations.append(UserRoleInfo(
                    user_id=org_data["user_id"] if "user_id" in org_data else user_id,
                    organization_id=org_data["organization_id"],
                    organization_name=org_data["organization_name"],
                    role=AppRole(org_data["role"]),
                    is_owner=org_data["is_owner"],
                    joined_at=org_data["joined_at"]
                ))

            logger.info(f"Retrieved {len(organizations)} organizations for user {user_id}")
            return organizations

        except Exception as e:
            logger.error(f"Error fetching user organizations with roles: {e}")
            return []

    async def get_organization_members_with_roles(
        self,
        organization_id: str
    ) -> List[OrganizationMemberWithRole]:
        """
        Get all members of organization with their roles.

        Args:
            organization_id: UUID of organization

        Returns:
            List of members with role information
        """
        try:
            # Query organization_members joined with user_roles
            response = self.client.table("organization_members") \
                .select(
                    "user_id, joined_at, is_owner, "
                    "user_roles(role, assigned_by)"
                ) \
                .eq("organization_id", organization_id) \
                .execute()

            members = []
            for member_data in response.data:
                # Get role from joined data
                role_data = member_data.get("user_roles")
                role = AppRole.USER  # Default
                assigned_by = None

                if role_data and len(role_data) > 0:
                    role = AppRole(role_data[0]["role"])
                    assigned_by = role_data[0].get("assigned_by")

                members.append(OrganizationMemberWithRole(
                    user_id=member_data["user_id"],
                    email=None,  # TODO: Fetch from auth.users
                    role=role,
                    is_owner=member_data["is_owner"],
                    joined_at=member_data["joined_at"],
                    assigned_by=assigned_by
                ))

            logger.info(f"Retrieved {len(members)} members with roles for org {organization_id}")
            return members

        except Exception as e:
            logger.error(f"Error fetching organization members with roles: {e}")
            return []

    async def get_role_details(
        self,
        user_id: str,
        organization_id: str
    ) -> Optional[UserRole]:
        """
        Get detailed role information for user in organization.

        Args:
            user_id: UUID of user
            organization_id: UUID of organization

        Returns:
            UserRole object if exists, None otherwise
        """
        try:
            response = self.client.table("user_roles") \
                .select("*") \
                .eq("user_id", user_id) \
                .eq("organization_id", organization_id) \
                .execute()

            if not response.data or len(response.data) == 0:
                return None

            role_data = response.data[0]
            return UserRole(
                id=role_data["id"],
                user_id=role_data["user_id"],
                organization_id=role_data["organization_id"],
                role=AppRole(role_data["role"]),
                assigned_by=role_data.get("assigned_by"),
                created_at=role_data["created_at"],
                updated_at=role_data["updated_at"]
            )

        except Exception as e:
            logger.error(f"Error fetching role details: {e}")
            return None

    # ============================================================================
    # Utility Operations
    # ============================================================================

    async def is_super_admin(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Check if user is super admin in organization"""
        role = await self.get_user_role(user_id, organization_id)
        return role == AppRole.SUPER_ADMIN

    async def is_admin_or_above(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Check if user is admin or super admin"""
        return await self.check_permission(user_id, organization_id, AppRole.ADMIN)

    async def can_manage_roles(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Check if user can manage roles (admin or super_admin)"""
        return await self.is_admin_or_above(user_id, organization_id)


# Global role service instance
_role_service: Optional[RoleService] = None


def get_role_service() -> RoleService:
    """Get or create global role service instance"""
    global _role_service
    if _role_service is None:
        _role_service = RoleService()
    return _role_service
