"""
Organization API Endpoints

Provides HTTP endpoints for managing organizations and members.
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
import secrets
import logging

from app.auth.dependencies import get_current_user
from app.models.user import User, GetMeResponse
from app.models.organization import (
    Organization, OrganizationCreate, OrganizationUpdate,
    OrganizationWithOwnership, OrganizationCreateResponse,
    OrganizationMemberListResponse, OrganizationMemberDetail,
    UserParent, UserChild, UserChildrenResponse,
    RoleAssignRequest, RoleAssignResponse, AppRole,
    UserRoleInfo, UserOrganizationsWithRolesResponse,
    OrganizationMembersWithRolesResponse,
    InvitationRequest, InvitationResponse, InvitationData
)
from app.services.organization_service import get_organization_service
from app.services.role_service import get_role_service
from datetime import datetime
from app.config import settings


import os
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations", tags=["organizations"])


# ============ User Profile Endpoint ============

@router.get(
    "/getme",
    response_model=GetMeResponse,
    summary="Get current user information",
    description="""
    Get detailed information about the currently authenticated user.

    **Returns:**
    - User profile information (ID, email, display name, role)
    - User metadata (additional profile data)
    - Organization information (if user belongs to an organization)
    - Token information (expiration and issued times)

    **Use Cases:**
    - Display user profile in frontend
    - Check if user belongs to an organization
    - Verify organization ownership status
    - Display organization details in UI

    **Authentication:** Requires valid JWT token

    **Example Response:**
    ```json
    {
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
        "is_organization_owner": true,
        "joined_organization_at": "2025-10-10T10:00:00Z",
        "token_expires_at": "2025-12-31T23:59:59Z",
        "token_issued_at": "2025-10-29T10:00:00Z"
    }
    ```

    **Notes:**
    - Organization fields will be null if user doesn't belong to any organization
    - `is_organization_owner` indicates if user is the owner of the organization
    - Token times are in UTC ISO 8601 format
    """
)
async def get_current_user_info(
    current_user: User = Depends(get_current_user)
) -> GetMeResponse:
    """
    Get current user information with organization details.

    Retrieves comprehensive information about the authenticated user including:
    - Basic user profile (ID, email, display name)
    - User metadata from JWT token
    - Organization membership details (if applicable)
    - Token expiration information

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        GetMeResponse: User profile with organization details

    Raises:
        HTTPException: If there's an error fetching organization data
    """
    try:
        org_service = get_organization_service()

        # Get user's organization (if any)
        user_org = await org_service.get_user_organization(current_user.user_id)

        # Prepare organization data
        organization_id = None
        organization_name = None
        organization_category = None
        organization_logo_url = None
        is_organization_owner = None
        joined_organization_at = None

        if user_org:
            organization_id = user_org.id
            organization_name = user_org.name
            organization_category = user_org.category.value if hasattr(user_org.category, 'value') else user_org.category
            organization_logo_url = user_org.logo_url
            is_organization_owner = (user_org.owner_id == current_user.user_id)

            # Get membership join date
            try:
                membership = await org_service.get_user_membership(current_user.user_id, user_org.id)
                if membership:
                    joined_organization_at = membership.joined_at
            except Exception as e:
                logger.warning(f"Failed to fetch membership join date: {e}")

        # Convert token timestamps to datetime
        token_expires_at = None
        token_issued_at = None

        if current_user.exp:
            token_expires_at = datetime.fromtimestamp(current_user.exp)
        if current_user.iat:
            token_issued_at = datetime.fromtimestamp(current_user.iat)

        # Build response
        response = GetMeResponse(
            user_id=current_user.user_id,
            email=current_user.email,
            display_name=current_user.display_name,
            role=current_user.role,
            user_metadata=current_user.user_metadata or {},
            organization_id=organization_id,
            organization_name=organization_name,
            organization_category=organization_category,
            organization_logo_url=organization_logo_url,
            is_organization_owner=is_organization_owner,
            joined_organization_at=joined_organization_at,
            token_expires_at=token_expires_at,
            token_issued_at=token_issued_at
        )

        logger.info(f"GetMe request for user {current_user.user_id} (org: {organization_id})")
        return response

    except Exception as e:
        logger.error(f"Error in getme endpoint: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch user information: {str(e)}"
        )


# ============ Organization Endpoints ============

@router.post("/", response_model=OrganizationCreateResponse, status_code=201)
async def create_organization(
    org_data: OrganizationCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new organization.

    Creates a business/organization and automatically:
    - Adds the creator as the organization owner
    - Assigns Super Admin role (handled by database)

    **Requirements:**
    - User must not already own an organization
    - Business name is required
    - Business category is required

    **Authentication:** Requires valid JWT token

    Args:
        org_data: Organization creation data
        current_user: Authenticated user from JWT token

    Returns:
        Created organization ID

    Raises:
        400: If user already owns an organization
        500: If creation fails

    Example:
        ```json
        {
            "name": "Acme Corporation",
            "legal_name": "Acme Corporation Inc.",
            "category": "technology",
            "description": "Leading tech solutions",
            "owner_id": "user-uuid"
        }
        ```
    """
    try:
        # Verify owner_id matches current user
        if org_data.owner_id != current_user.user_id:
            raise HTTPException(
                status_code=403,
                detail="Cannot create organization for another user"
            )

        # Check if user already has organization
        existing_org = await get_organization_service().get_user_organization(current_user.user_id)
        if existing_org:
            raise HTTPException(
                status_code=400,
                detail="User already owns an organization"
            )

        # Create organization
        org_id = await get_organization_service().create_organization(org_data)

        logger.info(f"Organization created: {org_id} by user {current_user.email}")

        return OrganizationCreateResponse(
            organization_id=org_id,
            message="Organization created successfully"
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Failed to create organization: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error creating organization: {e}")
        raise HTTPException(status_code=500, detail="Failed to create organization")


@router.get("/check-status")
async def check_user_organization_status(
    current_user: User = Depends(get_current_user)
):
    """
    Check if user needs to create organization.

    Returns user's organization status and whether they need to create
    a business/organization. This endpoint should be called after login
    to determine the next step for the user.

    **Logic:**
    - If user has organization: return organization data with needs_organization=false
    - If user has parent (invited): return needs_organization=false (will be added to parent org)
    - If user has no organization and no parent: return needs_organization=true

    **Authentication:** Requires valid JWT token

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        User organization status

    Example Response (needs organization):
        ```json
        {
            "has_organization": false,
            "has_parent": false,
            "needs_organization": true,
            "organization": null,
            "message": "User needs to create organization"
        }
        ```

    Example Response (has organization):
        ```json
        {
            "has_organization": true,
            "has_parent": false,
            "needs_organization": false,
            "organization": {...},
            "message": "User has organization"
        }
        ```
    """
    try:
        # Check if user has organization
        org = await get_organization_service().get_user_organization(current_user.user_id)
        has_organization = org is not None

        # Check if user has parent (was invited)
        parent = await get_organization_service().get_user_parent(current_user.user_id)
        has_parent = parent is not None

        # User needs to create organization if they don't have one and weren't invited
        needs_organization = not has_organization and not has_parent

        response = {
            "has_organization": has_organization,
            "has_parent": has_parent,
            "needs_organization": needs_organization,
            "organization": org,
        }

        if needs_organization:
            response["message"] = "User needs to create organization"
        elif has_organization:
            response["message"] = "User has organization"
        else:
            response["message"] = "User was invited, will be added to parent organization"

        logger.info(
            f"User {current_user.email} status: "
            f"has_org={has_organization}, has_parent={has_parent}, needs_org={needs_organization}"
        )

        return response

    except Exception as e:
        logger.error(f"Error checking organization status for user {current_user.email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to check organization status")


@router.get("/me", response_model=Optional[OrganizationWithOwnership])
async def get_my_organization(
    current_user: User = Depends(get_current_user)
):
    """
    Get current user's organization.

    Returns the organization that the current user belongs to, along with
    a flag indicating if they are the owner.

    **Authentication:** Requires valid JWT token

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        User's organization with ownership flag, or null if user has no organization

    Example Response:
        ```json
        {
            "id": "org-uuid",
            "name": "Acme Corporation",
            "legal_name": "Acme Corporation Inc.",
            "category": "technology",
            "description": "Leading tech solutions",
            "logo_url": null,
            "owner_id": "user-uuid",
            "created_at": "2025-10-10T10:00:00Z",
            "updated_at": "2025-10-10T10:00:00Z",
            "is_active": true,
            "metadata": {},
            "is_owner": true
        }
        ```
    """
    try:
        org = await get_organization_service().get_user_organization(current_user.user_id)
        return org

    except Exception as e:
        logger.error(f"Error fetching organization for user {current_user.email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch organization")


@router.get("/{org_id}", response_model=Organization)
async def get_organization(
    org_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get organization by ID.

    **Requirements:**
    - User must be a member of the organization

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        current_user: Authenticated user from JWT token

    Returns:
        Organization details

    Raises:
        403: If user is not a member
        404: If organization not found
    """
    try:
        # Verify user is member
        user_org = await get_organization_service().get_user_organization(current_user.user_id)
        if not user_org or user_org.id != org_id:
            raise HTTPException(
                status_code=403,
                detail="User is not a member of this organization"
            )

        # Get organization
        org = await get_organization_service().get_organization_by_id(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        return org

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching organization {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch organization")


@router.patch("/{org_id}", response_model=Organization)
async def update_organization(
    org_id: str,
    org_data: OrganizationUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update organization details.

    **Requirements:**
    - User must be the organization owner

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        org_data: Update data (all fields optional)
        current_user: Authenticated user from JWT token

    Returns:
        Updated organization

    Raises:
        403: If user is not the owner
        404: If organization not found
    """
    try:
        # Update organization
        await get_organization_service().update_organization(
            org_id=org_id,
            user_id=current_user.user_id,
            org_data=org_data
        )

        # Return updated organization
        org = await get_organization_service().get_organization_by_id(org_id)
        return org

    except RuntimeError as e:
        logger.error(f"Failed to update organization: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error updating organization: {e}")
        raise HTTPException(status_code=500, detail="Failed to update organization")


@router.delete("/{org_id}", status_code=204)
async def delete_organization(
    org_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete organization.

    **Warning:** This is irreversible. All members will be removed.

    **Requirements:**
    - User must be the organization owner

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        current_user: Authenticated user from JWT token

    Returns:
        204 No Content on success

    Raises:
        403: If user is not the owner
        404: If organization not found
    """
    try:
        await get_organization_service().delete_organization(org_id, current_user.user_id)
        logger.info(f"Organization {org_id} deleted by user {current_user.email}")
        return None

    except RuntimeError as e:
        logger.error(f"Failed to delete organization: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error deleting organization: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete organization")


# ============ Invitation Endpoints ============

@router.post("/invite", response_model=InvitationResponse)
async def invite_user(
    invite_data: InvitationRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Robust Invitation: Sends Clean Link (No Supabase Wrapper), SMTP Sending.
    """
    request_id = secrets.token_hex(4)
    logger.info(f"[{request_id}] 🚀 Starting invitation process for {invite_data.email}")

    try:
        # 1. Security & Context Resolution
        inviter_id = current_user.user_id
        target_org_id = invite_data.organization_id
        
        # Auto-detect Org ID
        if not target_org_id:
            logger.info(f"[{request_id}] 🔍 No organization_id provided. Inferring from current user...")
            user_org = await get_organization_service().get_user_organization(current_user.user_id)
            if user_org:
                target_org_id = user_org.id
            else:
                raise HTTPException(status_code=400, detail="You must belong to an organization to send invitations.")

        client = get_organization_service().client

        # 2. Permission Check
        perm_check = client.table("organization_members")\
            .select("user_id")\
            .eq("user_id", inviter_id)\
            .eq("organization_id", target_org_id)\
            .execute()
        
        if not perm_check.data:
             raise HTTPException(status_code=403, detail="You are not a member of this organization.")

        # 3. Pre-check Profile
        try:
            profile_check = client.table("profiles").select("id").eq("email", invite_data.email).execute()
            if profile_check.data:
                raise HTTPException(status_code=409, detail=f"User {invite_data.email} is already registered.")
        except HTTPException: raise
        except: pass

        # 4. Prepare Database Record
        token = secrets.token_urlsafe(32)
        db_data = {
            "invited_email": invite_data.email,
            "invited_by": inviter_id,
            # "organization_id": target_org_id,  <-- REMOVED to prevent DB crash
            "invitation_token": token,
            "status": "pending"
        }
        
        logger.info(f"[{request_id}] 💾 Inserting record into 'user_invitations'...")
        response = client.table("user_invitations").insert(db_data).execute()
        
        if not response.data:
            raise RuntimeError("Failed to create invitation record")
        
        invitation_id = response.data[0]["id"]
        
        # 5. Generate Link & Send Email
        logger.info(f"[{request_id}] 🔗 Generating Invitation Link...")
        
        try:
            base_url = settings.INVITATION_URL.rstrip('/')
            
            # [FIXED] This is the Clean URL we will send in the email
            redirect_url = f"{base_url}/accept-invitation?token={token}"
            
            # Call Supabase to register the user (creates auth entry), but ignore the link it returns
            client.auth.admin.generate_link({
                "type": "invite",
                "email": invite_data.email,
                "options": {
                    "redirect_to": redirect_url,
                    "data": { 
                        "custom_invitation_id": invitation_id,
                        "organization_id": target_org_id, 
                        "inviter_name": current_user.display_name or current_user.email
                    }
                }
            })
            
            # We don't use the Supabase link. We use our clean 'redirect_url'
            final_link = redirect_url

            logger.info(f"[{request_id}] 📧 Sending Email via SMTP...")
            
            # --- SMTP EMAIL SENDING ---
            smtp_host = os.getenv("SMTP_HOST")
            smtp_port = os.getenv("SMTP_PORT")
            smtp_user = os.getenv("SMTP_USER")
            smtp_pass = os.getenv("SMTP_PASS")
            from_email = os.getenv("SMTP_FROM_EMAIL")
            from_name = os.getenv("SMTP_FROM_NAME", "Syntra")

            if not all([smtp_host, smtp_port, smtp_user, smtp_pass, from_email]):
                 logger.error("SMTP Configuration missing")
            else:
                inviter_name = current_user.display_name or current_user.email
                subject = "You have been invited to join Syntra"
                
                html_body = f"""
                <!DOCTYPE html>
                <html>
                <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
                    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; padding: 30px; border-radius: 8px;">
                        <h2 style="color: #333; text-align: center;">Invitation to Collaborate</h2>
                        <p style="color: #666; font-size: 16px; text-align: center;">
                            <strong>{inviter_name}</strong> has invited you to join them on Syntra.
                        </p>
                        <div style="text-align: center; margin: 30px 0;">
                            <a href="{final_link}" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">Accept Invitation</a>
                        </div>
                        <p style="color: #999; font-size: 12px; text-align: center;">
                            If the button doesn't work, copy this link:<br>
                            <a href="{final_link}" style="color: #007bff; word-break: break-all;">{final_link}</a>
                        </p>
                    </div>
                </body>
                </html>
                """
                
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = f"{from_name} <{from_email}>"
                msg["To"] = invite_data.email
                msg.attach(MIMEText(html_body, "html"))
                
                smtp = aiosmtplib.SMTP(hostname=smtp_host, port=int(smtp_port), use_tls=False)
                await smtp.connect()
                # await smtp.starttls() 
                await smtp.login(smtp_user, smtp_pass)
                await smtp.send_message(msg)
                await smtp.quit()
                logger.info(f"[{request_id}] ✅ Email sent successfully")

        except Exception as auth_error:
            logger.error(f"[{request_id}] ⚠️ Email/Link Error: {auth_error}")
            # Even if email fails, we return the link to the user
            if 'final_link' not in locals():
                try:
                    client.table("user_invitations").delete().eq("id", invitation_id).execute()
                except: pass
                raise HTTPException(status_code=500, detail=f"Failed to process invitation: {str(auth_error)}")

        return InvitationResponse(
            success=True,
            message="Invitation processed.",
            data=InvitationData(
                invitation_id=invitation_id,
                invitation_link=final_link # Returns the Clean Link
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{request_id}] 💥 Critical Failure: {e}")
        raise HTTPException(status_code=500, detail=str(e))
            
# ============ Organization Member Endpoints ============

@router.get("/{org_id}/members", response_model=OrganizationMemberListResponse)
async def get_organization_members(
    org_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get all members of an organization.

    **Requirements:**
    - User must be a member of the organization

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        current_user: Authenticated user from JWT token

    Returns:
        List of organization members with details

    Raises:
        403: If user is not a member
        404: If organization not found

    Example Response:
        ```json
        {
            "members": [
                {
                    "user_id": "user-uuid",
                    "email": "user@example.com",
                    "joined_at": "2025-10-10T10:00:00Z",
                    "is_owner": true,
                    "role": "admin"
                }
            ],
            "total": 1,
            "organization": {...}
        }
        ```
    """
    try:
        # Get organization
        org = await get_organization_service().get_organization_by_id(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Get members
        members = await get_organization_service().get_organization_members(org_id, current_user.user_id)

        return OrganizationMemberListResponse(
            members=members,
            total=len(members),
            organization=org
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Failed to fetch members: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error fetching members: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch members")


# ============ User Hierarchy Endpoints ============

@router.get("/users/parent", response_model=Optional[UserParent])
async def get_user_parent(
    current_user: User = Depends(get_current_user)
):
    """
    Get current user's parent (upline).

    Used to determine if user was invited by another user.

    **Authentication:** Requires valid JWT token

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        User's parent if exists, null otherwise

    Example Response:
        ```json
        {
            "parent_id": "parent-uuid",
            "email": "parent@example.com"
        }
        ```
    """
    try:
        parent = await get_organization_service().get_user_parent(current_user.user_id)
        return parent

    except Exception as e:
        logger.error(f"Error fetching parent for user {current_user.email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch parent")


@router.get("/users/children", response_model=UserChildrenResponse)
async def get_user_children(
    current_user: User = Depends(get_current_user)
):
    """
    Get current user's children (downlines).

    Returns all users invited by the current user.

    **Authentication:** Requires valid JWT token

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        List of user's children

    Example Response:
        ```json
        {
            "children": [
                {
                    "user_id": "child-uuid",
                    "email": "child@example.com",
                    "created_at": "2025-10-10T10:00:00Z",
                    "role": "user"
                }
            ],
            "total": 1
        }
        ```
    """
    try:
        children = await get_organization_service().get_user_children(current_user.user_id)

        return UserChildrenResponse(
            children=children,
            total=len(children)
        )

    except Exception as e:
        logger.error(f"Error fetching children for user {current_user.email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch children")


# ============ Role Management Endpoints ============

@router.get("/{org_id}/members-with-roles", response_model=OrganizationMembersWithRolesResponse)
async def get_organization_members_with_roles(
    org_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get all members of an organization with their roles.

    **Requirements:**
    - User must be a member of the organization

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        current_user: Authenticated user from JWT token

    Returns:
        List of organization members with role information

    Example Response:
        ```json
        {
            "members": [
                {
                    "user_id": "user-uuid",
                    "email": "user@example.com",
                    "role": "super_admin",
                    "is_owner": true,
                    "joined_at": "2025-10-10T10:00:00Z",
                    "assigned_by": "owner-uuid"
                }
            ],
            "total": 1,
            "organization": {...}
        }
        ```
    """
    try:
        # Get organization
        org = await get_organization_service().get_organization_by_id(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Get members with roles
        members = await get_organization_service().get_organization_members_with_roles(
            org_id,
            current_user.user_id
        )

        return OrganizationMembersWithRolesResponse(
            members=members,
            total=len(members),
            organization=org
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Failed to fetch members with roles: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error fetching members with roles: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch members with roles")


@router.post("/{org_id}/members/{user_id}/role", response_model=RoleAssignResponse)
async def assign_role_to_member(
    org_id: str,
    user_id: str,
    role_data: RoleAssignRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Assign role to a member in organization.

    **Requirements:**
    - User must be admin or super_admin in the organization
    - Cannot assign super_admin role (reserved for owner)
    - Admin cannot assign admin or super_admin roles

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        user_id: UUID of the user to assign role to
        role_data: Role assignment data
        current_user: Authenticated user from JWT token

    Returns:
        Role assignment confirmation

    Raises:
        403: If user doesn't have permission
        400: If role assignment fails

    Example Request:
        ```json
        {
            "user_id": "user-uuid",
            "role": "admin"
        }
        ```
    """
    try:
        # Check if current user has permission to assign roles
        role_service = get_role_service()
        can_assign = await role_service.can_manage_roles(
            current_user.user_id,
            org_id
        )

        if not can_assign:
            raise HTTPException(
                status_code=403,
                detail="Only admin or super_admin can assign roles"
            )

        # Assign role
        role_id = await role_service.assign_role(
            user_id=role_data.user_id,
            organization_id=org_id,
            role=role_data.role,
            assigned_by=current_user.user_id
        )

        logger.info(f"Assigned {role_data.role} role to user {user_id} in org {org_id}")

        return RoleAssignResponse(
            role_id=role_id,
            message=f"Role {role_data.role.value} assigned successfully"
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Failed to assign role: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error assigning role: {e}")
        raise HTTPException(status_code=500, detail="Failed to assign role")


@router.get("/{org_id}/members/{user_id}/role", response_model=AppRole)
async def get_member_role(
    org_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a member's role in organization.

    **Requirements:**
    - User must be a member of the organization

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        user_id: UUID of the user
        current_user: Authenticated user from JWT token

    Returns:
        User's role in organization

    Example Response:
        ```json
        "admin"
        ```
    """
    try:
        # Verify current user is member
        user_org = await get_organization_service().get_user_organization(current_user.user_id)
        if not user_org or user_org.id != org_id:
            raise HTTPException(
                status_code=403,
                detail="User is not a member of this organization"
            )

        # Get role
        role_service = get_role_service()
        role = await role_service.get_user_role(user_id, org_id)

        return role

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching role: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch role")


@router.delete("/{org_id}/members/{user_id}/role", status_code=204)
async def remove_member_role(
    org_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Remove custom role from member (reverts to default 'user' role).

    **Requirements:**
    - User must be admin or super_admin in the organization
    - Cannot remove super_admin role
    - Admin cannot remove other admin roles

    **Authentication:** Requires valid JWT token

    Args:
        org_id: UUID of the organization
        user_id: UUID of the user
        current_user: Authenticated user from JWT token

    Returns:
        204 No Content on success

    Raises:
        403: If user doesn't have permission
        400: If role removal fails
    """
    try:
        # Check if current user has permission
        role_service = get_role_service()
        can_manage = await role_service.can_manage_roles(
            current_user.user_id,
            org_id
        )

        if not can_manage:
            raise HTTPException(
                status_code=403,
                detail="Only admin or super_admin can remove roles"
            )

        # Remove role
        await role_service.remove_role(
            user_id=user_id,
            organization_id=org_id,
            removed_by=current_user.user_id
        )

        logger.info(f"Removed custom role from user {user_id} in org {org_id}")
        return None

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Failed to remove role: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error removing role: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove role")


@router.get("/users/me/roles", response_model=UserOrganizationsWithRolesResponse)
async def get_my_roles(
    current_user: User = Depends(get_current_user)
):
    """
    Get all organizations user belongs to with their roles.

    **Authentication:** Requires valid JWT token

    Args:
        current_user: Authenticated user from JWT token

    Returns:
        List of organizations with role information

    Example Response:
        ```json
        {
            "organizations": [
                {
                    "user_id": "user-uuid",
                    "organization_id": "org-uuid",
                    "organization_name": "Acme Corp",
                    "role": "super_admin",
                    "is_owner": true,
                    "joined_at": "2025-10-10T10:00:00Z"
                }
            ],
            "total": 1
        }
        ```
    """
    try:
        role_service = get_role_service()
        organizations = await role_service.get_user_organizations_with_roles(current_user.user_id)

        return UserOrganizationsWithRolesResponse(
            organizations=organizations,
            total=len(organizations)
        )

    except Exception as e:
        logger.error(f"Error fetching user roles: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch user roles")


# ============ Health Endpoint ============

@router.get("/health")
async def organization_health():
    """
    Health check for organization service.

    Returns:
        Service health status
    """
    try:
        org_service = get_organization_service()
        is_configured = org_service._client is not None

        return {
            "status": "healthy" if is_configured else "not_configured",
            "configured": is_configured,
            "service": "organizations"
        }
    except Exception as e:
        logger.error(f"Organization health check failed: {e}")
        return {
            "status": "unhealthy",
            "configured": False,
            "service": "organizations",
            "error": str(e)
        }
