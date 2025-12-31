"""
Organization Service

Service layer for managing organizations and members in Supabase.
Integrates with ChromaDB for organization-specific document collections.
"""
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from supabase import create_client, Client

from app.config import settings
from app.models.organization import (
    Organization, OrganizationCreate, OrganizationUpdate,
    OrganizationWithOwnership, OrganizationMember,
    OrganizationMemberDetail, UserParent, UserChild,
    AppRole, OrganizationMemberWithRole
)
from app.services.role_service import get_role_service
from app.services.chromadb_service import ChromaDBService

logger = logging.getLogger(__name__)


class OrganizationService:
    """Service for managing organizations in Supabase"""

    def __init__(self):
        """Initialize Supabase client"""
        if not settings.is_supabase_configured:
            logger.warning("Supabase not configured. Organization features will not work.")
            self._client: Optional[Client] = None
        else:
            # Use service role key for backend operations (bypasses RLS)
            supabase_key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY

            if settings.SUPABASE_SERVICE_KEY:
                logger.info("Using Supabase service role key for organizations")
            else:
                logger.warning("Using Supabase anon key - ensure RLS is disabled")

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

    # ============ Organization Operations ============

    async def create_organization(self, org_data: OrganizationCreate) -> str:
        """
        Create a new organization using database function.

        Also creates a dedicated ChromaDB collection for document isolation.

        Args:
            org_data: Organization creation data

        Returns:
            Created organization ID

        Raises:
            RuntimeError: If creation fails
        """
        try:
            # Call database function to create organization
            response = self.client.rpc(
                "create_organization",
                {
                    "p_name": org_data.name,
                    "p_legal_name": org_data.legal_name,
                    "p_category": org_data.category.value,
                    "p_description": org_data.description,
                    "p_owner_id": org_data.owner_id
                }
            ).execute()

            if not response.data:
                raise RuntimeError("Failed to create organization")

            organization_id = response.data
            logger.info(f"Created organization '{org_data.name}' (ID: {organization_id}) for user {org_data.owner_id}")

            # ⭐ Create dedicated ChromaDB collection for this organization
            try:
                chromadb_service = ChromaDBService()
                collection_info = chromadb_service.create_organization_collection(organization_id)
                logger.info(f"Created ChromaDB collection: {collection_info['collection_name']}")
            except Exception as chroma_error:
                logger.error(f"Failed to create ChromaDB collection for organization {organization_id}: {chroma_error}")
                # Don't fail organization creation if ChromaDB fails
                # Collection can be created later if needed
                logger.warning(f"Organization {organization_id} created but ChromaDB collection creation failed")

            return organization_id

        except Exception as e:
            logger.error(f"Error creating organization: {e}")
            raise RuntimeError(f"Failed to create organization: {str(e)}")

    async def get_user_organization(self, user_id: str) -> Optional[OrganizationWithOwnership]:
        """
        Get organization for a specific user.

        Args:
            user_id: UUID of the user

        Returns:
            Organization with ownership flag if found, None otherwise
        """
        try:
            # Call database function to get user's organization
            response = self.client.rpc(
                "get_user_organization",
                {"p_user_id": user_id}
            ).execute()

            if not response.data or len(response.data) == 0:
                return None

            org_data = response.data[0]
            return OrganizationWithOwnership(**org_data)

        except Exception as e:
            logger.error(f"Error fetching organization for user {user_id}: {e}")
            return None

    async def get_organization_by_id(self, org_id: str) -> Optional[Organization]:
        """
        Get organization by ID.

        Args:
            org_id: UUID of the organization

        Returns:
            Organization if found, None otherwise
        """
        try:
            response = self.client.table("organizations").select("*").eq("id", org_id).execute()

            if not response.data or len(response.data) == 0:
                return None

            return Organization(**response.data[0])

        except Exception as e:
            logger.error(f"Error fetching organization {org_id}: {e}")
            return None

    async def update_organization(
        self,
        org_id: str,
        user_id: str,
        org_data: OrganizationUpdate
    ) -> bool:
        """
        Update organization details (owner only).

        Args:
            org_id: UUID of the organization
            user_id: UUID of the user (must be owner)
            org_data: Update data

        Returns:
            True if updated successfully

        Raises:
            RuntimeError: If update fails or user is not owner
        """
        try:
            # Verify user is owner of the organization
            member_check = self.client.table("organization_members").select("is_owner").eq("organization_id", org_id).eq("user_id", user_id).execute()

            if not member_check.data or len(member_check.data) == 0:
                raise RuntimeError("User is not a member of this organization")

            if not member_check.data[0].get("is_owner", False):
                raise RuntimeError("User is not the owner of this organization")

            # Build update payload (only include non-None values)
            update_payload = {}
            if org_data.name is not None:
                update_payload["name"] = org_data.name
            if org_data.legal_name is not None:
                update_payload["legal_name"] = org_data.legal_name
            if org_data.category is not None:
                update_payload["category"] = org_data.category.value
            if org_data.description is not None:
                update_payload["description"] = org_data.description
            if org_data.logo_url is not None:
                update_payload["logo_url"] = org_data.logo_url

            # Always update timestamp
            from datetime import timezone
            update_payload["updated_at"] = datetime.now(timezone.utc).isoformat()

            # Update organization
            self.client.table("organizations").update(update_payload).eq("id", org_id).execute()

            logger.info(f"Updated organization {org_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating organization {org_id}: {e}")
            raise RuntimeError(f"Failed to update organization: {str(e)}")

    async def delete_organization(self, org_id: str, user_id: str) -> bool:
        """
        Delete organization (owner only).

        Args:
            org_id: UUID of the organization
            user_id: UUID of the user (must be owner)

        Returns:
            True if deleted successfully

        Raises:
            RuntimeError: If user is not owner
        """
        try:
            # Verify user is owner
            org = await self.get_user_organization(user_id)
            if not org or org.id != org_id or not org.is_owner:
                raise RuntimeError("User is not the owner of this organization")

            # Delete organization (members will cascade delete)
            response = self.client.table("organizations").delete().eq("id", org_id).execute()

            logger.info(f"Deleted organization {org_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting organization {org_id}: {e}")
            raise RuntimeError(f"Failed to delete organization: {str(e)}")

    # ============ Organization Member Operations ============

    async def get_organization_members(
        self,
        org_id: str,
        user_id: str
    ) -> List[OrganizationMemberDetail]:
        """
        Get all members of an organization.

        Args:
            org_id: UUID of the organization
            user_id: UUID of the requesting user (must be member)

        Returns:
            List of organization members

        Raises:
            RuntimeError: If user is not a member
        """
        try:
            # Verify user is member of organization
            user_org = await self.get_user_organization(user_id)
            if not user_org or user_org.id != org_id:
                raise RuntimeError("User is not a member of this organization")

            # Call database function to get members
            response = self.client.rpc(
                "get_organization_members",
                {"p_org_id": org_id}
            ).execute()

            members = []
            for member_data in response.data:
                # Get user email from auth.users if available
                # For now, returning without email (would need auth.users join)
                members.append(OrganizationMemberDetail(
                    user_id=member_data["user_id"],
                    email=None,  # TODO: Fetch from auth.users
                    joined_at=member_data["joined_at"],
                    is_owner=member_data["is_owner"],
                    role="admin" if member_data["is_owner"] else "user"
                ))

            logger.info(f"Retrieved {len(members)} members for organization {org_id}")
            return members

        except Exception as e:
            logger.error(f"Error fetching members for organization {org_id}: {e}")
            raise RuntimeError(f"Failed to fetch members: {str(e)}")

    async def add_member_to_organization(
        self,
        org_id: str,
        user_id: str
    ) -> bool:
        """
        Add a member to organization.

        Args:
            org_id: UUID of the organization
            user_id: UUID of the user to add

        Returns:
            True if added successfully
        """
        try:
            data = {
                "organization_id": org_id,
                "user_id": user_id,
                "is_owner": False
            }

            response = self.client.table("organization_members").insert(data).execute()

            logger.info(f"Added user {user_id} to organization {org_id}")
            return True

        except Exception as e:
            logger.error(f"Error adding member to organization: {e}")
            return False

    async def add_user_to_parent_organization(
        self,
        user_id: str,
        parent_user_id: str
    ) -> bool:
        """
        Add invited user to parent's organization.

        Args:
            user_id: UUID of the invited user
            parent_user_id: UUID of the parent user

        Returns:
            True if added successfully
        """
        try:
            # Call database function
            self.client.rpc(
                "add_user_to_parent_organization",
                {
                    "p_user_id": user_id,
                    "p_parent_user_id": parent_user_id
                }
            ).execute()

            logger.info(f"Added user {user_id} to parent's organization")
            return True

        except Exception as e:
            logger.error(f"Error adding user to parent organization: {e}")
            return False

    async def get_organization_members_with_roles(
        self,
        org_id: str,
        user_id: str
    ) -> List[OrganizationMemberWithRole]:
        """
        Get all members of an organization with their roles.

        Args:
            org_id: UUID of the organization
            user_id: UUID of the requesting user (must be member)

        Returns:
            List of organization members with role information

        Raises:
            RuntimeError: If user is not a member
        """
        try:
            # Verify user is member of organization
            user_org = await self.get_user_organization(user_id)
            if not user_org or user_org.id != org_id:
                raise RuntimeError("User is not a member of this organization")

            # Use role service to get members with roles
            role_service = get_role_service()
            members = await role_service.get_organization_members_with_roles(org_id)

            logger.info(f"Retrieved {len(members)} members with roles for organization {org_id}")
            return members

        except Exception as e:
            logger.error(f"Error fetching members with roles for organization {org_id}: {e}")
            raise RuntimeError(f"Failed to fetch members with roles: {str(e)}")

    # ============ User Hierarchy Operations ============

    async def get_user_parent(self, user_id: str) -> Optional[UserParent]:
        """
        Get user's parent (upline).

        Note: Requires user_hierarchy table from existing schema.

        Args:
            user_id: UUID of the user

        Returns:
            User's parent if exists, None otherwise
        """
        try:
            # Query user_hierarchy table
            response = self.client.table("user_hierarchy").select("parent_user_id").eq("child_user_id", user_id).execute()

            if not response.data or len(response.data) == 0:
                return None

            parent_id = response.data[0]["parent_user_id"]

            # TODO: Get parent email from auth.users
            return UserParent(
                parent_id=parent_id,
                email=None
            )

        except Exception as e:
            logger.error(f"Error fetching parent for user {user_id}: {e}")
            return None

    async def get_user_children(self, user_id: str) -> List[UserChild]:
        """
        Get user's children (downlines).

        Note: Requires user_hierarchy table from existing schema.

        Args:
            user_id: UUID of the user

        Returns:
            List of user's children
        """
        try:
            # Query user_hierarchy table
            response = self.client.table("user_hierarchy").select("child_user_id, created_at").eq("parent_user_id", user_id).execute()

            children = []
            for child_data in response.data:
                # TODO: Get child email and role from auth.users
                children.append(UserChild(
                    user_id=child_data["child_user_id"],
                    email=None,
                    created_at=child_data["created_at"],
                    role="user"
                ))

            logger.info(f"Retrieved {len(children)} children for user {user_id}")
            return children

        except Exception as e:
            logger.error(f"Error fetching children for user {user_id}: {e}")
            return []


# Global organization service instance
_organization_service: Optional[OrganizationService] = None


def get_organization_service() -> OrganizationService:
    """Get or create global organization service instance"""
    global _organization_service
    if _organization_service is None:
        _organization_service = OrganizationService()
    return _organization_service
