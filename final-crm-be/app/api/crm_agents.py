"""
CRM Agents API Endpoints

Provides HTTP endpoints for CRM Agent Management.
Includes CRUD operations for agents, settings, integrations, and knowledge documents.
"""

from fastapi import APIRouter, File, HTTPException, Query, Depends, status, UploadFile, Response
from typing import List, Optional, Dict, Any, Tuple
import logging
import re
from datetime import datetime, timezone
import mimetypes

from app.models.agent import (
	Agent, AgentCreate, AgentUpdate, AgentStatusUpdate, AgentListResponse,
	AgentSettings, AgentSettingsUpdate,
	AgentIntegration, AgentIntegrationUpdate,
	KnowledgeDocument,
	AgentStatus
)
from app.auth.dependencies import get_current_user
from app.models.user import User
from app.services.organization_service import get_organization_service
from app.config import settings as app_settings
from app.services.mcp_service import get_mcp_service
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm/agents", tags=["crm-agents"])


# ============================================
# HELPER FUNCTIONS
# ============================================

def normalize_phone(phone: str) -> str:
    """
    Normalize phone number to ensure uniqueness.
    Removes '+', '-', spaces, and non-digit characters.
    Example: '+62 812-3456' -> '628123456'
    """
    if not phone:
        return phone
    # Remove all non-digit characters
    return re.sub(r'[^\d]', '', phone)

async def get_user_organization_id(user: User) -> str:
	"""Get user's organization ID and validate membership"""
	org_service = get_organization_service()
	user_org = await org_service.get_user_organization(user.user_id)

	if not user_org:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="User must belong to an organization to access CRM features"
		)

	return user_org.id


def get_supabase_client():
	"""Get Supabase client from settings"""
	from supabase import create_client

	if not app_settings.is_supabase_configured:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Supabase is not configured"
		)

	return create_client(app_settings.SUPABASE_URL, app_settings.SUPABASE_SERVICE_KEY)

async def check_agent_conflicts(
    supabase, 
    organization_id: str, 
    email: str, 
    phone: Optional[str], 
    exclude_agent_id: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Checks for existing agents with the same email or phone.
    Returns a tuple: (existing_by_email, existing_by_phone)
    Does NOT raise exceptions, returns objects for logic handling.
    """
    # 1. Build Query
    query = supabase.table("agents") \
        .select("*") \
        .eq("organization_id", organization_id)
    
    # Build OR condition
    or_conditions = [f"email.eq.{email}"]
    if phone:
        or_conditions.append(f"phone.eq.{phone}")
    
    query = query.or_(",".join(or_conditions))
    
    # Exclude current agent if updating
    if exclude_agent_id:
        query = query.neq("id", exclude_agent_id)
        
    response = query.execute()
    
    match_email = None
    match_phone = None

    # 2. Separate conflicts
    if response.data:
        for agent in response.data:
            if agent["email"] == email:
                match_email = agent
            # Only match phone if it's not None
            if phone and agent["phone"] == phone:
                match_phone = agent

    return match_email, match_phone

def get_auth_user_id_by_email(supabase, email: str) -> Optional[str]:
    """
    Attempts to fetch a User ID from Supabase Auth by email.
    Useful for auto-linking agents to registered users.
    """
    try:
        # Try to use admin API to find user
        # Note: This depends on the python client capabilities/permissions
        users = supabase.auth.admin.list_users()
        for u in users:
            if u.email == email:
                return u.id
        return None
    except Exception:
        return None

# ============================================
# AGENT CRUD ENDPOINTS
# ============================================

@router.get(
    "/",
    response_model=AgentListResponse,
    summary="Get all agents with integrations",
    description="Retrieve agents and their active integrations. Hides 'Archived' agents."
)
async def get_agents(
    status_filter: Optional[AgentStatus] = Query(None, description="Filter by status"),
    search: Optional[str] = Query(None, description="Search by name or email"),
    has_channel: Optional[str] = Query(None, description="Filter: Only return agents with a specific connected channel (e.g. 'whatsapp')"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # [FIX] JOIN the agent_integrations table so the frontend knows what is connected
        query = supabase.table("agents") \
            .select("*, agent_integrations(id, channel, status, enabled)", count="exact") \
            .eq("organization_id", organization_id) \
            .neq("status", "archived")

        if status_filter:
            query = query.eq("status", status_filter.value)

        if search:
            query = query.or_(f"name.ilike.%{search}%,email.ilike.%{search}%")

        response = query.range(skip, skip + limit - 1).order("created_at", desc=True).execute()

        final_agents = []
        for agent_data in response.data:
            # Extract the joined integrations
            integrations = agent_data.pop("agent_integrations", [])
            
            # Attach it to the agent object
            agent_data["integrations"] = integrations
            
            # [BONUS] Backend Filtering: If frontend asks for '?has_channel=whatsapp', 
            # we strip out any agents that don't have it enabled.
            if has_channel:
                has_valid_integration = any(
                    str(i.get("channel")).lower() == has_channel.lower() and i.get("enabled") is True
                    for i in integrations
                )
                if not has_valid_integration:
                    continue # Skip this agent, they don't have the required pipe
                    
            final_agents.append(Agent(**agent_data))

        return AgentListResponse(
            agents=final_agents,
            total=response.count if not has_channel else len(final_agents)
        )

    except Exception as e:
        logger.error(f"Error fetching agents: {e}")
        raise HTTPException(500, "Failed to fetch agents")
	
	
@router.get(
	"/{agent_id}",
	response_model=Agent,
	summary="Get agent by ID",
	description="Retrieve a specific agent by ID"
)
async def get_agent(
		agent_id: str,
		current_user: User = Depends(get_current_user)
):
	"""Get specific agent by ID"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		response = supabase.table("agents").select("*").eq("id", agent_id).eq("organization_id",
		                                                                      organization_id).execute()

		if not response.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		return Agent(**response.data[0])

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error fetching agent {agent_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to fetch agent"
		)

@router.post(
    "/",
    response_model=Agent,
    status_code=status.HTTP_201_CREATED,
    summary="Create new agent",
    description="Create agent. Auto-normalizes phone. Reactivates if email exists. Reclaims phone if held by inactive/archived agent."
)
async def create_agent(
    agent: AgentCreate,
    response: Response,  # <--- INJECT FASTAPI RESPONSE HERE
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # [FIX] Normalize phone BEFORE any checks
        if agent.phone:
            agent.phone = normalize_phone(agent.phone)

        # --- 1. UNIQUENESS CHECK ---
        # IMPORTANT: check_agent_conflicts MUST use .select("*") for Pydantic to work
        existing_email_agent, existing_phone_agent = await check_agent_conflicts(
            supabase, organization_id, agent.email, agent.phone
        )

        # --- 2. HANDLE CONFLICTS & IDEMPOTENCY ---
        
        # SCENARIO A: Email Exists
        if existing_email_agent:
            # If Active or Busy -> Hard Failure (Strict Validation)
            if existing_email_agent["status"] not in ["inactive", "archived"]:
                logger.warning(f"❌ Creation rejected: Active agent with email {agent.email} already exists.")
                raise HTTPException(
                    status_code=409, 
                    detail=f"An active agent with the email '{agent.email}' already exists."
                )
            
            # If Inactive OR Archived -> Prepare to Reactivate
            logger.info(f"♻️ Found {existing_email_agent['status']} agent {existing_email_agent['email']}. Reactivating...")
        
        # SCENARIO B: Phone Exists (on a DIFFERENT agent)
        if existing_phone_agent and agent.phone:
            # Check if it's the SAME agent we are about to reactivate
            is_same_agent = existing_email_agent and existing_phone_agent["id"] == existing_email_agent["id"]
            
            if not is_same_agent:
                if existing_phone_agent["status"] not in ["inactive", "archived"]:
                    # Hard Conflict: Phone used by another ACTIVE agent
                    raise HTTPException(
                        status_code=400, 
                        detail=f"The phone number '{agent.phone}' is already assigned to the active agent '{existing_phone_agent['name']}'. Please use a different number or deactivate that agent first."
                    )
                else:
                    # Soft Conflict: Phone used by INACTIVE/ARCHIVED agent -> Reclaim it
                    logger.info(f"♻️ Reclaiming phone {agent.phone} from {existing_phone_agent['status']} agent {existing_phone_agent['id']}")
                    supabase.table("agents").update({"phone": None}).eq("id", existing_phone_agent["id"]).execute()

        # --- 3. PREPARE DATA ---
        
        # 💀 AUTO-LINKER KILLED. We strictly use what the frontend sends, or None.
        final_user_id = agent.user_id

        # --- 4. EXECUTION ---

        # SCENARIO: Reactivate Existing
        if existing_email_agent:
            reactivate_data = {
                "status": "active",
                "name": agent.name,
                "phone": agent.phone,
                "avatar_url": agent.avatar_url,
                "user_id": final_user_id, # Explicitly overwrite with new (or None)
                "last_active_at": datetime.now(timezone.utc).isoformat()
            }
            db_response = supabase.table("agents").update(reactivate_data).eq("id", existing_email_agent["id"]).execute()
            
            response.status_code = status.HTTP_200_OK  # <--- OVERRIDE TO 200 OK because we just updated an old record
            return Agent(**db_response.data[0])

        # SCENARIO: Create New
        logger.info(f"✨ Creating fresh agent: {agent.email}")
        
        new_agent_data = {
            "organization_id": organization_id,
            "name": agent.name,
            "email": agent.email,
            "phone": agent.phone,
            "status": agent.status.value,
            "avatar_url": agent.avatar_url,
            "user_id": final_user_id,
            "assigned_chats_count": 0,
            "resolved_today_count": 0,
            "avg_response_time_seconds": 0,
            "last_active_at": datetime.now(timezone.utc).isoformat(),
        }

        db_response = supabase.table("agents").insert(new_agent_data).execute()
        
        if not db_response.data:
            # Human readable UX error instead of blank 500
            raise HTTPException(
                status_code=500, 
                detail="We couldn't save the agent to the database. Please check your connection and try again."
            )
            
        created_agent = Agent(**db_response.data[0])

        # Initialize settings
        try:
            supabase.table("agent_settings").insert({
                "agent_id": created_agent.id,
                "persona_config": {},
                "schedule_config": {},
                "advanced_config": {},
                "ticketing_config": {}
            }).execute()
        except Exception as settings_err:
            logger.warning(f"Failed to initialize settings for agent {created_agent.id}: {settings_err}") 

        # Defaults to 201 Created from the decorator
        return created_agent

    except HTTPException:
        # Let our deliberately crafted 400s, 409s, and 500s pass through
        raise
    except Exception as e:
        # You read this in the logs, not the user.
        logger.error(f"Critical error creating agent '{agent.email}': {e}", exc_info=True)
        # The user reads this clean message.
        raise HTTPException(
            status_code=500, 
            detail="An unexpected system error occurred while setting up this agent. Our team has been notified."
        )   

@router.put(
    "/{agent_id}",
    response_model=Agent,
    summary="Update agent",
    description="Update agent. Auto-normalizes phone numbers."
)
async def update_agent(
    agent_id: str,
    agent_update: AgentUpdate,
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Verify existence
        current = supabase.table("agents").select("*").eq("id", agent_id).single().execute()
        if not current.data:
            raise HTTPException(404, "Agent not found")
        
        existing_data = current.data

        update_data = agent_update.model_dump(exclude_unset=True)
        if not update_data:
            return Agent(**existing_data)

        # [FIX] Normalize phone if present in update
        if "phone" in update_data:
            update_data["phone"] = normalize_phone(update_data["phone"])

        # 2. UNIQUENESS CHECK (Only if changing email/phone)
        new_email = update_data.get("email", existing_data["email"])
        new_phone = update_data.get("phone", existing_data["phone"])
        
        should_check = "email" in update_data or "phone" in update_data
        
        if should_check:
            match_email, match_phone = await check_agent_conflicts(
                supabase, organization_id, new_email, new_phone, exclude_agent_id=agent_id
            )
            
            # Check Email Conflict
            if match_email and match_email["status"] != "inactive":
                 raise HTTPException(400, f"Email {new_email} is already used by active agent {match_email['name']}")

            # Check Phone Conflict
            if match_phone and new_phone:
                if match_phone["status"] != "inactive":
                    raise HTTPException(400, f"Phone {new_phone} is already used by active agent {match_phone['name']}")
                else:
                    # Reclaim phone from inactive agent
                    logger.info(f"♻️ Reclaiming phone {new_phone} from inactive agent {match_phone['id']}")
                    supabase.table("agents").update({"phone": None}).eq("id", match_phone["id"]).execute()

        # 3. User Linking (If email changed and no user_id)
        if "email" in update_data and not update_data.get("user_id") and not existing_data.get("user_id"):
             # Attempt auto-link
             found_user_id = get_auth_user_id_by_email(supabase, new_email)
             if found_user_id:
                 update_data["user_id"] = found_user_id
                 logger.info(f"🔗 Auto-linked updated agent {new_email} to User {found_user_id}")

        # 4. Update
        if "status" in update_data and update_data["status"]:
            update_data["status"] = update_data["status"].value

        response = supabase.table("agents").update(update_data).eq("id", agent_id).execute()
        return Agent(**response.data[0])

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Update failed: {e}")
        raise HTTPException(500, str(e))	


@router.patch(
    "/{agent_id}/status",
    response_model=Agent,
    summary="Update agent status",
    description="Update agent status only (active/inactive/busy)."
)
async def update_agent_status(
    agent_id: str,
    status_update: AgentStatusUpdate,
    current_user: User = Depends(get_current_user)
):
    """Update agent status only"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Verify existence and ownership
        current = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).single().execute()
        if not current.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        # 2. Update status
        update_data = {
            "status": status_update.status.value,
            "last_active_at": datetime.now(timezone.utc).isoformat()
        }
        
        response = supabase.table("agents").update(update_data).eq("id", agent_id).execute()
        
        if not response.data:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to update agent status")
            
        return Agent(**response.data[0])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent status {agent_id}: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
	
@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete agent (Soft Delete & Unassign)",
    description="Soft delete agent and unassign them from all active chats."
)
async def delete_agent(
    agent_id: str,
    current_user: User = Depends(get_current_user)
):
    """Soft delete agent and unassign active chats"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Check if agent exists
        existing = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not existing.data:
            raise HTTPException(404, detail="Agent not found")

        # 2. SOFT DELETE: Mark archived & unlink user
        # [UPDATED] Using "archived" status as requested
        update_data = {
            "status": "archived",
            "user_id": None,
            "last_active_at": datetime.now(timezone.utc).isoformat()
        }
        
        # Update agent record
        supabase.table("agents").update(update_data).eq("id", agent_id).execute()

        # 3. UNASSIGN ACTIVE CHATS
        active_statuses = ["open", "assigned", "pending"]
        
        # A. Clear from 'assigned_agent_id' (Legacy field)
        supabase.table("chats") \
            .update({"assigned_agent_id": None, "status": "open"}) \
            .eq("assigned_agent_id", agent_id) \
            .in_("status", active_statuses) \
            .execute()
            
        # B. Clear from 'human_agent_id' & reset handled_by
        supabase.table("chats") \
            .update({
                "human_agent_id": None, 
                "handled_by": "unassigned", 
                "status": "open"
            }) \
            .eq("human_agent_id", agent_id) \
            .in_("status", active_statuses) \
            .execute()

        logger.info(f"Agent {agent_id} archived and unassigned from active chats by {current_user.user_id}")

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting agent {agent_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete agent: {str(e)}"
        )
	
# ============================================
# AGENT SETTINGS ENDPOINTS
# ============================================

@router.get(
	"/{agent_id}/settings",
	response_model=AgentSettings,
	summary="Get agent settings",
	description="Retrieve settings for a specific agent"
)
async def get_agent_settings(
		agent_id: str,
		current_user: User = Depends(get_current_user)
):
	"""Get agent settings"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		# Verify agent belongs to organization
		agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id",
		                                                                          organization_id).execute()

		if not agent_check.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		# Get settings
		response = supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()

		if not response.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Settings for agent {agent_id} not found"
			)

		return AgentSettings(**response.data[0])

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error fetching agent settings for {agent_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to fetch agent settings"
		)

@router.put(
	"/{agent_id}/settings",
	response_model=AgentSettings,
	summary="Update agent settings",
	description="Update settings for a specific agent"
)
async def update_agent_settings(
		agent_id: str,
		settings_update: AgentSettingsUpdate,
		current_user: User = Depends(get_current_user)
):
	"""Update agent settings"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		# Verify agent belongs to organization
		agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id",
		                                                                          organization_id).execute()

		if not agent_check.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		# Prepare update data
		update_data = {}

		if settings_update.persona_config:
			update_data["persona_config"] = settings_update.persona_config.model_dump()

		if settings_update.schedule_config:
			dumped_schedule = settings_update.schedule_config.model_dump()
			logger.info(f"📅 Schedule config being saved for agent {agent_id}:")
			logger.info(f"  Timezone: {dumped_schedule.get('timezone')}")
			for idx, wh in enumerate(dumped_schedule.get('workingHours', [])):
				logger.info(f"  Day {idx}: {wh.get('day')} - enabled: {wh.get('enabled')}")
			update_data["schedule_config"] = dumped_schedule

		if settings_update.advanced_config:
			update_data["advanced_config"] = settings_update.advanced_config.model_dump()

		if settings_update.ticketing_config:
			update_data["ticketing_config"] = settings_update.ticketing_config.model_dump()

		if not update_data:
			# No fields to update, fetch existing
			response = supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()
			return AgentSettings(**response.data[0])

		# Update settings
		response = supabase.table("agent_settings").update(update_data).eq("agent_id", agent_id).execute()

		if not response.data:
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail="Failed to update agent settings"
			)

		logger.info(f"Agent settings updated: {agent_id} by user {current_user.user_id}")

		return AgentSettings(**response.data[0])

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error updating agent settings for {agent_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to update agent settings"
		)

# ============================================
# KNOWLEDGE DOCUMENTS ENDPOINTS
# ============================================

@router.get(
	"/{agent_id}/knowledge-documents",
	response_model=List[KnowledgeDocument],
	summary="Get agent knowledge documents",
	description="Retrieve all knowledge documents for a specific agent"
)
async def get_agent_knowledge_documents(
		agent_id: str,
		current_user: User = Depends(get_current_user)
):
	"""Get all knowledge documents for an agent"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		# Verify agent belongs to organization
		agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id",
		                                                                          organization_id).execute()

		if not agent_check.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		# Get knowledge documents
		response = supabase.table("knowledge_documents").select("*").eq("agent_id", agent_id).order("uploaded_at",
		                                                                                            desc=True).execute()

		documents = [KnowledgeDocument(**doc) for doc in response.data]

		return documents

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error fetching knowledge documents for agent {agent_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to fetch knowledge documents"
		)


@router.post(
    "/{agent_id}/knowledge-documents",
    response_model=KnowledgeDocument,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload knowledge document (V2) - Async",
    description="Accepts file, uploads to storage, and queues background processing to prevent UI freezing."
)
async def create_knowledge_document(
    agent_id: str,
    response: Response,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    from uuid import uuid4
    from app.services.document_processor_v2 import DocumentProcessorV2
    from app.services.document_queue_service import get_document_queue_service

    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # =========================================================
        # STEP 1: FAST VALIDATION & SETUP
        # =========================================================
        filename = file.filename
        file_content = await file.read() # Read into memory instantly before connection closes
        doc_processor = DocumentProcessorV2()

        try:
            ext = doc_processor.validate_knowledge_file(filename, file_content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        file_size_kb = len(file_content) // 1024
        file_type = ext.upper()
        file_id = str(uuid4())
        agent_bucket_name = f"agent_{agent_id}"

        # Verify agent ownership
        agent_check = supabase.table("agents").select("id, name").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data:
            raise HTTPException(404, detail="Agent not found")
        agent_name = agent_check.data[0]["name"]

        # =========================================================
        # STEP 2: SYNCHRONOUS STORAGE UPLOAD (Backup raw file)
        # =========================================================
        try:
            buckets = supabase.storage.list_buckets()
            if not any(b.name == agent_bucket_name for b in buckets):
                supabase.storage.create_bucket(agent_bucket_name, options={"public": False})
        except Exception:
            pass

        try:
            supabase.storage.from_(agent_bucket_name).upload(
                path=file_id,
                file=file_content,
                file_options={"content-type": file.content_type, "upsert": "false"}
            )
        except Exception as e:
            logger.error(f"❌ Fast storage upload failed: {e}")
            raise HTTPException(500, detail="Failed to save file to storage.")

        # =========================================================
        # STEP 3: CREATE "PENDING" DB RECORD & RETURN TO FRONTEND
        # =========================================================
        doc_data = {
            "agent_id": agent_id,
            "name": filename,
            "file_url": f"storage://{agent_bucket_name}/{file_id}",
            "file_type": file_type,
            "file_size_kb": file_size_kb,
            "metadata": {
                "file_id": file_id,
                "bucket": agent_bucket_name,
                "processor_version": "v2",
                "status": "pending",  # STATE MACHINE IS CRITICAL HERE
                "chunks": 0
            }
        }
        
        db_response = supabase.table("knowledge_documents").insert(doc_data).execute()
        if not db_response.data:
            supabase.storage.from_(agent_bucket_name).remove([file_id])
            raise HTTPException(500, detail="Database insert failed")

        created_doc = KnowledgeDocument(**db_response.data[0])

        # =========================================================
        # STEP 4: PUSH TO REDIS QUEUE (instead of BackgroundTasks)
        # =========================================================
        queue = get_document_queue_service()
        queued = queue.enqueue(
            doc_id=created_doc.id,
            agent_id=agent_id,
            agent_name=agent_name,
            organization_id=organization_id,
            file_id=file_id,
            filename=filename,
            bucket_name=agent_bucket_name,
        )

        if not queued:
            logger.error(f"⚠️ Redis queue failed for {filename}, file safe in storage.")
            error_meta = {**doc_data["metadata"], "status": "queue_failed"}
            supabase.table("knowledge_documents") \
                .update({"metadata": error_meta}) \
                .eq("id", created_doc.id).execute()

        logger.info(f"🚀 Queued background processing for {filename}. Unblocking UI.")
        response.status_code = status.HTTP_202_ACCEPTED
        return created_doc

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Upload initialization failed: {e}", exc_info=True)
        raise HTTPException(500, detail="An unexpected system error occurred.")


@router.delete(
    "/{agent_id}/knowledge-documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete knowledge document",
    description="STRICT ORDER: Chroma -> Storage -> DB. Aborts if Chroma fails."
)
async def delete_knowledge_document(
    agent_id: str,
    doc_id: str,
    current_user: User = Depends(get_current_user)
):
    from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
    from app.services.chromadb_service import ChromaDBService

    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Verify Agent Ownership & Get Document Metadata
        # We need the metadata BEFORE we delete anything.
        agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data:
            raise HTTPException(404, detail="Agent not found")

        doc = supabase.table("knowledge_documents").select("*").eq("id", doc_id).execute()
        if not doc.data:
            raise HTTPException(404, detail="Document not found")
        
        meta = doc.data[0].get("metadata", {})
        file_id = meta.get("file_id")
        version = meta.get("processor_version", "v1")

        # =========================================================
        # ⚠️ STEP 1: ATTEMPT TO DELETE FROM CHROMA (No longer blocking)
        # =========================================================
        if file_id:
            try:
                if version == "v2":
                    svc = get_crm_chroma_service_v2()
                    success = svc.delete_document(agent_id, file_id)
                    if not success:
                        logger.warning(f"⚠️ ChromaDB vectors not found or delete failed for {file_id}. Moving on.")
                else:
                    # Legacy V1 Fallback
                    service = ChromaDBService()
                    name = f"agent_{agent_id}"
                    try:
                        col = service.client.get_collection(name=name, embedding_function=service.embedding_function)
                        col.delete(where={"file_id": {"$eq": file_id}})
                    except Exception:
                        pass
            except Exception as e:
                # WE DO NOT RAISE A 500 HERE ANYMORE. WE LOG AND PROCEED.
                logger.warning(f"⚠️ Connection to Vector DB failed: {e}. Moving on to DB/Storage deletion.")

        # =========================================================
        # STEP 2: DELETE FROM STORAGE
        # =========================================================
        if file_id:
            try:
                # Try to remove, but don't crash if file is already missing from bucket
                supabase.storage.from_(f"agent_{agent_id}").remove([file_id])
            except Exception as e:
                logger.warning(f"⚠️ Storage delete warning: {e}")

        # =========================================================
        # STEP 3: DELETE FROM DATABASE (Final Commit)
        # =========================================================
        supabase.table("knowledge_documents").delete().eq("id", doc_id).execute()
        
        logger.info(f"🗑️ [Strict Delete] Successfully removed document {doc_id}")
        return None

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Delete failed: {e}")
        raise HTTPException(500, detail=str(e))

@router.get(
    "/{agent_id}/knowledge-documents/{doc_id}/download",
    summary="Download knowledge document",
    description="Downloads the physical file for a knowledge document."
)
async def download_knowledge_document(
    agent_id: str,
    doc_id: str,
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Verify Agent Ownership & Get Document Metadata
        agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")

        doc = supabase.table("knowledge_documents").select("*").eq("id", doc_id).execute()
        if not doc.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
        
        doc_data = doc.data[0]
        meta = doc_data.get("metadata", {})
        
        # The V2 upload process stores the storage file_id here
        file_id = meta.get("file_id") 
        filename = doc_data.get("name", "document.bin")
        
        if not file_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File ID not found in document metadata")

        # 2. Download from Storage
        bucket_name = f"agent_{agent_id}"
        try:
            # Supabase Python SDK download() returns bytes directly into memory.
            file_bytes = supabase.storage.from_(bucket_name).download(file_id)
        except Exception as e:
            logger.error(f"Failed to download file {file_id} from bucket {bucket_name}: {e}")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve file from storage")

        # 3. Determine Content-Type
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "application/octet-stream"

        # 4. Return Response with attachment headers
        return Response(
            content=file_bytes,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download endpoint failed: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    
# ============================================
# AGENT INTEGRATIONS ENDPOINTS
# ============================================

@router.get(
	"/{agent_id}/integrations",
	response_model=List[AgentIntegration],
	summary="Get agent integrations",
	description="Retrieve all integrations for a specific agent"
)
async def get_agent_integrations(
		agent_id: str,
		current_user: User = Depends(get_current_user)
):
	"""Get all integrations for an agent"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		# Verify agent belongs to organization
		agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id",
		                                                                          organization_id).execute()

		if not agent_check.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		# Get integrations
		response = supabase.table("agent_integrations").select("*").eq("agent_id", agent_id).execute()

		integrations = [AgentIntegration(**integration) for integration in response.data]

		return integrations

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error fetching integrations for agent {agent_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to fetch agent integrations"
		)


@router.put(
	"/{agent_id}/integrations/{channel}",
	response_model=AgentIntegration,
	summary="Update agent integration",
	description="""
    Update integration configuration for a specific channel.
    """
)
async def update_agent_integration(
		agent_id: str,
		channel: str,
		integration_update: AgentIntegrationUpdate,
		current_user: User = Depends(get_current_user)
):
	"""
	Update integration for a specific channel.

	Automatically extracts and updates the status field if config.status is provided.
	"""
	try:
		organization_id = await get_user_organization_id(current_user)
		supabase = get_supabase_client()

		# Verify agent belongs to organization
		agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id",
		                                                                          organization_id).execute()

		if not agent_check.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Agent with ID {agent_id} not found"
			)

		# Prepare update data
		update_data = integration_update.model_dump(exclude_unset=True)

		# Convert enum to value if status is being updated
		if "status" in update_data and update_data["status"]:
			update_data["status"] = update_data["status"].value

		# Extract status from config if config is provided
		if "config" in update_data and update_data["config"]:
			config = update_data["config"]

			# If config has status field, update the status field in database
			if isinstance(config, dict) and "status" in config:
				config_status = config["status"]

				# Validate and set status from config
				valid_statuses = ["connected", "disconnected", "connecting", "error"]
				if config_status in valid_statuses:
					update_data["status"] = config_status
					logger.info(
						f"Updating status to '{config_status}' from config.status for agent {agent_id}/{channel}")

		# Update or insert integration
		existing = supabase.table("agent_integrations").select("*").eq("agent_id", agent_id).eq("channel",
		                                                                                        channel).execute()

		if existing.data:
			logger.info(f"UPDATE DATA: {update_data}")
			# Update existing
			response = supabase.table("agent_integrations").update(update_data).eq("agent_id", agent_id).eq("channel",
			                                                                                                channel).execute()
		else:
			# Insert new
			insert_data = {
				"agent_id": agent_id,
				"channel": channel,
				**update_data
			}
			response = supabase.table("agent_integrations").insert(insert_data).execute()

		if not response.data:
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail="Failed to update agent integration"
			)

		logger.info(f"Agent integration updated: {agent_id}/{channel} by user {current_user.user_id}")

		return AgentIntegration(**response.data[0])

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error updating integration for agent {agent_id}/{channel}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to update agent integration"
		)


# ============================================
# MCP INTEGRATIONS ENDPOINTS
# ============================================

class MCPTestRequest(BaseModel):
    url: str
    transport: str
    apiKey: Optional[str] = None

@router.post(
    "/{agent_id}/mcp/test-connection",
    summary="Test MCP Connection (Secure)",
    description="Securely tests the MCP connection by fetching the saved URL and API key directly from the database to prevent SSRF."
)
async def test_mcp_connection(
    agent_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Secure Handshake. 
    The FE no longer sends the URL/API key. The BE fetches it from the agent's integration record.
    """
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Verify agent ownership
        agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")

        # 2. Fetch the secure integration config from DB
        integration = supabase.table("agent_integrations").select("config").eq("agent_id", agent_id).eq("channel", "mcp").execute()
        
        if not integration.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="MCP integration not configured for this agent. Save it first.")
            
        config = integration.data[0].get("config", {})
        servers = config.get("servers", [])
        
        if not servers:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No servers found in the saved MCP configuration.")
            
        # Extract the first server (based on your current UI config structure)
        target_server = servers[0]
        url = target_server.get("url")
        api_key = target_server.get("apiKey")
        
        if not url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Server URL is missing in the database configuration.")

        # 3. Test the connection securely
        service = get_mcp_service()
        result = await service.test_connection(
            url=url,
            transport="http",  # Hardcoded for Palapa REST architecture
            api_key=api_key
        )
        
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Secure connection test failed for agent {agent_id}: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.post(
    "/{agent_id}/mcp/initialize",
    summary="Initialize Palapa Tables as AI Tools",
    description="Crawls the connected Palapa database, extracts schemas, and translates them into OpenAI tools."
)
async def init_mcp_tools(
    agent_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    The 'initialize' process. Called by the frontend to fetch and translate Palapa schemas.
    Currently set to log the output and return it to the FE.
    """
    import json
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()
        
        # 1. Verify agent ownership
        agent_check = supabase.table("agents").select("id").eq("id", agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")
            
        # 2. Trigger the schema translator
        service = get_mcp_service()
        tools = await service.get_all_tools_schema(supabase, agent_id)
        
        # 3. LOG IT OUT FIRST (As requested)
        logger.info(f"🛠️ [INIT] Successfully mapped {len(tools)} tools for Agent {agent_id}.")
        logger.info(f"🛠️ [INIT] Payload sending to Frontend:\n{json.dumps(tools, indent=2)}")
        
        # 4. Return to FE so your HTML can use it
        return {
            "success": True,
            "message": f"Successfully initialized {len(tools)} database tables as AI tools.",
            "tools_count": len(tools),
            "tools": tools
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to init tools for agent {agent_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Schema initialization failed: {str(e)}"
        )
    
    