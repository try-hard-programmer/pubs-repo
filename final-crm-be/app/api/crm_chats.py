"""
CRM Chats & Messages API Endpoints

Provides HTTP endpoints for chat management, customer management, messaging, and ticketing.
"""

from fastapi import APIRouter, HTTPException, Query, Depends, status, File, UploadFile, Form, Response
from typing import Optional, List, Dict, Any
import logging
import re
import json
from datetime import datetime, timezone
from uuid import uuid4
from app.services.storage_service import get_storage_service
from app.models.agent import (
    Customer, CustomerCreate, CustomerUpdate, CustomerListResponse,
    Chat, ChatCreate, ChatUpdate, ChatAssign, ChatEscalation, ChatListResponse, ChatStatus,
    Message, MessageCreate, MessageListResponse, SenderType, MessageAttachment,
    Ticket, TicketCreate, TicketUpdate, TicketListResponse, TicketStatus, TicketPriority,
    DashboardMetrics, CommunicationChannel
)
from app.models.ticket import TicketActivityResponse, ActorType
from app.models.user import User

from app.auth.dependencies import get_current_user
from app.services.organization_service import get_organization_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.websocket_service import get_connection_manager
from app.config import settings as app_settings
from app.services.webhook_callback_service import get_webhook_callback_service
from app.services.ticket_service import get_ticket_service
from app.models.ticket import ActorType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm", tags=["crm-chats"])


# ============================================
# HELPER FUNCTIONS
# ============================================

async def get_user_organization_id(user: User) -> str:
    """Get user's organization ID and validate membership"""
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(user.user_id)
    if not user_org:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User must belong to an organization")
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

async def get_default_ai_agent(organization_id: str, supabase) -> Optional[str]:
    """
    Get default AI agent for organization.
    AI agents are identified by user_id = NULL.
    Returns None if no AI agent is found.
    """
    try:
        response = supabase.table("agents") \
            .select("id") \
            .eq("organization_id", organization_id) \
            .eq("status", "active") \
            .is_("user_id", "null") \
            .order("created_at", desc=False) \
            .limit(1) \
            .execute()

        if response.data:
            return response.data[0]["id"]

        logger.warning(f"No active AI agent found for organization {organization_id}")
        return None
    except Exception as e:
        logger.error(f"Error fetching default AI agent for organization {organization_id}: {e}")
        return None

def format_whatsapp_phone(phone: str) -> str:
    """
    Format phone number for WhatsApp API

    Converts various phone number formats to WhatsApp-compatible format (e.g., 628123456789)

    Examples:
        +62 812-3456-7890 → 628123456789
        0812-3456-7890 → 628123456789
        62812-3456-7890 → 628123456789
        812-3456-7890 → 628123456789

    Args:
        phone: Phone number in any format

    Returns:
        Formatted phone number starting with country code (62 for Indonesia)
    """
    if not phone:
        return ""

    # Remove all non-digit characters
    cleaned = re.sub(r'[^\d]', '', phone)

    # If starts with 0, replace with 62 (Indonesia country code)
    if cleaned.startswith('0'):
        cleaned = '62' + cleaned[1:]

    # If doesn't start with 62, prepend it
    if not cleaned.startswith('62'):
        cleaned = '62' + cleaned

    return cleaned

async def send_message_via_channel(
    chat_data: Dict[str, Any],
    customer_data: Dict[str, Any],
    message_content: str,
    supabase,
    message_metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Sends message strictly via the defined agent. 
    Returns raw data on success for ID capture.
    Handles Text, Media (Image/Video/Audio), and Files (Documents).
    """
    try:
        chat_channel = chat_data.get("channel")
        sender_id = chat_data.get("sender_agent_id")
        
        if not sender_id:
             return {"success": False, "message": "No sender_agent_id provided."}

        # 1. STRICT INTEGRATION CHECK
        int_check = supabase.table("agent_integrations") \
            .select("id") \
            .eq("agent_id", sender_id) \
            .eq("channel", chat_channel) \
            .eq("enabled", True) \
            .execute()
        
        if not int_check.data:
            return {
                "success": False, 
                "message": f"Agent {sender_id} has no connected {chat_channel} account."
            }

        # 2. PREPARE CONTENT & MEDIA (Moved to TOP Scope)
        effective_chat_data = chat_data.copy()
        msg_meta = message_metadata or {}
        
        # [CRITICAL FIX] Define this BEFORE checking channels
        media_url = msg_meta.get("media_url") or msg_meta.get("file_url")

        # 3. ROUTE TO SERVICE
        if chat_channel == "whatsapp":
            # [FIX] PRIORITY: Check metadata for stored WhatsApp ID (LID or @c.us)
            metadata = customer_data.get("metadata", {}) or {}
            target = metadata.get("whatsapp_lid")
            
            # Fallback to phone or whatsapp_id
            if not target:
                target = customer_data.get("phone")
            if not target or target == "None":
                target = metadata.get("whatsapp_id")
            
            if not target: return {"success": False, "message": "Customer has no phone or WhatsApp ID"}
            
            # [FIX] GENERIC ID PRESERVATION
            final_target = str(target).strip()
            
            if "@" in final_target:
                pass 
            else:
                cleaned = re.sub(r'[^\d]', '', final_target)
                if len(cleaned) < 15 and not cleaned.startswith('62') and cleaned.startswith('0'): 
                    cleaned = '62' + cleaned[1:]
                final_target = cleaned
            
            svc = get_whatsapp_service()
            
            try:
                if media_url:
                    # Determine if it's a file/document or general media
                    is_document = msg_meta.get("is_document", False)
                    filename = msg_meta.get("filename")
                    
                    if is_document or filename:
                        # Send as Document/File
                        res = await svc.send_file_message(
                            session_id=sender_id,
                            phone_number=final_target,
                            file_url=media_url,
                            filename=filename,
                            caption=message_content
                        )
                    else:
                        # Send as Media (Image, Video, etc.)
                        media_type = msg_meta.get("media_type", "image")
                        res = await svc.send_media_message(
                            session_id=sender_id,
                            phone_number=final_target,
                            media_url=media_url,
                            caption=message_content,
                            media_type=media_type
                        )
                else:
                    # Send Text
                    res = await svc.send_text_message(sender_id, final_target, message_content)
                
                return {"success": True, "data": res}
            except Exception as e:
                return {"success": False, "message": str(e)}

        elif chat_channel == "telegram":
            try:
                svc = get_webhook_callback_service()
                
                # [FIX] media_url is now defined and safe to pass
                res = await svc.send_callback(
                    effective_chat_data, 
                    message_content, 
                    supabase,
                    media_url=media_url 
                )
                if res.get("success"):
                    return {"success": True, "data": res.get("data", res)} 
                else:
                    return {"success": False, "message": res.get("error") or "Telegram Send Failed"}
            except Exception as e:
                return {"success": False, "message": str(e)}
        
        return {"success": True, "message": "Internal channel"}

    except Exception as e:
        logger.error(f"Send Error: {e}")
        return {"success": False, "message": str(e)}
        
def _extract_attachment(metadata: Dict[str, Any]) -> Optional[MessageAttachment]:
    """Helper to extract attachment data from metadata"""
    if not metadata:
        return None
        
    url = metadata.get("media_url") or metadata.get("file_url")
    if not url:
        return None
        
    return MessageAttachment(
        url=url,
        type=metadata.get("media_type") or "application/octet-stream",
        name=metadata.get("filename") or "file.bin"
    )

async def create_message_internal(chat_data: dict, content: str, user_id: str, supabase):
    """Inserts message, Sends, and SYNCHRONOUSLY captures ID. Deletes on failure."""
    logger.info(f"[msg_internal] Sending for chat {chat_data['id']}")
    
    msg_data = {
        "chat_id": chat_data["id"], 
        "sender_type": "agent", 
        "sender_id": user_id, 
        "content": content, 
        "metadata": {"source": "new_chat_modal"}
    }
    
    # 1. Insert Message (Optimistic Save)
    res = supabase.table("messages").insert(msg_data).execute()
    if not res.data:
        raise HTTPException(500, "Failed to insert message")
    
    new_message_id = res.data[0]["id"]
    
    # 2. Get Customer Data
    cust_res = supabase.table("customers").select("*").eq("id", chat_data["customer_id"]).single().execute()
    if cust_res.data:
        # 3. Send External (WhatsApp/Telegram)
        result = await send_message_via_channel(chat_data, cust_res.data, content, supabase)
        
        # 4. CHECK FAILURE & ROLLBACK
        if not result.get("success"):
            error_msg = result.get("message", "")
            logger.error(f"❌ Send Failed: {error_msg} -> Rolling back DB message {new_message_id}")
            
            # [FIX] DELETE the message we just inserted so it doesn't stay in DB
            supabase.table("messages").delete().eq("id", new_message_id).execute()
            
            raise HTTPException(400, f"Message Failed: {error_msg}")
        
        else:
            # 5. Success Handling (Capture ID)
            data = result.get("data", {})
            resolved_id = None
            if isinstance(data, dict):
                resolved_id = data.get("peer_id") or data.get("user_id") or data.get("id") or data.get("resolved_chat_id")
            
            if resolved_id and str(resolved_id).isdigit():
                current_meta = cust_res.data.get("metadata") or {}
                channel_key = f"{chat_data['channel']}_id"
                if str(current_meta.get(channel_key)) != str(resolved_id):
                    logger.info(f"🔗 Capturing ID {resolved_id}")
                    current_meta[channel_key] = str(resolved_id)
                    supabase.table("customers").update({"metadata": current_meta}).eq("id", chat_data["customer_id"]).execute()

async def create_ticket(
        self, 
        data: TicketCreate, 
        organization_id: str, 
        ticket_config: Dict = None, 
        actor_id: Optional[str] = None,
        actor_type: ActorType = ActorType.SYSTEM
    ) -> Ticket:
        
        # 1. Generate Number
        ticket_num = await self._generate_ticket_number(organization_id, ticket_config)
        logger.info(f"🎫 Creating Ticket {ticket_num} for Chat {data.chat_id}")

        # [FIX] Resolve Customer Name explicitly from DB
        customer_name = "Unknown Customer"
        if data.customer_id:
            try:
                cust_res = self.supabase.table("customers").select("name").eq("id", data.customer_id).single().execute()
                if cust_res.data:
                    customer_name = cust_res.data.get("name") or "Unknown Customer"
            except Exception as e:
                logger.warning(f"Failed to resolve customer name: {e}")

        # [FIX] Smart Title Generation
        # If title is missing or generic (UNKNOWN), regenerate it using the real name
        final_title = data.title
        is_placeholder = final_title and ("UNKNOWN" in final_title or "New Ticket" in final_title)
        
        if not final_title or is_placeholder:
            priority_val = data.priority.value if hasattr(data.priority, "value") else str(data.priority)
            
            # Create description snippet
            desc_text = data.description or "No Content"
            snippet = desc_text[:30] + "..." if len(desc_text) > 30 else desc_text
            
            # Format: [LOW] John Doe - Issue Description...
            final_title = f"[{priority_val.upper()}] {customer_name} - {snippet}"

        # 2. Insert Data
        insert_data = {
            "organization_id": organization_id,
            "customer_id": data.customer_id,
            "chat_id": data.chat_id,
            "ticket_number": ticket_num,
            "title": final_title,  # <--- Using the fixed title
            "description": data.description,
            "category": data.category,
            "priority": data.priority.value if hasattr(data.priority, "value") else data.priority,
            "status": TicketStatus.OPEN.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        res = self.supabase.table("tickets").insert(insert_data).execute()
        if not res.data: 
            raise Exception("Failed to insert ticket")
        
        new_ticket = Ticket(**res.data[0])

        # 3. Log & Broadcast
        await self.log_activity(
            ticket_id=new_ticket.id, 
            action="created", 
            description=f"Ticket created by {actor_type.value}", 
            actor_id=actor_id, 
            actor_type=actor_type
        )

        try:
            conn = get_connection_manager()
            await conn.broadcast_chat_update(
                organization_id=organization_id,
                chat_id=new_ticket.chat_id,
                update_type="ticket_created",  
                data={
                    "ticket_id": new_ticket.id,
                    "ticket_number": new_ticket.ticket_number,
                    "status": new_ticket.status,
                    "priority": new_ticket.priority
                }
            )
        except Exception: pass

        return new_ticket

# ============================================
# CUSTOMER ENDPOINTS
# ============================================

@router.get(
    "/customers",
    response_model=CustomerListResponse,
    summary="Get all customers",
    description="Retrieve all customers for the organization"
)
async def get_customers(
    search: Optional[str] = Query(None, description="Search by name, email, or phone"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=100, description="Number of records to return"),
    current_user: User = Depends(get_current_user)
):
    """Get all customers for organization"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Build query
        query = supabase.table("customers").select("*", count="exact").eq("organization_id", organization_id)

        # Apply search filter
        if search:
            query = query.or_(f"name.ilike.%{search}%,email.ilike.%{search}%,phone.ilike.%{search}%")

        # Apply pagination
        query = query.range(skip, skip + limit - 1).order("created_at", desc=True)

        # Execute query
        response = query.execute()

        customers = [Customer(**customer) for customer in response.data]

        return CustomerListResponse(
            customers=customers,
            total=response.count if response.count else len(customers)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch customers"
        )


@router.get(
    "/customers/{customer_id}",
    response_model=Customer,
    summary="Get customer by ID",
    description="Retrieve a specific customer by ID"
)
async def get_customer(
    customer_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get specific customer by ID"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        response = supabase.table("customers").select("*").eq("id", customer_id).eq("organization_id", organization_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with ID {customer_id} not found"
            )

        return Customer(**response.data[0])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer {customer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch customer"
        )


@router.post(
    "/customers",
    response_model=Customer,
    status_code=status.HTTP_201_CREATED,
    summary="Create new customer",
    description="Create a new customer in the organization"
)
async def create_customer(
    customer: CustomerCreate,
    current_user: User = Depends(get_current_user)
):
    """Create a new customer"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Prepare customer data
        customer_data = {
            "organization_id": organization_id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "avatar_url": customer.avatar_url,
            "metadata": customer.metadata
        }

        # Insert customer
        response = supabase.table("customers").insert(customer_data).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create customer"
            )

        logger.info(f"Customer created: {response.data[0]['id']} by user {current_user.user_id}")

        return Customer(**response.data[0])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating customer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create customer"
        )


@router.put(
    "/customers/{customer_id}",
    response_model=Customer,
    summary="Update customer",
    description="Update an existing customer's information"
)
async def update_customer(
    customer_id: str,
    customer_update: CustomerUpdate,
    current_user: User = Depends(get_current_user)
):
    """Update an existing customer"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Check if customer exists
        existing = supabase.table("customers").select("*").eq("id", customer_id).eq("organization_id", organization_id).execute()

        if not existing.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with ID {customer_id} not found"
            )

        # Prepare update data
        update_data = customer_update.model_dump(exclude_unset=True)

        if not update_data:
            return Customer(**existing.data[0])

        # Update customer
        response = supabase.table("customers").update(update_data).eq("id", customer_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update customer"
            )

        logger.info(f"Customer updated: {customer_id} by user {current_user.user_id}")

        return Customer(**response.data[0])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer {customer_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update customer"
        )


# ============================================
# CHAT ENDPOINTS
# ============================================

@router.get(
    "/chats",
    response_model=ChatListResponse,
    summary="Get all chats",
    description="Retrieve all chats with optional filtering"
)
async def get_chats(
    status_filter: Optional[ChatStatus] = Query(None, description="Filter by chat status"),
    channel: Optional[CommunicationChannel] = Query(None, description="Filter by channel"),
    assigned_to: Optional[str] = Query(None, description="Filter by assigned agent ID (legacy)"),
    unassigned: Optional[bool] = Query(None, description="Filter unassigned chats"),
    handled_by: Optional[str] = Query(None, description="Filter by handler: ai, human, or unassigned"),
    ai_assigned_to: Optional[str] = Query(None, description="Filter by AI agent ID"),
    human_assigned_to: Optional[str] = Query(None, description="Filter by human agent ID"),
    escalated: Optional[bool] = Query(None, description="Filter escalated chats only"),
    # [NEW] Date Filtering Parameters
    created_after: Optional[datetime] = Query(None, description="Filter chats created after this timestamp (ISO 8601)"),
    created_before: Optional[datetime] = Query(None, description="Filter chats created before this timestamp (ISO 8601)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=100, description="Number of records to return"),
    current_user: User = Depends(get_current_user)
):
    """Get all chats with filters"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Build query
        query = supabase.table("chats").select("*", count="exact").eq("organization_id", organization_id)

        # Apply filters
        if status_filter:
            query = query.eq("status", status_filter.value)

        if channel:
            query = query.eq("channel", channel.value)

        # Legacy filter (backward compatibility)
        if assigned_to:
            query = query.eq("assigned_agent_id", assigned_to)

        if unassigned is True:
            query = query.is_("assigned_agent_id", "null")

        # New dual agent filters
        if handled_by:
            query = query.eq("handled_by", handled_by)

        if ai_assigned_to:
            query = query.eq("ai_agent_id", ai_assigned_to)

        if human_assigned_to:
            query = query.eq("human_agent_id", human_assigned_to)

        if escalated is True:
            query = query.not_.is_("escalated_at", "null")

        # [NEW] Apply Date Range Filters
        if created_after:
            query = query.gte("created_at", created_after.isoformat())
        
        if created_before:
            query = query.lte("created_at", created_before.isoformat())

        # Apply pagination
        query = query.range(skip, skip + limit - 1).order("last_message_at", desc=True)

        # Execute query
        response = query.execute()

        # Fetch last message and customer name for each chat
        chats_with_messages = []

        for chat_data in response.data:
            chat_id = chat_data["id"]
            customer_id = chat_data.get("customer_id")

            # Get last message for this chat
            last_message_response = supabase.table("messages") \
                .select("*") \
                .eq("chat_id", chat_id) \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()

            # Add last_message to chat data
            if last_message_response.data:
                chat_data["last_message"] = last_message_response.data[0]
            else:
                chat_data["last_message"] = None

            # Get customer name
            customer_name = None
            if customer_id:
                try:
                    customer_response = supabase.table("customers") \
                        .select("name") \
                        .eq("id", customer_id) \
                        .eq("organization_id", organization_id) \
                        .execute()

                    if customer_response.data:
                        customer_name = customer_response.data[0].get("name")
                except Exception as e:
                    logger.warning(f"Failed to fetch customer name for customer_id {customer_id}: {e}")
                    customer_name = None

            # Add customer_name to chat data
            chat_data["customer_name"] = customer_name

            # Get legacy agent name (for backward compatibility)
            agent_name = None
            assigned_agent_id = chat_data.get("assigned_agent_id")
            if assigned_agent_id:
                try:
                    agent_response = supabase.table("agents") \
                        .select("name") \
                        .eq("id", assigned_agent_id) \
                        .eq("organization_id", organization_id) \
                        .execute()

                    if agent_response.data:
                        agent_name = agent_response.data[0].get("name")
                except Exception as e:
                    logger.warning(f"Failed to fetch agent name for assigned_agent_id {assigned_agent_id}: {e}")
                    agent_name = None

            # Add agent_name to chat data
            chat_data["agent_name"] = agent_name

            # Get AI agent name
            ai_agent_name = None
            ai_agent_id = chat_data.get("ai_agent_id")
            if ai_agent_id:
                try:
                    ai_agent_response = supabase.table("agents") \
                        .select("name") \
                        .eq("id", ai_agent_id) \
                        .eq("organization_id", organization_id) \
                        .execute()

                    if ai_agent_response.data:
                        ai_agent_name = ai_agent_response.data[0].get("name")
                except Exception as e:
                    logger.warning(f"Failed to fetch AI agent name for ai_agent_id {ai_agent_id}: {e}")
                    ai_agent_name = None

            # Add ai_agent_name to chat data
            chat_data["ai_agent_name"] = ai_agent_name

            # Get human agent name
            human_agent_name = None
            human_id = None
            human_agent_id = chat_data.get("human_agent_id")
            if human_agent_id:
                try:
                    human_agent_response = supabase.table("agents") \
                        .select("name","user_id") \
                        .eq("id", human_agent_id) \
                        .eq("organization_id", organization_id) \
                        .execute()

                    if human_agent_response.data:
                        human_agent_name = human_agent_response.data[0].get("name")
                        human_id = human_agent_response.data[0].get("user_id")
                except Exception as e:
                    logger.warning(f"Failed to fetch human agent name for human_agent_id {human_agent_id}: {e}")
                    human_agent_name = None

            # Add human_agent_name to chat data
            chat_data["human_agent_name"] = human_agent_name
            chat_data["human_id"] = human_id

            chats_with_messages.append(Chat(**chat_data))

        return ChatListResponse(
            chats=chats_with_messages,
            total=response.count if response.count else len(chats_with_messages)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching chats: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch chats"
        )


@router.get(
    "/chats/{chat_id}",
    response_model=Chat,
    summary="Get chat by ID",
    description="Retrieve a specific chat by ID"
)
async def get_chat(
    chat_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get specific chat by ID"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        response = supabase.table("chats").select("*").eq("id", chat_id).eq("organization_id", organization_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat with ID {chat_id} not found"
            )

        chat_data = response.data[0]

        # Get last message for this chat
        last_message_response = supabase.table("messages") \
            .select("*") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        # Add last_message to chat data
        if last_message_response.data:
            chat_data["last_message"] = last_message_response.data[0]
        else:
            chat_data["last_message"] = None

        return Chat(**chat_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching chat {chat_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch chat"
        )

@router.post(
    "/chats",
    response_model=Chat,
    status_code=status.HTTP_201_CREATED,
    summary="Create new chat",
    description="Atomic: Verify Sender -> Resolve Customer -> Create Chat -> Send"
)
async def create_chat(
    chat: ChatCreate,
    current_user: User = Depends(get_current_user)
):
    try:
        org_service = get_organization_service()
        user_org = await org_service.get_user_organization(current_user.user_id)
        if not user_org: raise HTTPException(400, "No Organization")
        organization_id = user_org.id
        supabase = get_supabase_client()
        
        logger.info(f"🚀 [create_chat] Contact: {chat.contact}, Channel: {chat.channel}")

        # Normalize Channel Value safely
        channel_val = chat.channel.value if hasattr(chat.channel, "value") else chat.channel

        # ==============================================================================
        # 1. RESOLVE AGENT & SENDER FIRST (READ ONLY - NO WRITES YET)
        # ==============================================================================
        ai_agent_id, human_agent_id = None, None
        assigned_agent_id, sender_agent_id = None, None
        handled_by = "unassigned"
        status_value = "open"

        # A. Resolve Gateway (The Pipe) - SMART LOGIC
        if chat.using_agent_integration_id:
            target_id = chat.using_agent_integration_id
            logger.info(f"🔌 Resolving Gateway for: {target_id}")
            
            # 1. Try by Integration ID
            int_res = supabase.table("agent_integrations") \
                .select("agent_id, channel, enabled") \
                .eq("id", target_id) \
                .execute()
            
            # 2. Fallback: Try by Agent ID
            if not int_res.data:
                logger.info("...ID not found, trying as Agent ID...")
                int_res = supabase.table("agent_integrations") \
                    .select("agent_id, channel, enabled") \
                    .eq("agent_id", target_id) \
                    .eq("channel", channel_val) \
                    .execute()

            if int_res.data and len(int_res.data) > 0:
                integration = int_res.data[0]
                if integration["channel"] != channel_val:
                    raise HTTPException(400, f"Integration supports {integration['channel']}, but chat is {channel_val}")
                if not integration["enabled"]:
                     raise HTTPException(400, "Selected integration is disabled")
                
                # SET SENDER AGENT
                sender_agent_id = integration["agent_id"]
                logger.info(f"✅ Gateway Resolved: Agent {sender_agent_id} via {channel_val}")
            else:
                logger.warning(f"Gateway {target_id} not found")
                raise HTTPException(404, "Gateway Integration not found")

        # B. Resolve Default AI (Read Only)
        default_ai = None
        ai_res = supabase.table("agents").select("id").eq("organization_id", organization_id).is_("user_id", "null").eq("status", "active").limit(1).execute()
        if ai_res.data: default_ai = ai_res.data[0]["id"]

        # C. Resolve Handler (The Person)
        if chat.assigned_agent_id:
            target_id = chat.assigned_agent_id
            
            if target_id == "me":
                me_res = supabase.table("agents").select("id").eq("user_id", current_user.user_id).eq("organization_id", organization_id).execute()
                if me_res.data: target_id = me_res.data[0]["id"]
                else: raise HTTPException(400, "You do not have an Agent profile.")

            agent_check = supabase.table("agents").select("id", "user_id").eq("organization_id", organization_id).eq("id", target_id).execute()
            
            if agent_check.data:
                agent = agent_check.data[0]
                assigned_agent_id = agent["id"]
                
                if agent["user_id"]: # Human
                    human_agent_id = assigned_agent_id
                    handled_by = "human"
                    status_value = "assigned"
                    if default_ai: ai_agent_id = default_ai
                    
                    # [GATEWAY LOGIC] Only check personal integration if Gateway NOT set
                    if not sender_agent_id:
                        int_check = supabase.table("agent_integrations").select("id").eq("agent_id", assigned_agent_id).eq("channel", channel_val).eq("enabled", True).execute()
                        if int_check.data:
                            sender_agent_id = assigned_agent_id 
                        else:
                            raise HTTPException(400, f"Agent has no personal {channel_val} account. Please select a Gateway Integration.")
                else: # AI
                    ai_agent_id = assigned_agent_id
                    handled_by = "ai"
                    status_value = "assigned"
                    if not sender_agent_id: sender_agent_id = assigned_agent_id
            else:
                raise HTTPException(404, "Assigned Agent not found")

        # D. Fallback Logic
        if not assigned_agent_id:
            if sender_agent_id:
                # Gateway + No Agent -> Shared Inbox
                status_value = "open"
                handled_by = "unassigned"
            elif default_ai:
                # No Gateway + Default AI -> Assigned to AI
                assigned_agent_id = default_ai
                handled_by = "ai"
                status_value = "assigned"
                sender_agent_id = default_ai
            else:
                raise HTTPException(400, "No valid Sender (Integration) available.")

        # ==============================================================================
        # 2. RESOLVE CUSTOMER (SAFE NOW)
        # ==============================================================================
        customer_id = chat.customer_id
        
        if not customer_id:
            if not chat.contact: raise HTTPException(400, "Contact required")
            
            clean_input = re.sub(r'[^\d]', '', chat.contact)
            
            # A. Check Metadata
            if channel_val == "telegram":
                meta_q = supabase.table("customers").select("id").eq("organization_id", organization_id).contains("metadata", {"telegram_id": clean_input}).execute()
                if meta_q.data: customer_id = meta_q.data[0]["id"]
            elif channel_val == "whatsapp":
                meta_q = supabase.table("customers").select("id").eq("organization_id", organization_id).contains("metadata", {"whatsapp_id": clean_input}).execute()
                if meta_q.data: customer_id = meta_q.data[0]["id"]

            # B. Check Columns
            if not customer_id:
                query = supabase.table("customers").select("id").eq("organization_id", organization_id)
                if "@" in chat.contact:
                    existing = query.eq("email", chat.contact).execute()
                else:
                    no_prefix = clean_input[2:] if clean_input.startswith('62') else clean_input
                    or_query = f"phone.eq.{chat.contact},phone.eq.{clean_input},phone.eq.0{no_prefix},phone.eq.62{no_prefix}"
                    existing = query.or_(or_query).execute()
                if existing.data: customer_id = existing.data[0]["id"]

            # C. Create New
            if not customer_id:
                cust_metadata = {"source": "dashboard_create"}
                final_phone = None
                final_email = None

                if "@" in chat.contact:
                    final_email = chat.contact
                else:
                    final_phone = chat.contact
                    if channel_val == "telegram": cust_metadata["telegram_id"] = clean_input
                    elif channel_val == "whatsapp": cust_metadata["whatsapp_id"] = clean_input

                new_cust = supabase.table("customers").insert({
                    "organization_id": organization_id,
                    "name": chat.customer_name or "New Customer",
                    "phone": final_phone,
                    "email": final_email,
                    "metadata": cust_metadata
                }).execute()
                
                if new_cust.data: customer_id = new_cust.data[0]["id"]
                else: raise HTTPException(500, "Failed to create customer")

        # ==============================================================================
        # 3. REUSE OR CREATE CHAT
        # ==============================================================================
        active_chat = supabase.table("chats").select("*").eq("customer_id", customer_id).eq("channel", channel_val).neq("status", "resolved").neq("status", "closed").execute()
        
        chat_obj = None
        if active_chat.data:
            chat_obj = active_chat.data[0]
            logger.info(f"♻️ Reusing Chat {chat_obj['id']}")
            
            upd = {"sender_agent_id": sender_agent_id}
            
            if assigned_agent_id:
                upd.update({
                    "status": status_value,
                    "handled_by": handled_by,
                    "assigned_agent_id": assigned_agent_id
                })
                if human_agent_id: upd["human_agent_id"] = human_agent_id
                if ai_agent_id: upd["ai_agent_id"] = ai_agent_id
            elif handled_by == "unassigned":
                upd.update({
                    "status": "open",
                    "handled_by": "unassigned",
                    "assigned_agent_id": None,
                    "human_agent_id": None
                })
            
            supabase.table("chats").update(upd).eq("id", chat_obj["id"]).execute()
            chat_obj.update(upd)
        else:
            new_chat_data = {
                "organization_id": organization_id, "customer_id": customer_id, "channel": channel_val,
                "assigned_agent_id": assigned_agent_id, "ai_agent_id": ai_agent_id, "human_agent_id": human_agent_id,
                "handled_by": handled_by, "status": status_value, "sender_agent_id": sender_agent_id,
                "unread_count": 0, "last_message_at": datetime.utcnow().isoformat()
            }
            res = supabase.table("chats").insert(new_chat_data).execute()
            chat_obj = res.data[0]

        # ==============================================================================
        # 4. SEND MESSAGE & BROADCAST
        # ==============================================================================
        if chat.initial_message:
            await create_message_internal(chat_obj, chat.initial_message, current_user.user_id, supabase)

        # 5. WEBSOCKET BROADCAST
        if app_settings.WEBSOCKET_ENABLED:
            try:
                conn = get_connection_manager()
                if chat.initial_message:
                    sender_name = "AI Assistant"
                    if handled_by == "human": sender_name = "Human Agent"
                    
                    await conn.broadcast_new_message(
                        organization_id=organization_id,
                        chat_id=chat_obj["id"],
                        message_id=uuid4().hex,
                        customer_id=customer_id,
                        customer_name=chat.customer_name or "Customer",
                        message_content=chat.initial_message,
                        channel=channel_val,
                        handled_by=handled_by,
                        sender_type="agent",
                        sender_id=sender_agent_id or current_user.user_id,
                        sender_name=sender_name,
                        is_new_chat=True,
                        was_reopened=False
                    )
                else:
                    await conn.broadcast_chat_update(
                        organization_id=organization_id,
                        chat_id=chat_obj["id"],
                        update_type="chat_created",
                        data=chat_obj
                    )
            except Exception as e:
                logger.warning(f"WS Broadcast failed: {e}")

        chat_obj["last_message"] = None
        return Chat(**chat_obj)

    except HTTPException as he: raise he
    except Exception as e:
        logger.error(f"Create chat error: {e}", exc_info=True)
        raise HTTPException(500, "Failed to create chat")

@router.put(
    "/chats/{chat_id}/assign",
    response_model=Chat,
    summary="Assign chat to agent",
    description="Assigns chat to agent (Gateway Model). Preserves original sender_agent_id for connectivity."
)
async def assign_chat(
    chat_id: str,
    assignment: ChatAssign,
    current_user: User = Depends(get_current_user)
):
    """
    Assign chat to an agent.
    
    GATEWAY ARCHITECTURE:
    - Changes 'assigned_agent_id' (Who is handling it).
    - PRESERVES 'sender_agent_id' (Who owns the WhatsApp/Telegram connection).
    - Auto-syncs the customer's active ticket to the new agent.
    """
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. Get Chat & Channel Info
        chat_check = supabase.table("chats").select("*").eq("id", chat_id).eq("organization_id", organization_id).execute()

        if not chat_check.data:
            raise HTTPException(404, f"Chat {chat_id} not found")
        
        existing_chat = chat_check.data[0]
        customer_id = existing_chat.get("customer_id")

        # 2. Resolve Target Agent ID
        target_agent_id = None

        if assignment.assigned_to_me:
            # Logic: Find my agent -> Find by Email -> Create New
            agent_response = supabase.table("agents") \
				.select("id") \
				.eq("user_id", current_user.user_id) \
				.eq("organization_id", organization_id) \
				.execute()

            if not agent_response.data:
                # Try Email Link
                user_email = current_user.user_metadata.get("email")
                if user_email:
                    email_check = supabase.table("agents").select("id").eq("email", user_email).eq("organization_id", organization_id).execute()
                    if email_check.data:
                        # Link orphan
                        target_agent_id = email_check.data[0]['id']
                        supabase.table("agents").update({"user_id": current_user.user_id, "status": "active"}).eq("id", target_agent_id).execute()
            
            # Create New if still missing
            if not target_agent_id and not agent_response.data:
                agent_data = {
					"organization_id": organization_id,
					"name": current_user.user_metadata.get("full_name") or "Unnamed Agent",
					"user_id": current_user.user_id,
                    "email": current_user.user_metadata.get("email") or f"{current_user.user_id}@example.com",
                    "phone": current_user.user_metadata.get("phone") or f"000",
					"status": "active",
				}
                create_res = supabase.table("agents").insert(agent_data).execute()
                if not create_res.data: raise HTTPException(500, "Failed to create agent")
                target_agent_id = create_res.data[0]["id"]
            
            elif agent_response.data:
                target_agent_id = agent_response.data[0]["id"]
        else:
            target_agent_id = assignment.assigned_agent_id

        # 3. Verify Agent Exists
        if not target_agent_id: raise HTTPException(400, "No agent identified for assignment")
        
        agent_check = supabase.table("agents").select("id","user_id","name").eq("id", target_agent_id).eq("organization_id", organization_id).execute()
        if not agent_check.data: raise HTTPException(404, "Agent not found")
        
        target_agent = agent_check.data[0]

        # 4. Prepare Update Data
        # IMPORTANT: We do NOT update sender_agent_id. We keep the gateway.
        handle_by = "human" if target_agent.get("user_id") else "ai"
        
        update_data = {
            "assigned_agent_id": target_agent_id,
            "status": "assigned",
            "handled_by": handle_by,
        }
        
        if handle_by == "human":
            update_data["human_agent_id"] = target_agent_id

        # 5. Execute Chat Update
        response = supabase.table("chats").update(update_data).eq("id", chat_id).execute()
        if not response.data: raise HTTPException(500, "Failed to assign chat")

        # 6. Sync Ticket
        if customer_id:
            try:
                open_ticket = supabase.table("tickets") \
                    .select("id") \
                    .eq("customer_id", customer_id) \
                    .neq("status", "resolved") \
                    .neq("status", "closed") \
                    .order("created_at", desc=True) \
                    .limit(1) \
                    .execute()

                if open_ticket.data:
                    supabase.table("tickets").update({
                        "assigned_agent_id": target_agent_id,
                        "updated_at": datetime.utcnow().isoformat()
                    }).eq("id", open_ticket.data[0]["id"]).execute()
                    logger.info(f"🔄 Synced Ticket {open_ticket.data[0]['id']}")
            except Exception as e:
                logger.warning(f"Ticket sync warning: {e}")

        # Return result
        chat_data = response.data[0]
        
        # Enrich with Last Message
        last_msg = supabase.table("messages").select("*").eq("chat_id", chat_id).order("created_at", desc=True).limit(1).execute()
        chat_data["last_message"] = last_msg.data[0] if last_msg.data else None
        
        return Chat(**chat_data)

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error assigning chat: {e}")
        raise HTTPException(500, f"Assignment failed: {str(e)}")


@router.put(
    "/chats/{chat_id}/escalate",
    response_model=Chat,
    summary="Escalate chat to human agent",
    description="Escalate a chat from AI agent to human agent. AI agent info is preserved."
)
async def escalate_chat(
    chat_id: str,
    escalation: ChatEscalation,
    current_user: User = Depends(get_current_user)
):
    """Escalate chat from AI to human agent"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Check if chat exists and belongs to organization
        chat_check = supabase.table("chats").select("*").eq("id", chat_id).eq("organization_id", organization_id).execute()

        if not chat_check.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat with ID {chat_id} not found"
            )

        existing_chat = chat_check.data[0]

        # Check if chat is already handled by human
        if existing_chat.get("handled_by") == "human":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chat is already being handled by a human agent"
            )

        # Verify human agent exists and is actually a human (user_id NOT NULL)
        agent_check = supabase.table("agents") \
            .select("id", "user_id") \
            .eq("id", escalation.human_agent_id) \
            .eq("organization_id", organization_id) \
            .execute()

        if not agent_check.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent with ID {escalation.human_agent_id} not found"
            )

        agent_data = agent_check.data[0]

        # Ensure it's a human agent (not AI)
        if agent_data.get("user_id") is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot escalate to an AI agent. Please provide a human agent ID."
            )

        # Prepare escalation update
        update_data = {
            "human_agent_id": escalation.human_agent_id,
            "handled_by": "human",
            "status": "assigned",
            "assigned_agent_id": escalation.human_agent_id,  # Update for backward compatibility
            "escalated_at": datetime.utcnow().isoformat(),
            "escalation_reason": escalation.reason
        }

        # IMPORTANT: ai_agent_id is NOT updated - it's preserved!
        # IMPORTANT: sender_agent_id is NOT updated - it's preserved!
        # This ensures replies are sent from the same WhatsApp number/Telegram bot/Email

        # Update chat
        response = supabase.table("chats").update(update_data).eq("id", chat_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to escalate chat"
            )

        logger.info(
            f"Chat {chat_id} escalated from AI (preserved: {existing_chat.get('ai_agent_id')}) "
            f"to human agent {escalation.human_agent_id} by user {current_user.user_id}. "
            f"Sender agent preserved: {existing_chat.get('sender_agent_id')} "
            f"(WhatsApp/Telegram/Email will remain same for customer)"
        )

        chat_data = response.data[0]

        # Get last message for this chat
        last_message_response = supabase.table("messages") \
            .select("*") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        # Add last_message to chat data
        if last_message_response.data:
            chat_data["last_message"] = last_message_response.data[0]
        else:
            chat_data["last_message"] = None

        # Fetch AI agent name (preserved)
        ai_agent_name = None
        if chat_data.get("ai_agent_id"):
            try:
                ai_agent_response = supabase.table("agents") \
                    .select("name") \
                    .eq("id", chat_data["ai_agent_id"]) \
                    .execute()
                if ai_agent_response.data:
                    ai_agent_name = ai_agent_response.data[0].get("name")
            except Exception as e:
                logger.warning(f"Failed to fetch AI agent name: {e}")

        chat_data["ai_agent_name"] = ai_agent_name

        # Fetch human agent name
        human_agent_name = None
        try:
            human_agent_response = supabase.table("agents") \
                .select("name") \
                .eq("id", escalation.human_agent_id) \
                .execute()
            if human_agent_response.data:
                human_agent_name = human_agent_response.data[0].get("name")
        except Exception as e:
            logger.warning(f"Failed to fetch human agent name: {e}")

        chat_data["human_agent_name"] = human_agent_name

        # Fetch customer name
        customer_name = None
        if chat_data.get("customer_id"):
            try:
                customer_response = supabase.table("customers") \
                    .select("name") \
                    .eq("id", chat_data["customer_id"]) \
                    .execute()
                if customer_response.data:
                    customer_name = customer_response.data[0].get("name")
            except Exception as e:
                logger.warning(f"Failed to fetch customer name: {e}")

        chat_data["customer_name"] = customer_name
        chat_data["agent_name"] = human_agent_name  # For backward compatibility

        return Chat(**chat_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error escalating chat {chat_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to escalate chat"
        )


@router.put(
    "/chats/{chat_id}/resolve",
    response_model=Chat,
    summary="Resolve chat",
    description="Mark a chat as resolved"
)
async def resolve_chat(
    chat_id: str,
    current_user: User = Depends(get_current_user)
):
    """Mark chat as resolved"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Check if chat exists
        chat_check = supabase.table("chats").select("*").eq("id", chat_id).eq("organization_id", organization_id).execute()

        if not chat_check.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat with ID {chat_id} not found"
            )

        # Update chat
        update_data = {
            "status": "resolved",
            "resolved_at": datetime.utcnow().isoformat(),
            "resolved_by_agent_id": chat_check.data[0].get("assigned_agent_id")
        }

        response = supabase.table("chats").update(update_data).eq("id", chat_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resolve chat"
            )

        logger.info(f"Chat {chat_id} resolved by user {current_user.user_id}")

        chat_data = response.data[0]

        # Get last message for this chat
        last_message_response = supabase.table("messages") \
            .select("*") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        # Add last_message to chat data
        if last_message_response.data:
            chat_data["last_message"] = last_message_response.data[0]
        else:
            chat_data["last_message"] = None

        return Chat(**chat_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving chat {chat_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve chat"
        )


# ============================================
# MESSAGE ENDPOINTS
# ============================================

@router.get(
    "/chats/{chat_id}/messages",
    response_model=MessageListResponse,
    summary="Get chat messages",
    description="Retrieve all messages in a chat"
)
async def get_chat_messages(
    chat_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user)
):
    """Get all messages in a chat"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Verify chat
        chat_check = supabase.table("chats").select("id").eq("id", chat_id).eq("organization_id", organization_id).execute()
        if not chat_check.data: 
            raise HTTPException(404, f"Chat {chat_id} not found")

        # Get messages
        response = supabase.table("messages").select("*", count="exact").eq("chat_id", chat_id).range(skip, skip + limit - 1).order("created_at", desc=False).execute()

        # Enrich messages with sender_name
        messages_with_sender = []
        for message_data in response.data:
            sender_name = None
            sender_id = message_data.get("sender_id")
            sender_type = message_data.get("sender_type")

            if sender_id and sender_type:
                if sender_type == "agent":
                    # [FIX] Fetch Email from Agents table instead of Name
                    try:
                        agent_response = supabase.table("agents") \
                            .select("email") \
                            .eq("user_id", sender_id) \
                            .eq("organization_id", organization_id) \
                            .execute()
                        
                        # Use Email if available
                        if agent_response.data and agent_response.data[0].get("email"):
                            sender_name = agent_response.data[0].get("email")
                        else:
                            # Fallback if email is missing in agents table
                            sender_name = "Human Agent"
                            
                    except Exception:
                        sender_name = "Human Agent"

                elif sender_type == "ai":
                    try:
                        agent_response = supabase.table("agents").select("name").eq("id", sender_id).execute()
                        sender_name = agent_response.data[0].get("name") if agent_response.data else "AI Assistant"
                    except:
                        sender_name = "AI Assistant"

                elif sender_type == "customer":
                    try:
                        cust_response = supabase.table("customers").select("name").eq("id", sender_id).execute()
                        sender_name = cust_response.data[0].get("name") if cust_response.data else "Customer"
                    except:
                        sender_name = "Customer"

            message_data["sender_name"] = sender_name
            messages_with_sender.append(Message(**message_data))

        return MessageListResponse(
            messages=messages_with_sender,
            total=response.count if response.count else len(messages_with_sender)
        )

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        raise HTTPException(500, "Failed to fetch messages")


@router.post(
    "/chats/{chat_id}/messages",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    summary="Send message (Text or File)",
    description="Send a new message. Supports Multipart/Form-Data for file uploads."
)
async def create_message(
    chat_id: str,
    content: str = Form(...),
    sender_type: str = Form(...),
    sender_id: str = Form(...),
    ticket_id: Optional[str] = Form(None),
    metadata: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user)
):
    """Send a message (Text or File) in a chat"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()
        
        # Verify chat
        chat_check = supabase.table("chats").select("id, customer_id, channel, handled_by").eq("id", chat_id).eq("organization_id", organization_id).execute()
        if not chat_check.data: raise HTTPException(404, f"Chat {chat_id} not found")
        chat_data_minimal = chat_check.data[0]

        # Parse Metadata
        msg_metadata = {}
        if metadata:
            try:
                msg_metadata = json.loads(metadata)
            except Exception:
                logger.warning("Invalid metadata JSON, ignoring")

        # ============================================
        # 1. HANDLE FILE UPLOAD
        # ============================================
        if file:
            try:
                storage = get_storage_service(supabase)
                file_ext = file.filename.split('.')[-1] if '.' in file.filename else "bin"
                unique_filename = f"{uuid4()}.{file_ext}"
                file_path = f"chat-media/{chat_id}" 

                file_content = await file.read()
                
                storage.upload_file(
                    organization_id=organization_id, 
                    file_id=unique_filename,
                    file_content=file_content,
                    filename=file.filename,
                    folder_path=file_path,
                    mime_type=file.content_type
                )
                
                public_url = storage.get_public_url(
                    organization_id=organization_id,
                    file_id=unique_filename,
                    folder_path=file_path
                )

                msg_metadata["media_url"] = public_url
                msg_metadata["filename"] = file.filename
                msg_metadata["media_type"] = file.content_type
                msg_metadata["is_document"] = not file.content_type.startswith("image/") and not file.content_type.startswith("video/")
                
                logger.info(f"✅ File uploaded: {public_url}")

            except Exception as e:
                logger.error(f"❌ File upload failed: {e}")
                raise HTTPException(500, f"File upload failed: {str(e)}")

        # ============================================
        # 2. DB INSERT
        # ============================================
        message_data = {
            "chat_id": chat_id,
            "sender_type": sender_type,
            "sender_id": sender_id,
            "content": content,
            "ticket_id": ticket_id,
            "metadata": msg_metadata
        }
        
        response = supabase.table("messages").insert(message_data).execute()
        if not response.data: raise HTTPException(500, "Failed to create message")
        
        supabase.table("chats").update({"last_message_at": datetime.utcnow().isoformat()}).eq("id", chat_id).execute()
        
        logger.info(f"Message created in chat {chat_id}")

        # ============================================
        # 3. SEND EXTERNAL (The Critical Part)
        # ============================================
        if sender_type in ["agent", "ai"]:
            try:
                chat_full = supabase.table("chats").select("*").eq("id", chat_id).single().execute()
                if chat_full.data:
                    chat_data = chat_full.data
                    cust_data = supabase.table("customers").select("*").eq("id", chat_data["customer_id"]).single().execute()
                    
                    if cust_data.data:
                        # [DEBUG LOG]
                        logger.info(f"📤 Attempting to send to {chat_data.get('channel')}...")
                        
                        result = await send_message_via_channel(
                            chat_data=chat_data,
                            customer_data=cust_data.data,
                            message_content=content,
                            supabase=supabase,
                            message_metadata=msg_metadata 
                        )
                        
                        # [DEBUG LOG] Check the result!
                        if result.get("success"):
                            logger.info(f"✅ External Send Success: {result}")
                        else:
                            logger.error(f"❌ External Send Failed: {result.get('message')}")
            except Exception as e:
                logger.error(f"❌ Critical Error sending external message: {e}")

        # ============================================
        # 4. BROADCAST (Fixed)
        # ============================================
        created_message = response.data[0]
        sender_name = None
        
        # Resolve Sender Name (Simplified)
        if sender_type == "agent":
            sender_name = "Human Agent" # Simplify for speed, or fetch if needed
        elif sender_type == "ai":
            sender_name = "AI Assistant"
        elif sender_type == "customer":
            sender_name = "Customer"
            
        created_message["sender_name"] = sender_name
        
        # Extract attachment for response model
        attachment_obj = _extract_attachment(created_message.get("metadata"))
        created_message["attachment"] = attachment_obj

        if app_settings.WEBSOCKET_ENABLED:
            try:
                conn = get_connection_manager()
                ws_cust_id = chat_data_minimal.get("customer_id")
                
                broadcast_name = sender_name
                if sender_type == "customer":
                    c_res = supabase.table("customers").select("name").eq("id", ws_cust_id).single().execute()
                    broadcast_name = c_res.data.get("name") if c_res.data else "Unknown Customer"

                # [FIX] Convert Pydantic model to Dict
                attach_dict = attachment_obj.model_dump() if attachment_obj else None

                await conn.broadcast_new_message(
                    organization_id=organization_id,
                    chat_id=chat_id,
                    message_id=created_message["id"],
                    customer_id=ws_cust_id,
                    customer_name=broadcast_name, 
                    message_content=content,
                    channel=chat_data_minimal.get("channel"),
                    handled_by=chat_data_minimal.get("handled_by"),
                    sender_type=sender_type,
                    sender_id=sender_id,
                    attachment=attach_dict  # <--- NOW IT WILL WORK
                )
            except Exception as e:
                logger.warning(f"WS Broadcast failed: {e}")

        return Message(**created_message)

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Create message error: {e}")
        raise HTTPException(500, "Failed to create message")
    
# ============================================
# TICKET ENDPOINTS
# ============================================


@router.get(
    "/tickets",
    response_model=TicketListResponse,
    summary="Get all tickets",
    description="Retrieve all tickets with optional filtering, joined with Customer and Channel data."
)
async def get_tickets(
    status_filter: Optional[TicketStatus] = Query(None, description="Filter by ticket status"),
    priority: Optional[TicketPriority] = Query(None, description="Filter by priority"),
    category: Optional[str] = Query(None, description="Filter by category"),
    assigned_to: Optional[str] = Query(None, description="Filter by assigned agent ID"),
    chat_id: Optional[str] = Query(None, description="Filter tickets by specific chat ID"),
    customer_id: Optional[str] = Query(None, description="Filter tickets by specific customer ID"),
    updated_after: Optional[datetime] = Query(None),
    created_after: Optional[datetime] = Query(None),
    sort_by: str = Query("updated_at"),
    sort_order: str = Query("desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user)
):
    """Get all tickets with filters, Customer Join, and Channel Join"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # [FIX 1] JOIN: Added ', chats(channel)' to get the source channel
        query = supabase.table("tickets") \
            .select("*, customers(id, name, email), chats(channel)", count="exact") \
            .eq("organization_id", organization_id)

        # Filters (Kept exactly as you had them)
        if status_filter: query = query.eq("status", status_filter.value)
        if priority: query = query.eq("priority", priority.value)
        if category: query = query.eq("category", category)
        if assigned_to: query = query.eq("assigned_agent_id", assigned_to)
        if chat_id: query = query.eq("chat_id", chat_id)
        if customer_id: query = query.eq("customer_id", customer_id)
        if updated_after: query = query.gte("updated_at", updated_after.isoformat())
        if created_after: query = query.gte("created_at", created_after.isoformat())

        # Sort & Paginate
        valid_sort_fields = ["created_at", "updated_at", "priority", "ticket_number", "status"]
        if sort_by not in valid_sort_fields: sort_by = "updated_at"
        is_descending = sort_order.lower() == "desc"
        query = query.order(sort_by, desc=is_descending).range(skip, skip + limit - 1)

        response = query.execute()

        # [FIX 2] Map data (Customer Name + Channel)
        tickets_with_customer = []
        for ticket_data in response.data:
            # 1. Handle Customer
            customer_obj = ticket_data.get("customers")
            if customer_obj:
                ticket_data["customer_name"] = customer_obj.get("name")
                ticket_data["customer"] = customer_obj 
            else:
                ticket_data["customer_name"] = "Unknown Customer"

            # 2. Handle Channel (Flatten it)
            chat_obj = ticket_data.get("chats")
            if chat_obj:
                # Extract 'channel' from nested object to root 'channel' field
                ticket_data["channel"] = chat_obj.get("channel")
            
            # Remove nested objects to keep response clean (optional, but good for Pydantic)
            ticket_data.pop("chats", None)

            # [LOG] Debugging
            logger.info(f"🎫 Ticket {ticket_data.get('ticket_number')} -> Customer: {ticket_data.get('customer_name')} | Channel: {ticket_data.get('channel')}")

            tickets_with_customer.append(Ticket(**ticket_data))

        return TicketListResponse(
            tickets=tickets_with_customer,
            total=response.count if response.count else len(tickets_with_customer)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching tickets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch tickets"
        )
    
@router.get(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    summary="Get ticket by ID",
    description="Retrieve a specific ticket by ID"
)
async def get_ticket_by_id(
    ticket_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get specific ticket by ID"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        response = supabase.table("tickets").select("*").eq("id", ticket_id).eq("organization_id", organization_id).execute()

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ticket with ID {ticket_id} not found"
            )

        return Ticket(**response.data[0])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching ticket {ticket_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch ticket"
        )

@router.post(
    "/tickets",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    summary="Create new ticket",
    description="Create a new support ticket (Checks for active tickets first)"
)
async def create_ticket_client(
    data: TicketCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new ticket.
    Refactored to use TicketService for core logic.
    """
    try:
        # 0. Setup Dependencies
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # 1. CHECK FOR EXISTING ACTIVE TICKET (Anti-Duplicate)
        # We keep this check here to prevent conflicts before invoking the service
        if data.chat_id:
            existing_check = supabase.table("tickets") \
                .select("*") \
                .eq("organization_id", organization_id) \
                .eq("chat_id", data.chat_id) \
                .neq("status", "resolved") \
                .neq("status", "closed") \
                .execute()

            if existing_check.data:
                existing_ticket = existing_check.data[0]
                logger.info(f"♻️ Found existing active ticket {existing_ticket['ticket_number']} for Chat {data.chat_id}")
                
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An active ticket ({existing_ticket['ticket_number']}) already exists for this chat."
                )

        # 2. DELEGATE TO SERVICE
        # The service handles Numbering, Smart Titles, Insert, Logging, and Broadcasting
        ticket_service = get_ticket_service()
        
        new_ticket = await ticket_service.create_ticket(
            data=data,
            organization_id=organization_id,
            ticket_config=None,  # Service defaults to "TKT-" if None
            actor_id=current_user.user_id,
            actor_type=ActorType.HUMAN
        )

        return new_ticket

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating ticket: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create ticket: {str(e)}"
        )

@router.put(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    summary="Update ticket",
    description="Update an existing ticket (Auto-logs status/priority changes)"
)
async def update_ticket(
    ticket_id: str,
    ticket_update: TicketUpdate,
    current_user: User = Depends(get_current_user)
):
    """Update an existing ticket directly"""
    try:
        # 0. Setup Dependencies
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()
        
        # 1. Verify Ownership (Security)
        check = supabase.table("tickets").select("id").eq("id", ticket_id).eq("organization_id", organization_id).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # 2. Delegate to Service (Handles DB Update, Auto-Release, Logging)
        ticket_service = get_ticket_service()
        updated_ticket = await ticket_service.update_ticket(
            ticket_id=ticket_id, 
            update_data=ticket_update, 
            actor_id=current_user.user_id, 
            actor_type=ActorType.HUMAN
        )

        # 3. Broadcast Update
        try:
            conn = get_connection_manager()
            await conn.broadcast_chat_update(
                organization_id=organization_id,
                chat_id=updated_ticket.chat_id,
                update_type="ticket_updated",
                data={
                    "ticket_id": updated_ticket.id,
                    "ticket_number": updated_ticket.ticket_number,
                    "status": updated_ticket.status,
                    "priority": updated_ticket.priority
                }
            )
        except Exception: pass

        return updated_ticket

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating ticket {ticket_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update ticket: {str(e)}"
        )
        
@router.get(
    "/tickets/{ticket_id}/activities",
    response_model=List[TicketActivityResponse],
    summary="Get ticket timeline",
    description="Retrieve the full history of a ticket (created, updated, status changes)"
)
async def get_ticket_activities(
    ticket_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get ticket history"""
    try:
        # Check permission (User must belong to org)
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()
        
        # Verify ticket exists
        check = supabase.table("tickets").select("id").eq("id", ticket_id).eq("organization_id", organization_id).execute()
        if not check.data:
            raise HTTPException(404, "Ticket not found")

        # Fetch History
        log_service = get_ticket_service()
        return await log_service.get_ticket_history(ticket_id)

    except HTTPException: raise
    except Exception as e:
        logger.error(f"History error: {e}")
        raise HTTPException(500, "Failed to fetch ticket history")
    
# ============================================
# ANALYTICS ENDPOINTS
# ============================================

@router.get(
    "/analytics/dashboard",
    response_model=DashboardMetrics,
    summary="Get dashboard metrics",
    description="Retrieve CRM dashboard metrics and statistics"
)
async def get_dashboard_metrics(
    current_user: User = Depends(get_current_user)
):
    """Get dashboard metrics"""
    try:
        organization_id = await get_user_organization_id(current_user)
        supabase = get_supabase_client()

        # Get total chats
        total_chats_response = supabase.table("chats").select("id", count="exact").eq("organization_id", organization_id).execute()
        total_chats = total_chats_response.count if total_chats_response.count else 0

        # Get open chats
        open_chats_response = supabase.table("chats").select("id", count="exact").eq("organization_id", organization_id).eq("status", "open").execute()
        open_chats = open_chats_response.count if open_chats_response.count else 0

        # Get resolved today
        today = datetime.utcnow().date().isoformat()
        resolved_today_response = supabase.table("chats").select("id", count="exact").eq("organization_id", organization_id).eq("status", "resolved").gte("resolved_at", today).execute()
        resolved_today = resolved_today_response.count if resolved_today_response.count else 0

        # Get active agents
        active_agents_response = supabase.table("agents").select("id", count="exact").eq("organization_id", organization_id).eq("status", "active").execute()
        active_agents = active_agents_response.count if active_agents_response.count else 0

        # Get tickets by status
        tickets_response = supabase.table("tickets").select("status").eq("organization_id", organization_id).execute()

        tickets_by_status = {
            "open": 0,
            "in_progress": 0,
            "resolved": 0,
            "closed": 0
        }

        for ticket in tickets_response.data:
            status = ticket.get("status", "open")
            if status in tickets_by_status:
                tickets_by_status[status] += 1

        # Calculate average response time (simplified)
        avg_response_time = "2.5 min"  # TODO: Calculate from actual data

        return DashboardMetrics(
            total_chats=total_chats,
            open_chats=open_chats,
            resolved_today=resolved_today,
            avg_response_time=avg_response_time,
            active_agents=active_agents,
            tickets_by_status=tickets_by_status
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching dashboard metrics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch dashboard metrics"
        )
