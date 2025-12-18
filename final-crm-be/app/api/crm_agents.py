"""
CRM Agents API Endpoints

Provides HTTP endpoints for CRM Agent Management.
Includes CRUD operations for agents, settings, integrations, and knowledge documents.
"""

from fastapi import APIRouter, HTTPException, Query, Depends, status, UploadFile
from typing import List, Optional, Dict, Any, Tuple
import logging
import re
from datetime import datetime, timezone

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
        .select("id, status, email, phone, user_id, name") \
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
    summary="Get all agents",
    description="Retrieve agents. Hides 'Deleted' agents (Inactive + No User ID). Shows 'Offline' agents (Inactive + Has User ID)."
)
async def get_agents(
    status_filter: Optional[AgentStatus] = Query(None, description="Filter by agent status"),
    search: Optional[str] = Query(None, description="Search by name or email"),
    show_deleted: bool = Query(False, description="Include deleted agents"),
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        query = supabase.table("agents").select("*", count="exact").eq("organization_id", organization_id)

        if status_filter:
            query = query.eq("status", status_filter.value)
        
        elif not show_deleted:
            query = query.or_("status.neq.inactive,user_id.not.is.null")

        if search:
            query = query.or_(f"name.ilike.%{search}%,email.ilike.%{search}%")

        response = query.order("created_at", desc=True).execute()

        agents = [Agent(**agent) for agent in response.data]

        return AgentListResponse(
            agents=agents,
            total=response.count or len(agents)
        )

    except Exception as e:
        logger.error(f"Error fetching agents: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to fetch agents")

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
    description="Create agent. Auto-normalizes phone. Reactivates if email exists. Reclaims phone if held by inactive agent."
)
async def create_agent(
    agent: AgentCreate,
    current_user: User = Depends(get_current_user)
):
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # [FIX] Normalize phone BEFORE any checks
        if agent.phone:
            agent.phone = normalize_phone(agent.phone)

        # --- 1. UNIQUENESS CHECK ---
        existing_email_agent, existing_phone_agent = await check_agent_conflicts(
            supabase, organization_id, agent.email, agent.phone
        )

        # --- 2. HANDLE CONFLICTS & IDEMPOTENCY ---
        
        # SCENARIO A: Email Exists
        if existing_email_agent:
            # If Active -> Return it (Idempotent success)
            if existing_email_agent["status"] != "inactive":
                logger.info(f"✅ Agent exists and active: {existing_email_agent['email']}. Returning existing.")
                return Agent(**existing_email_agent)
            
            # If Inactive -> Prepare to Reactivate
            logger.info(f"♻️ Found inactive agent {existing_email_agent['email']}. Reactivating...")
            # We will handle reactivation in the execution block below using this ID
        
        # SCENARIO B: Phone Exists (on a DIFFERENT agent)
        if existing_phone_agent and agent.phone:
            # Check if it's the SAME agent we are about to reactivate
            is_same_agent = existing_email_agent and existing_phone_agent["id"] == existing_email_agent["id"]
            
            if not is_same_agent:
                if existing_phone_agent["status"] != "inactive":
                    # Hard Conflict: Phone used by another ACTIVE agent
                    raise HTTPException(400, f"Phone {agent.phone} is already used by active agent {existing_phone_agent['name']}")
                else:
                    # Soft Conflict: Phone used by INACTIVE agent -> Reclaim it
                    logger.info(f"♻️ Reclaiming phone {agent.phone} from inactive agent {existing_phone_agent['id']}")
                    supabase.table("agents").update({"phone": None}).eq("id", existing_phone_agent["id"]).execute()

        # --- 3. PREPARE DATA ---
        
        # Determine User ID Linking
        final_user_id = agent.user_id
        if not final_user_id:
            # 1. Check if admin is creating themselves
            if current_user.user_metadata.get("email") == agent.email:
                final_user_id = current_user.user_id
            else:
                # 2. Try to lookup in Auth system
                final_user_id = get_auth_user_id_by_email(supabase, agent.email)
                if final_user_id:
                    logger.info(f"🔗 Auto-linked agent {agent.email} to Auth User {final_user_id}")

        # --- 4. EXECUTION ---

        # SCENARIO: Reactivate Existing
        if existing_email_agent:
            reactivate_data = {
                "status": "active",
                "name": agent.name,
                "phone": agent.phone,
                "avatar_url": agent.avatar_url,
                "user_id": final_user_id or existing_email_agent["user_id"], # Keep old if no new provided
                "last_active_at": datetime.now(timezone.utc).isoformat()
            }
            response = supabase.table("agents").update(reactivate_data).eq("id", existing_email_agent["id"]).execute()
            return Agent(**response.data[0])

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

        response = supabase.table("agents").insert(new_agent_data).execute()
        
        if not response.data:
            raise HTTPException(500, "Failed to create agent")
            
        created_agent = Agent(**response.data[0])

        # Initialize settings
        try:
            supabase.table("agent_settings").insert({
                "agent_id": created_agent.id,
                "persona_config": {},
                "schedule_config": {},
                "advanced_config": {},
                "ticketing_config": {}
            }).execute()
        except:
            pass 

        return created_agent

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating agent: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))


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
	status_code=status.HTTP_201_CREATED,
	summary="Upload knowledge document",
	description="Upload a new knowledge document for an agent with automatic embedding"
)
async def create_knowledge_document(
		agent_id: str,
		file: UploadFile,
		current_user: User = Depends(get_current_user)
):
	"""
	Upload a knowledge document for an agent.

	Process flow:
	1. Upload file to Supabase Storage (bucket: agent_{agent_id})
	2. Extract text and generate embeddings
	3. Store embeddings in ChromaDB (collection: agent_{agent_id})
	4. Save metadata to database
	5. Rollback storage if embedding fails

	Args:
		agent_id: Agent UUID
		file: File upload (multipart/form-data)
		current_user: Current authenticated user

	Returns:
		KnowledgeDocument with metadata
	"""
	from uuid import uuid4
	from app.services.storage_service import StorageService
	from app.services.document_processor import DocumentProcessor
	from app.services.chromadb_service import ChromaDBService
	from app.utils.chunking import split_into_chunks

	file_id = None
	storage_uploaded = False

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

		# Read file content
		file_content = await file.read()
		filename = file.filename
		file_size_kb = len(file_content) // 1024
		file_type = filename.rsplit(".", 1)[-1].upper() if "." in filename else "UNKNOWN"

		# Generate unique file ID
		file_id = str(uuid4())

		logger.info(f"📤 Uploading knowledge document for agent {agent_id}: {filename} ({file_size_kb} KB)")

		# Step 1: Upload to Supabase Storage with bucket agent_{agent_id}
		# Using custom bucket name format for agents
		agent_bucket_name = f"agent_{agent_id}"
		storage_service = StorageService(supabase)

		# Ensure bucket exists (create if needed)
		try:
			buckets = supabase.storage.list_buckets()
			bucket_exists = any(b.name == agent_bucket_name for b in buckets)

			if not bucket_exists:
				supabase.storage.create_bucket(
					agent_bucket_name,
					options={
						"public": False,
						"allowed_mime_types": None
					}
				)
				logger.info(f"✅ Created storage bucket: {agent_bucket_name}")
		except Exception as e:
			logger.error(f"Failed to create bucket: {e}")
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail=f"Failed to create storage bucket: {str(e)}"
			)

		# Upload file to agent-specific bucket
		try:
			supabase.storage.from_(agent_bucket_name).upload(
				path=file_id,
				file=file_content,
				file_options={
					"content-type": file.content_type or "application/octet-stream",
					"cache-control": "3600",
					"upsert": "false"
				}
			)
			storage_uploaded = True
			logger.info(f"✅ Uploaded to storage: {agent_bucket_name}/{file_id}")
		except Exception as e:
			logger.error(f"Storage upload failed: {e}")
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail=f"File upload failed: {str(e)}"
			)

		# Get file URL
		try:
			signed_url_response = supabase.storage.from_(agent_bucket_name).create_signed_url(file_id,
			                                                                                  31536000)  # 1 year
			file_url = signed_url_response["signedURL"]
		except:
			file_url = f"storage://{agent_bucket_name}/{file_id}"

		# Step 2: Process document and extract text
		logger.info(f"📄 Processing document: {filename}")
		doc_processor = DocumentProcessor(storage_service)

		try:
			clean_text, _ = doc_processor.process_document(
				content=file_content,
				filename=filename,
				folder_path=None,
				organization_id=organization_id,
				file_id=file_id
			)

			if not clean_text or len(clean_text.strip()) == 0:
				raise ValueError("No text extracted from document")

			logger.info(f"✅ Extracted {len(clean_text)} characters from document")

		except Exception as e:
			logger.error(f"Document processing failed: {e}")
			# Rollback storage
			if storage_uploaded:
				try:
					supabase.storage.from_(agent_bucket_name).remove([file_id])
					logger.info(f"🔄 Rolled back storage upload: {agent_bucket_name}/{file_id}")
				except:
					pass
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail=f"Document processing failed: {str(e)}"
			)

		# Step 3: Generate embeddings and store in ChromaDB
		logger.info(f"🔄 Generating embeddings for: {filename}")
		chromadb_service = ChromaDBService()

		# Use agent-specific collection name
		agent_collection_name = f"agent_{agent_id}"

		try:
			# Split text into chunks
			chunks = split_into_chunks(
				text=clean_text,
				size=512,  # 512 tokens per chunk
				overlap=50
			)

			logger.info(f"📦 Split into {len(chunks)} chunks")

			# Get or create agent-specific collection
			try:
				collection = chromadb_service.client.get_collection(
					name=agent_collection_name,
					embedding_function=chromadb_service.embedding_function
				)
			except:
				# Create collection if doesn't exist
				collection = chromadb_service.client.create_collection(
					name=agent_collection_name,
					embedding_function=chromadb_service.embedding_function,
					metadata={
						"hnsw:space": "cosine",
						"agent_id": agent_id,
						"organization_id": organization_id
					}
				)
				logger.info(f"✅ Created ChromaDB collection: {agent_collection_name}")

			# Prepare IDs and metadata
			chunk_ids = [f"{file_id}-{i}" for i in range(len(chunks))]
			chunk_metas = [
				{
					"file_id": file_id,
					"filename": filename,
					"chunk_index": i,
					"agent_id": agent_id,
					"organization_id": organization_id
				}
				for i in range(len(chunks))
			]

			# Add to ChromaDB
			collection.add(
				documents=chunks,
				ids=chunk_ids,
				metadatas=chunk_metas
			)

			logger.info(f"✅ Added {len(chunks)} chunks to ChromaDB collection: {agent_collection_name}")

		except Exception as e:
			logger.error(f"Embedding generation failed: {e}")
			# Rollback storage
			if storage_uploaded:
				try:
					supabase.storage.from_(agent_bucket_name).remove([file_id])
					logger.info(f"🔄 Rolled back storage upload: {agent_bucket_name}/{file_id}")
				except:
					pass
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail=f"Embedding generation failed: {str(e)}"
			)

		# Step 4: Save metadata to database
		doc_data = {
			"agent_id": agent_id,
			"name": filename,
			"file_url": file_url,
			"file_type": file_type,
			"file_size_kb": file_size_kb,
			"metadata": {
				"file_id": file_id,
				"bucket": agent_bucket_name,
				"chunks_count": len(chunks),
				"text_length": len(clean_text)
			}
		}

		response = supabase.table("knowledge_documents").insert(doc_data).execute()

		if not response.data:
			# Rollback storage and ChromaDB
			if storage_uploaded:
				try:
					supabase.storage.from_(agent_bucket_name).remove([file_id])
					logger.info(f"🔄 Rolled back storage upload")
				except:
					pass
			try:
				collection.delete(where={"file_id": {"$eq": file_id}})
				logger.info(f"🔄 Rolled back ChromaDB chunks")
			except:
				pass
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail="Failed to save document metadata"
			)

		logger.info(f"✅ Knowledge document created for agent {agent_id}: {filename}")

		return KnowledgeDocument(**response.data[0])

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error creating knowledge document for agent {agent_id}: {e}")
		# Cleanup on unexpected error
		if storage_uploaded and file_id:
			try:
				agent_bucket_name = f"agent_{agent_id}"
				supabase = get_supabase_client()
				supabase.storage.from_(agent_bucket_name).remove([file_id])
				logger.info(f"🔄 Cleaned up storage on error")
			except:
				pass
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Failed to create knowledge document: {str(e)}"
		)


@router.delete(
	"/{agent_id}/knowledge-documents/{doc_id}",
	status_code=status.HTTP_204_NO_CONTENT,
	summary="Delete knowledge document",
	description="Delete a knowledge document from an agent, including storage and embeddings"
)
async def delete_knowledge_document(
		agent_id: str,
		doc_id: str,
		current_user: User = Depends(get_current_user)
):
	"""
	Delete a knowledge document completely.

	Process flow:
	1. Retrieve document metadata to get file_id and bucket info
	2. Delete embeddings from ChromaDB (collection: agent_{agent_id})
	3. Delete file from Supabase Storage (bucket: agent_{agent_id})
	4. Delete metadata from database

	Args:
		agent_id: Agent UUID
		doc_id: Document UUID
		current_user: Current authenticated user
	"""
	from app.services.chromadb_service import ChromaDBService

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

		# Get document metadata to retrieve file_id and bucket info
		doc_response = supabase.table("knowledge_documents").select("*").eq("id", doc_id).eq("agent_id",
		                                                                                     agent_id).execute()

		if not doc_response.data:
			raise HTTPException(
				status_code=status.HTTP_404_NOT_FOUND,
				detail=f"Knowledge document with ID {doc_id} not found for agent {agent_id}"
			)

		document = doc_response.data[0]
		metadata = document.get("metadata", {})
		file_id = metadata.get("file_id")
		agent_bucket_name = f"agent_{agent_id}"
		agent_collection_name = f"agent_{agent_id}"

		logger.info(f"🗑️  Deleting knowledge document {doc_id} for agent {agent_id}")

		# Step 1: Delete embeddings from ChromaDB
		if file_id:
			try:
				chromadb_service = ChromaDBService()
				collection = chromadb_service.client.get_collection(
					name=agent_collection_name,
					embedding_function=chromadb_service.embedding_function
				)

				# Delete all chunks with matching file_id
				collection.delete(where={"file_id": {"$eq": file_id}})
				logger.info(f"✅ Deleted embeddings from ChromaDB collection: {agent_collection_name}")
			except Exception as e:
				# Log but don't fail if ChromaDB deletion fails
				logger.warning(f"Failed to delete from ChromaDB: {e}")

		# Step 2: Delete file from Supabase Storage
		if file_id:
			try:
				supabase.storage.from_(agent_bucket_name).remove([file_id])
				logger.info(f"✅ Deleted file from storage: {agent_bucket_name}/{file_id}")
			except Exception as e:
				# Log but don't fail if storage deletion fails
				logger.warning(f"Failed to delete from storage: {e}")

		# Step 3: Delete metadata from database
		supabase.table("knowledge_documents").delete().eq("id", doc_id).execute()
		logger.info(f"✅ Deleted document metadata from database")

		logger.info(
			f"✅ Knowledge document {doc_id} completely deleted from agent {agent_id} by user {current_user.user_id}")

		return None

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error deleting knowledge document {doc_id}: {e}")
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to delete knowledge document"
		)


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
