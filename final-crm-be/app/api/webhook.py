"""
Webhook API Endpoints
Receive incoming messages from external services (WhatsApp, Telegram, Email)
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import JSONResponse
import logging
import asyncio
import base64
import json 
import uuid
import time
import re

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Tuple

from app.models.webhook import (
    WhatsAppWebhookMessage,
    WhatsAppUnofficialWebhookMessage,
    TelegramWebhookMessage,
    EmailWebhookMessage,
    WebhookRouteResponse,
    WebhookErrorResponse,
    WhatsAppEventPayload
)
from app.middleware.webhook_auth import get_webhook_secret
from app.services.message_router_service import get_message_router_service
from app.services.agent_finder_service import get_agent_finder_service
from app.services.websocket_service import get_connection_manager

from app.config import settings as app_settings
from app.models.ticket import TicketCreate, ActorType, TicketPriority, AITicketUpdatePayload, TicketUpdate
from app.services.ticket_service import get_ticket_service
from app.api.crm_chats import send_message_via_channel
from app.utils.schedule_validator import get_agent_schedule_config, is_within_schedule
from app.services.llm_queue_service import get_llm_queue
from app.services.redis_service import acquire_lock, get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

# ============================================
# HELPER FUNCTIONS
# ============================================

def _generate_status_message(result: dict) -> str:
    if result.get("status") == "dropped_inactive": return "Message dropped (Agent Inactive)"
    if result.get("handled_by") == "system_busy": return "Auto-reply sent (Agent Busy)"
    return "Message processed"

def parse_agent_config(config_data: Any) -> Dict:
    if not config_data: return {}
    if isinstance(config_data, dict): return config_data
    if isinstance(config_data, str):
        try: return json.loads(config_data)
        except: return {}
    return {}

async def fetch_agent_settings(supabase, agent_id: str) -> Dict[str, Any]:
    try:
        res = supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()
        if res.data:
            data = res.data[0]
            data.pop("id", None) 
            data.pop("created_at", None)
            data.pop("updated_at", None)
            return data
    except Exception: pass
    return {}

async def send_auto_reply(supabase, channel: str, agent_id: str, contact_info: Dict, message_text: str):
    """Sends reply via channel (WhatsApp/Telegram)."""
    try:
        if channel == "whatsapp":
            from app.services.whatsapp_service import get_whatsapp_service
            await get_whatsapp_service().send_text_message(agent_id, contact_info.get("phone"), message_text)
        elif channel == "telegram":
            chat_data = {"id": "temp_auto_reply", "channel": "telegram", "sender_agent_id": agent_id}
            customer_data = {"telegram_id": contact_info.get("telegram_id")}
            await send_message_via_channel(chat_data, customer_data, message_text, supabase)
    except Exception as e:
        logger.error(f"❌ Auto-reply failed: {e}")

async def save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, content, channel, metadata=None, agent_name=None): # <--- Added agent_name
    try:
        msg_data = {
            "chat_id": chat_id,
            "sender_type": "ai",
            "sender_id": agent_id,
            "content": content,
            "metadata": metadata or {"type": "auto_reply"}
        }
        res = supabase.table("messages").insert(msg_data).execute()
        
        if res.data and app_settings.WEBSOCKET_ENABLED:
            new_msg = res.data[0]
            await get_connection_manager().broadcast_new_message(
                organization_id=org_id, chat_id=chat_id, message_id=new_msg["id"],
                customer_id=None, customer_name=agent_name or "AI Agent", # Fallback
                message_content=content, channel=channel, handled_by="ai",
                sender_type="ai", sender_id=agent_id,
                sender_name=agent_name # <--- [FIX] Pass the real name here
            )
    except Exception as e:
        logger.error(f"Failed to save/broadcast system message: {e}")

async def resolve_lid_to_real_number(
    contact: str, 
    agent_id: str, 
    channel: str,
    supabase
) -> str:
    """
    Resolve LID to real phone number if needed.
    
    Args:
        contact: Contact identifier (can be LID or regular phone)
        agent_id: Agent ID for WhatsApp service lookup
        channel: Communication channel (must be 'whatsapp' for LID resolution)
        supabase: Supabase client for caching resolved numbers
    
    Returns:
        Resolved phone number (with @c.us suffix) or original contact if resolution fails
    """
    # Only resolve for WhatsApp LIDs
    if channel != "whatsapp" or "@lid" not in str(contact):
        return contact
    try:        
        from app.services.whatsapp_service import get_whatsapp_service
        wa_svc = get_whatsapp_service()
        
        # Attempt to resolve LID
        lookup = await wa_svc.get_contact_by_id(agent_id, contact)
        
        if lookup.get("success") and lookup.get("number"):
            resolved = lookup["number"]
            return resolved
        else:
            logger.warning(f"⚠️ Could not resolve LID {contact}: {lookup.get('message', 'Unknown error')}")
            logger.warning(f"⚠️ Will attempt to send to original LID address (may fail)")
            return contact
            
    except Exception as e:
        logger.error(f"❌ LID resolution error: {e}")
        return contact
    
async def _handle_message_content(message_data: dict, data_type: str) -> Tuple[str, str, Optional[str]]:
    """
    DRY Helper: Analyzes message body/media, uploads content if needed.
    [FIX] Aggressively sanitizes Base64 from captions and auto-detects image types.
    """
    content = ""
    msg_type = "text"
    media_url = None
    
    if data_type == "media":
        # Handle Media
        media_obj = message_data.get("messageMedia", {}) or message_data
        base64_data = media_obj.get("data") or media_obj.get("body", "")
        
        if not base64_data: 
            # Last resort: check if 'body' in the root message_data has the base64
            base64_data = message_data.get("body", "")

        if not base64_data: 
            raise ValueError("Media data missing")
        
        mime = media_obj.get("mimetype", "application/octet-stream")
        raw_type = media_obj.get("type", "file")
        
        # [FIX] Smart Type Detection (Check headers if MIME is generic)
        if "image" in mime:
            msg_type = "image"
        elif base64_data.startswith("/9j/") or base64_data.startswith("iVBORw0KGgo"):
            msg_type = "image" # Force image for JPEG/PNG signatures
            if "octet" in mime: mime = "image/jpeg" # Correct MIME
        elif "video" in mime:
            msg_type = "video"
        elif "audio" in mime:
            msg_type = "audio"
        elif "pdf" in mime:
            msg_type = "document"
        else:
            msg_type = "file"
            
        # [FIX] Improved Extension Logic
        ext = "bin"
        if "/" in mime and "octet" not in mime:
            ext = mime.split("/")[-1].replace("jpeg", "jpg")
        else:
            # Fallback extension based on type
            if msg_type == "image": ext = "jpg"
            elif msg_type == "audio": ext = "mp3"
            elif msg_type == "video": ext = "mp4"
        
        media_url = await _upload_media_to_supabase(base64_data, mime, ext)
        
        # [FIX] Extract Caption & Sanitize Base64 bleeding
        # 1. Try explicit caption
        content = media_obj.get("caption", "")
        
        # 2. Fallback to body (common in WA), BUT check if it's Base64 first
        if not content:
            msg_structure = message_data.get("message", {})
            potential_content = msg_structure.get("body", "") or msg_structure.get("_data", {}).get("body", "")
            
            # Only use if it does NOT look like Base64
            if potential_content and len(str(potential_content)) < 500:
                content = potential_content
            elif potential_content and " " in str(potential_content)[:50]:
                content = potential_content

        # 3. Final Sanity Check: If content is actually the Base64 string, kill it.
        if content and (str(content).startswith("/9j/") or len(str(content)) > 1000):
            logger.warning("🧹 Sanitized Base64 string from caption field.")
            content = ""

    else:
        # Handle Text
        body = message_data.get("body") or message_data.get("_data", {}).get("body", "")
        
        # Check for Base64 sneaking in as text (No-Caption Image)
        if body and isinstance(body, str) and (body.startswith("/9j/") or (len(body) > 500 and " " not in body[:50])):
            try:
                # Treat as Image upload
                logger.info("🕵️ Detected Base64 image sent as text. Converting...")
                media_url = await _upload_media_to_supabase(body, "image/jpeg", "jpg")
                content = "" # Clear text content
                msg_type = "image"
            except Exception as e:
                logger.error(f"❌ Failed to process Base64 text: {e}")
                content = "[⚠️ Error: Image could not be processed]" 
                msg_type = "text"
        else:
            content = body
            
    return content, msg_type, media_url

async def is_duplicate_message(dedup_key: str, ttl: int = 300) -> bool:
    """
    Checks Redis to see if message ID was processed recently.
    Atomic: Works across multiple containers.
    Returns: True if duplicate, False if new.
    """
    try:
        client = get_redis()
        # SET key "1" EX 300 NX 
        # NX = Only set if Not Exists.
        # If Redis returns True, we successfully set it (It's NEW).
        # If Redis returns None, it already exists (It's a DUPLICATE).
        was_set = await client.set(f"dedup:{dedup_key}", "1", ex=ttl, nx=True)
        return not was_set
    except Exception as e:
        # Fail open: If Redis dies, process the message anyway rather than dropping it.
        logger.error(f"⚠️ Redis Dedup Error: {e}")
        return False

# ============================================
# MAIN PROCESSOR (FIXED)
# ============================================

def get_supabase_client():
    from supabase import create_client
    if not app_settings.is_supabase_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase is not configured")
    return create_client(app_settings.SUPABASE_URL, app_settings.SUPABASE_SERVICE_KEY)

# ============================================
# EMAIL WEBHOOK
# ============================================

@router.post(
    "/email",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive Email message",
    description="Webhook endpoint to receive incoming emails",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "No agent integration found"},
        500: {"description": "Internal server error"}
    }
)
async def email_webhook(
    message: EmailWebhookMessage,
    secret: str = Depends(get_webhook_secret)
):
    """
    Receive incoming email and route to correct chat.

    **Authentication:** Requires `X-API-Key` header with valid secret key.

    **Request Example:**
    ```json
    {
        "email": "customer@example.com",
        "to_email": "support@example.com",
        "sender_name": "Jane Smith",
        "subject": "Question about product",
        "message": "Hello, I have a question...",
        "message_id": "email-msg-123",
        "timestamp": "2025-10-21T15:30:00Z"
    }
    ```

    Args:
        message: Email webhook message
        secret: Validated webhook secret (injected by dependency)

    Returns:
        WebhookRouteResponse with routing details

    Raises:
        HTTPException: If routing fails or no agent integration found
    """
    try:
        logger.info(
            f"📧 Email webhook received: "
            f"from={message.email}, to={message.to_email}"
        )

        # Get Supabase client
        supabase = get_supabase_client()

        # STEP 1: Find agent by Email integration
        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_email(
            email=message.to_email
        )

        if not agent:
            logger.error(f"❌ No agent integration found for Email: {message.to_email}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent integration found for Email {message.to_email}"
            )

        organization_id = agent["organization_id"]
        logger.info(
            f"✅ Agent found: {agent['name']} (org={organization_id}, "
            f"is_ai={agent['user_id'] is None}, status={agent.get('status')})"
        )

        # Prepare message metadata
        message_metadata = {
            "email_message_id": message.message_id,
            "email_subject": message.subject,
            "attachments": message.attachments,
            "timestamp": message.timestamp,
            **message.metadata
        }

        # Prepare customer metadata
        customer_metadata = {
            "email_display_name": message.sender_name
        }

        # STEP 2: Process webhook message (unified logic)
        result = await process_webhook_message_v2(
            agent=agent,
            channel="email",
            contact=message.email,
            message_content=message.message,
            customer_name=message.sender_name,
            message_metadata=message_metadata,
            customer_metadata=customer_metadata,
            supabase=supabase
        )

        # Prepare response
        response = WebhookRouteResponse(
            success=True,
            chat_id=result["chat_id"],
            message_id=result["message_id"],
            customer_id=result["customer_id"],
            is_new_chat=result["is_new_chat"],
            was_reopened=result["was_reopened"],
            handled_by=result["handled_by"],
            status=result["status"],
            channel=result["channel"],
            message=_generate_status_message(result)
        )

        logger.info(
            f"✅ Email routed: "
            f"chat={result['chat_id']}, is_new={result['is_new_chat']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing Email webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process Email: {str(e)}"
        )

# ============================================
# WA-UNOFFICIAL HELPER FUNCTIONS
# ============================================

async def _upload_media_to_supabase(
    media_data: str,
    mime_type: str,
    file_extension: str
) -> str:
    """
    Upload media to 'tmp' bucket.
    Storage Policy: Files are temporary.
    Link Expiry: 3 Days (259200 seconds).
    """
    try:
        # Decode
        file_content = base64.b64decode(media_data)
        file_id = str(uuid.uuid4())
        filename = f"{file_id}.{file_extension}"
        
        # TARGET: 'tmp' bucket
        bucket_name = "tmp" 
        supabase = get_supabase_client()
        
        logger.info(f"   📤 [UPLOAD] Uploading to {bucket_name}/{filename} (Size: {len(media_data)})")

        # --- ATTEMPT 1: Optimistic Upload ---
        try:
            supabase.storage.from_(bucket_name).upload(
                path=filename,
                file=file_content,
                file_options={"content-type": mime_type, "upsert": "false"}
            )
        except Exception as e:
            # --- ATTEMPT 2: Self-Healing (Auto-Create Bucket) ---
            logger.warning(f"   ⚠️ Upload failed: {e}. Checking bucket...")
            try:
                buckets = supabase.storage.list_buckets()
                if not any(b.name == bucket_name for b in buckets):
                    logger.info(f"   🛠️ Creating bucket '{bucket_name}'...")
                    supabase.storage.create_bucket(bucket_name, options={"public": False})
                
                supabase.storage.from_(bucket_name).upload(
                    path=filename,
                    file=file_content,
                    file_options={"content-type": mime_type, "upsert": "false"}
                )
            except Exception as retry_e:
                logger.error(f"   ❌ FATAL: Retry failed: {retry_e}")
                raise retry_e

        # [FIX] Set Expiry to 3 Days (259200 seconds)
        url_response = supabase.storage.from_(bucket_name).create_signed_url(filename, 259200)
        
        if isinstance(url_response, dict):
             public_url = url_response.get("signedURL") or url_response.get("signedUrl")
        else:
             public_url = url_response

        if not public_url:
            raise Exception("Generated URL is empty")

        logger.info(f"   🔗 [UPLOAD] URL: {public_url[:50]}...")
        return public_url

    except Exception as e:
        logger.error(f"❌ [UPLOAD FAILED] Critical: {e}", exc_info=True)
        # Return 500 to Worker so we know it failed
        raise HTTPException(status_code=500, detail=f"Media Upload Error: {str(e)}")
    
def _extract_phone_number(whatsapp_id: str) -> str:
    if not whatsapp_id: return ""
    if "@lid" in whatsapp_id: return whatsapp_id
    if "@g.us" in whatsapp_id: return whatsapp_id # Keep Group IDs intact
    
    clean_id = whatsapp_id.split("@")[0].split(":")[0]
    return clean_id

async def _convert_unofficial_to_standard(unofficial: WhatsAppUnofficialWebhookMessage) -> WhatsAppWebhookMessage:
    """
    Unified converter: Extracts Real Sender Number & Enforces 'Group Name' format.
    """
    raw_payload = unofficial.data
    
    # 1. EXTRACT IDENTITY
    wrapper = raw_payload.get("message", {}) or raw_payload.get("messageMedia", {})
    identity_source = wrapper.get("_data", {}) or wrapper
    
    # Identify Context
    chat_id = identity_source.get("id", {}).get("remote") or identity_source.get("from", "")
    sender_id = identity_source.get("author") or identity_source.get("participant") or chat_id

    # Clean IDs
    phone = _extract_phone_number(chat_id)          # Group ID
    sender_clean = _extract_phone_number(sender_id) # Participant ID (LID or Phone)
    to_num = _extract_phone_number(identity_source.get("to", ""))

    # [FIX] Get Real Phone Number (Strip Suffix)
    real_number = sender_clean.split('@')[0]

    # [FIX] Detect Sender Name (Prioritize Real Name)
    real_sender_name = (
        identity_source.get("notifyName") or 
        identity_source.get("pushname") or 
        identity_source.get("verifiedName")
    )
    
    # Fallback: If Name is missing/empty, use the Number
    if not real_sender_name or str(real_sender_name).strip() == "":
        real_sender_name = real_number
        
    # [FIX] Logic for "Customer Name"
    # For Groups, the "Customer" is the Group itself. 
    if "@g.us" in chat_id:
        # 1. Get Group Name
        group_subject = (
            identity_source.get("chat", {}).get("name") or 
            identity_source.get("groupInfo", {}).get("subject") or
            wrapper.get("chat", {}).get("name")
        )
        
        # [FIX] Clean "Group g.us" - Use Sender Name if Subject is Bad
        if not group_subject or "@g.us" in str(group_subject):
            group_subject = f"{real_sender_name}"
            
        final_customer_name = group_subject
    else:
        # For DMs, "Customer Name" is the Sender
        final_customer_name = real_sender_name
    
    msg_id = identity_source.get("id", {}).get("id") if isinstance(identity_source.get("id"), dict) else None
    ts = datetime.fromtimestamp(identity_source.get("t")).isoformat() if identity_source.get("t") else None
    
    # 2. EXTRACT CONTENT
    if unofficial.dataType == "media":
        content_source = raw_payload
    else:
        content_source = identity_source

    try:
        text, type_str, url = await _handle_message_content(content_source, unofficial.dataType)
    except Exception as e:
        logger.error(f"Content parse error: {e}")
        text = "" 
        type_str = "text"
        url = None

    return WhatsAppWebhookMessage(
        phone_number=phone,
        to_number=to_num,
        sender_name=final_customer_name,
        message=text, 
        message_id=msg_id, 
        message_type=type_str,
        media_url=url, 
        timestamp=ts,
        metadata={
            "session_id": unofficial.sessionId,
            "is_group": "@g.us" in chat_id,
            "group_participant": sender_clean if "@g.us" in chat_id else None,
            "real_contact_number": real_number,    # Capture Real Number (Raw LID for now)
            "real_sender_name": real_sender_name,  # Capture Specific Sender Name
            "notifyName": real_sender_name,
            "pushName": real_sender_name,
            "original_sender_id": sender_id,
            "whatsapp_name": real_sender_name
        }
    )

async def process_webhook_message_v2(
    agent: Dict, channel: str, contact: str, message_content: str,
    customer_name: Optional[str], message_metadata: Dict, customer_metadata: Dict, supabase
) -> Dict:
    """
    V2 Processor: Route -> Broadcast -> Busy/Schedule -> Ticket/AI
    STATUS: STABLE (Includes 3s Poll for Media Consistency)
    """
    agent_id = agent["id"]
    org_id = agent["organization_id"]
    status = agent.get("status", "active")

    # [FIX] Handle BOTH Inactive and Archived
    # Return early with a specific status flag
    if status in ["inactive", "archived"]:
        logger.info(f"🛑 Agent {agent_id} is {status}. Dropping message.")
        return {"success": False, "status": f"dropped_{status}"}

    # 1. ROUTING
    router = get_message_router_service(supabase)
    res = await router.route_incoming_message(
        agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata
    )
    chat_id, cust_id, msg_id = res["chat_id"], res["customer_id"], res["message_id"]

    # 2. UPDATE CUSTOMER DATA (Phone & Metadata)
    if cust_id and customer_metadata:
        try:
            cust_res = supabase.table("customers").select("metadata").eq("id", cust_id).single().execute()
            current = {}
            if cust_res.data:
                raw = cust_res.data.get("metadata")
                if raw:
                    current = json.loads(raw) if isinstance(raw, str) else raw
            
            merged = {**current, **customer_metadata}
            supabase.table("customers").update({"metadata": merged}).eq("id", cust_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ Failed to update customer metadata: {e}")

    # 3. WEBSOCKET BROADCAST (WITH STABILITY LOOP)
    if app_settings.WEBSOCKET_ENABLED:
        try:
            fresh_metadata = message_metadata
            
            # [RESTORED] STABILITY LOOP
            # If it's a media message, wait briefly and check DB to ensure we have the Final URL.
            # This fixes "Blurry Images" caused by the Frontend getting the event before the DB is consistent.
            if channel == "whatsapp" and message_metadata.get("message_type") in ["image", "video", "document"]:
                initial_url = message_metadata.get("media_url")
                
                # Try 3 times (1s interval)
                for i in range(3):
                    await asyncio.sleep(1) # Wait for DB consistency
                    try:
                        msg_res = supabase.table("messages").select("metadata").eq("id", msg_id).single().execute()
                        if msg_res.data and msg_res.data.get("metadata"):
                            db_meta = msg_res.data["metadata"]
                            # If DB has a DIFFERENT URL (e.g. Supabase Storage link instead of None/Blurhash), use it!
                            if db_meta.get("media_url") and db_meta.get("media_url") != initial_url:
                                fresh_metadata = db_meta
                                logger.info(f"📸 Found updated media URL for {msg_id} after {i+1}s wait.")
                                break
                    except Exception: pass
            
            attachment_data = None
            if fresh_metadata.get("media_url"):
                attachment_data = {
                    "url": fresh_metadata["media_url"], 
                    "type": fresh_metadata.get("message_type", "image"),
                    "name": "Media Attachment"
                }

            sender_display = customer_name

            await get_connection_manager().broadcast_new_message(
                organization_id=org_id, chat_id=chat_id, message_id=msg_id,
                customer_id=cust_id, customer_name=sender_display or "Unknown",
                message_content=message_content, channel=channel, handled_by=res["handled_by"],
                sender_type="customer", sender_id=cust_id, is_new_chat=res["is_new_chat"],
                was_reopened=res.get("was_reopened", False), 
                metadata=fresh_metadata,
                attachment=attachment_data
            )
        except Exception as e:
            logger.error(f"❌ WS Broadcast Failed: {e}")

    if res.get("is_merged_event"): return res
    if res.get("handled_by") == "human": return res

    # 5. BUSY CHECK
    if agent.get("status") == "busy":
        msg = "Maaf, saat ini kami sedang sibuk."
        await send_auto_reply(supabase, channel, agent_id, {"phone": contact}, msg)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "system_busy"}

    # 6. SCHEDULE CHECK
    schedule = await get_agent_schedule_config(agent_id, supabase)
    is_within, _ = is_within_schedule(schedule, datetime.now(ZoneInfo("UTC")))
    if not is_within:
        msg = "Maaf kami sedang tutup saat ini."
        try: supabase.table("messages").update({"metadata": {**message_metadata, "out_of_schedule": True}}).eq("id", msg_id).execute()
        except: pass
        await send_message_via_channel({"id": chat_id, "channel": channel, "sender_agent_id": agent_id}, {"phone": contact}, msg, supabase)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "ooo_system"}

    # 7. TICKET & AI TRIGGER
    queue_svc = get_llm_queue()
    ticket_svc = get_ticket_service()
    
    # Defaults Ticket 
    ai_priority = "low"
    ticket_ready = False
    target_ticket_id = None  

    # Note: We keep the lock here to prevent two parallel webhooks from creating 
    # two duplicates tickets for the same chat.
    async with acquire_lock(f"webhook:ticket:{chat_id}", expire=30, wait_time=5) as acquired:
        if acquired:
            try:
                active_ticket = None
                ticket_query = supabase.table("tickets").select("id, assigned_agent_id, priority")\
                    .eq("customer_id", cust_id).in_("status", ["open", "in_progress"]).limit(1).execute()
                
                if ticket_query.data:
                    # Case A: Existing Ticket Found
                    active_ticket = ticket_query.data[0]
                    if active_ticket.get("assigned_agent_id"): 
                        return {**res, "handled_by": "human_ticket"}
                    
                    ai_priority = str(active_ticket.get("priority")).lower() # its not only medium so lets data decide
                    target_ticket_id = active_ticket.get("id")
                    ticket_ready = True
                else:
                    # Case B: Create New Ticket
                    ticket_config = parse_agent_config(agent.get("ticketing_config"))
                    new_ticket_data = TicketCreate(
                        chat_id=chat_id, customer_id=cust_id,
                        title=f"[LOW] New Interaction - {message_content[:40]}",
                        description=f"First Message: {message_content}\n\n[Auto-created: Low Priority Default]",
                        priority=TicketPriority.LOW, category="General"
                    )
                    # Create ticket returns the full Ticket object
                    new_ticket = await ticket_svc.create_ticket(new_ticket_data, org_id, ticket_config, None, ActorType.SYSTEM)
                    
                    target_ticket_id = new_ticket.id 
                    ai_priority = "low"
                    ticket_ready = True
            except Exception as e:
                logger.error(f"❌ Ticket Creation Flow Error: {e}")
        else:
            logger.warning(f"⏳ Could not acquire ticket lock for {chat_id}. Skipping ticket creation check.")

    # [FIX] Only enqueue if we are SURE the ticket exists
    if ticket_ready and target_ticket_id:
        await queue_svc.enqueue(
            chat_id=chat_id, 
            message_id=msg_id, 
            supabase_client=supabase, 
            priority=ai_priority,
            ticket_id=target_ticket_id 
        )
        return {**res, "handled_by": "ai_v2_queued"}
    else:
        logger.error(f"🛑 Skipping AI for chat {chat_id}: Ticket context missing or lock failed.")
        return {**res, "handled_by": "error_no_ticket"}
    
# ============================================
# 2. WHATSAPP UNOFFICIAL WEBHOOK (Updated)
# ============================================

@router.post("/wa-unofficial", response_model=WebhookRouteResponse)
async def whatsapp_unofficial_webhook(message: WhatsAppUnofficialWebhookMessage, secret: str = Depends(get_webhook_secret)):
    # [FIX] Import re at the very top of function to prevent UnboundLocalError
    import re 
    
    try:
        supabase = get_supabase_client()
        agent_id = message.sessionId

        # 1. System Events
        system_events = ["qr", "authenticated", "ready", "disconnected", "loading_screen", "message_ack", "message_revoke", "status_find_partner"]
        if message.dataType in system_events:
            return JSONResponse(content={"success": True, "status": "processed_system_event"})

        # 2. Structure & Dedup
        data_wrapper = message.data.get("message", {}) or message.data.get("messageMedia", {})
        data_content = data_wrapper.get("_data", {}) or data_wrapper 
        whatsapp_id = data_content.get("id", {}).get("id") or message.data.get("id", {}).get("id")

        if not whatsapp_id and message.dataType not in ["ready", "authenticated"]:
             return JSONResponse(content={"status": "ignored", "reason": "malformed_structure"})
        
        if data_content.get("id", {}).get("fromMe", False) or data_content.get("fromMe", False):
             # [OPTIONAL] You can comment this return out if you rely on fromMe events to update message status/content
             # But usually for clean dashboard, we ignore fromMe or process it carefully.
             return JSONResponse(content={"status": "ignored", "reason": "from_me"})

        dedup_key = f"{whatsapp_id}_{message.dataType}"
        if await is_duplicate_message(dedup_key):
            return JSONResponse(content={"status": "ignored", "reason": "duplicate_redis_cache"})

        # 3. Agent Verify
        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        if not agent_res.data: return JSONResponse(status_code=200, content={"status": "error", "message": "Agent not found"})
        agent = agent_res.data[0]
        
        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        # =========================================================================
        # 3.5 PRE-FLIGHT CHECK (FIXED: LID RESOLUTION IN HISTORY)
        # =========================================================================
        raw_chat_id = data_content.get("id", {}).get("remote") or data_content.get("from", "")
        is_group_raw = "@g.us" in str(raw_chat_id)

        # Prepare identities (Me/Agent)
        potential_ids = set()
        if agent.get("phone"):
            intl_phone = str(agent.get("phone")).replace("+", "").replace("-", "").strip()
            potential_ids.add(intl_phone)
            if intl_phone.startswith("62"):
                potential_ids.add("0" + intl_phone[2:]) 
        
        potential_ids.add(_extract_phone_number(agent_id))
        
        raw_me = message.data.get("me", {})
        if raw_me.get("wid"): potential_ids.add(_extract_phone_number(raw_me["wid"]))
        if raw_me.get("lid"): potential_ids.add(_extract_phone_number(raw_me["lid"]))
        if raw_me.get("user"): potential_ids.add(_extract_phone_number(raw_me["user"]))
        
        agent_name = agent.get("name")
        pushname = raw_me.get("pushname")

        if is_group_raw:
            is_mentioned_raw = False
            
            # --- A. Explicit Mentions (Metadata) ---
            raw_mentions = data_content.get("mentionedJidList", [])
            for m in raw_mentions:
                if any(pid in str(m) for pid in potential_ids):
                    is_mentioned_raw = True
                    break
            
            # --- B. Reply Context Check ---
            quoted_participant = data_content.get("quotedParticipant") or \
                                 data_content.get("_data", {}).get("quotedParticipant")
            
            if not is_mentioned_raw and quoted_participant:
                q_clean = _extract_phone_number(quoted_participant)
                
                # Case 1: Direct Reply to Bot
                # A. Check if ID matches directly
                if any(pid in str(q_clean) for pid in potential_ids):
                    is_mentioned_raw = True
                    logger.info(f"✅ Pre-Flight: User replied to Agent ({q_clean})")
                
                # B. [FIX] Check if it's the Bot's LID (Resolve -> Match Phone)
                elif "@lid" in quoted_participant or len(q_clean) > 15:
                    try:
                        # If I reply to the bot, q_participant is the Bot's LID.
                        # We resolve it to see if the Real Number is ME.
                        candidate_lid = quoted_participant if "@" in quoted_participant else f"{quoted_participant}@lid"
                        resolved_bot = await resolve_lid_to_real_number(candidate_lid, agent_id, "whatsapp", supabase)
                        clean_bot = _extract_phone_number(resolved_bot)
                        
                        if any(pid in str(clean_bot) for pid in potential_ids):
                            is_mentioned_raw = True
                            logger.info(f"✅ Pre-Flight: Resolved Bot LID {q_clean} -> Me ({clean_bot})")
                    except Exception as e:
                        pass

                # Case 2: Reply to Self (Thread Continuity)
                elif not is_mentioned_raw:
                    sender_clean = _extract_phone_number(data_content.get("author") or data_content.get("participant") or "")
                    q_clean = _extract_phone_number(quoted_participant)

                    # [FIX] Resolve Sender LID if needed (to match Quoted Phone)
                    sender_real = sender_clean
                    if "@lid" in (data_content.get("author") or "") or len(sender_clean) > 15:
                        try:
                            # If sender is LID, resolve to Real Number (e.g. 628...)
                            sender_lid = sender_clean if "@" in sender_clean else f"{sender_clean}@lid"
                            resolved_sender = await resolve_lid_to_real_number(sender_lid, agent_id, "whatsapp", supabase)
                            sender_real = _extract_phone_number(resolved_sender)
                        except Exception: pass
                    
                    # [FIX] Resolve Quoted LID if needed
                    q_real = q_clean
                    if "@lid" in quoted_participant or len(q_clean) > 15:
                        try:
                            q_lid = quoted_participant if "@" in quoted_participant else f"{quoted_participant}@lid"
                            resolved_q = await resolve_lid_to_real_number(q_lid, agent_id, "whatsapp", supabase)
                            q_real = _extract_phone_number(resolved_q)
                        except Exception: pass

                    # Check if Sender matches Quoted (Reply to Self) - Compare REAL numbers
                    is_reply_to_self = sender_real and (sender_real in q_real or q_real in sender_real)
                    
                    if is_reply_to_self:
                        quoted_msg = data_content.get("quotedMsg") or data_content.get("_data", {}).get("quotedMsg", {})
                        quoted_body = quoted_msg.get("body", "") or quoted_msg.get("caption", "")
                        
                        if quoted_body:
                            # a. Check Name/Pushname
                            if (pushname and f"@{pushname}" in quoted_body) or (agent_name and f"@{agent_name}" in quoted_body):
                                is_mentioned_raw = True
                                logger.info("✅ Pre-Flight: 'Reply to Self' detected with Bot Name in history.")
                            
                            # b. Check IDs (Direct)
                            if not is_mentioned_raw:
                                for my_id in potential_ids:
                                    if f"@{my_id}" in quoted_body:
                                        is_mentioned_raw = True
                                        logger.info(f"✅ Pre-Flight: 'Reply to Self' detected with Bot ID ({my_id}) in history.")
                                        break
                            
                            # c. Check IDs (Deep Resolution for LIDs in History)
                            if not is_mentioned_raw:
                                history_mentions = re.findall(r"@(\d+)", quoted_body)
                                for hist_id in history_mentions:
                                    try:
                                        candidate_lid = f"{hist_id}@lid"
                                        resolved_hist = await resolve_lid_to_real_number(candidate_lid, agent_id, "whatsapp", supabase)
                                        clean_hist = _extract_phone_number(resolved_hist)
                                        
                                        if clean_hist in potential_ids:
                                            is_mentioned_raw = True
                                            logger.info(f"✅ Pre-Flight: 'Reply to Self' history mention @{hist_id} resolved to Me ({clean_hist})")
                                            break
                                    except: pass

            # --- C. Text Body Check (Fallback) ---
            if not is_mentioned_raw:
                raw_caption = data_content.get("caption", "")
                raw_body = data_content.get("body", "")
                
                raw_text = ""
                if raw_caption:
                    raw_text = raw_caption
                elif raw_body and len(str(raw_body)) < 1000:
                    raw_text = raw_body
                
                # Check IDs & Names
                for my_id in potential_ids:
                    if f"@{my_id}" in raw_text: is_mentioned_raw = True; break
                
                if not is_mentioned_raw:
                    if pushname and f"@{pushname}" in raw_text: is_mentioned_raw = True
                    elif agent_name and f"@{agent_name}" in raw_text: is_mentioned_raw = True

                # Check LIDs (Regex)
                if not is_mentioned_raw:
                    number_matches = re.findall(r"@\s?(\d{10,20})", raw_text)
                    for detected_num in number_matches:
                        try:
                            if detected_num in potential_ids:
                                is_mentioned_raw = True
                                break
                            
                            suffix = "@lid" if detected_num.startswith("2") else "@c.us"
                            resolved = await resolve_lid_to_real_number(f"{detected_num}{suffix}", agent_id, "whatsapp", supabase)
                            clean_resolved = _extract_phone_number(resolved)
                            
                            if clean_resolved in potential_ids:
                                is_mentioned_raw = True
                                logger.info(f"✅ Pre-Flight: Resolved {detected_num} -> Me")
                                break
                        except: pass

            if not is_mentioned_raw:
                return JSONResponse(content={"status": "ignored", "reason": "group_no_mention_preflight"})

        # =========================================================================
        # 4. Standardize
        # =========================================================================
        standard_message = await _convert_unofficial_to_standard(message)
        meta = standard_message.metadata
        is_group = meta.get("is_group", False)

        # Loop Protection
        if agent.get("phone"):
            agent_phone_clean = str(agent.get("phone")).replace("+", "").replace("-", "").strip()
            sender_num = standard_message.phone_number.split("@")[0]
            if sender_num == agent_phone_clean or ("0" + sender_num[2:]) == agent_phone_clean:
                return JSONResponse(content={"status": "ignored", "reason": "loop_protection"})

        # =========================================================================
        # 5. POST-STANDARD CLEANUP (CRITICAL FIX FOR DASHBOARD)
        # =========================================================================
        final_content = standard_message.message or ""
        
        # 1. Strip Agent/Push Names
        if pushname and f"@{pushname}" in final_content:
            final_content = final_content.replace(f"@{pushname}", "").strip()
        if agent_name and f"@{agent_name}" in final_content:
            final_content = final_content.replace(f"@{agent_name}", "").strip()

        # 2. Strip Own IDs (Group context)
        if is_group:
            for my_id in potential_ids:
                if f"@{my_id}" in final_content:
                    final_content = final_content.replace(f"@{my_id}", "").strip()

        # 3. [GLOBAL] Strip ALL Regex Matches (LIDs)
        # This removes "@2714..." from incoming echos/messages so Dashboard stays clean
        number_matches = re.findall(r"@\s?(\d{10,20})", final_content)
        for detected_num in number_matches:
             final_content = re.sub(r"@\s?" + detected_num, "", final_content).strip()
        
        standard_message.message = final_content
        logger.info(f"🧹 Message content cleaned: '{final_content}'")

        # 6. Zombie Check
        if standard_message.timestamp:
            try:
                msg_time = datetime.fromisoformat(standard_message.timestamp)
                if msg_time.tzinfo is None: msg_time = msg_time.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - msg_time).total_seconds() > 300:
                    return JSONResponse(content={"status": "ignored", "reason": "too_old"})
            except Exception: pass

        # 7. Sender Resolution
        contact_id = standard_message.phone_number
        final_participant_number = meta.get("real_contact_number")

        if not is_group:
            contact_id = await resolve_lid_to_real_number(standard_message.phone_number, agent_id, "whatsapp", supabase)
            final_participant_number = contact_id
        else:
            raw_participant = meta.get("group_participant", "")
            is_potential_lid = raw_participant and (str(raw_participant).isdigit() and len(str(raw_participant)) >= 10)
            
            if is_potential_lid or "@lid" in str(raw_participant):
                try:
                    lid_to_resolve = raw_participant if "@" in str(raw_participant) else f"{raw_participant}@lid"
                    real_phone = await resolve_lid_to_real_number(lid_to_resolve, agent_id, "whatsapp", supabase)
                    if real_phone and "@" in real_phone and "lid" not in real_phone:
                        final_participant_number = real_phone.split("@")[0]
                except Exception: pass

        if "@c.us" in contact_id: contact_id = contact_id.split("@")[0]
        sender_name = standard_message.sender_name or contact_id

        # 8. Process Message
        clean_metadata = standard_message.metadata.copy()
        if "real_contact_number" in clean_metadata:
            del clean_metadata["real_contact_number"]

        result = await process_webhook_message_v2(
            agent=agent, 
            channel="whatsapp", 
            contact=contact_id, 
            message_content=standard_message.message, 
            customer_name=sender_name,
            message_metadata={
                **clean_metadata,
                "whatsapp_message_id": standard_message.message_id,
                "message_type": standard_message.message_type,
                "timestamp": standard_message.timestamp,
                "is_lid": "@lid" in standard_message.phone_number,
                "media_url": standard_message.media_url,
                "is_group": is_group,
                "participant": meta.get("group_participant"), 
                "sender_display_name": meta.get("real_sender_name"),
                "real_contact_number": final_participant_number,
            },
            customer_metadata={
                "whatsapp_name": meta.get("real_sender_name"),
                "real_number": final_participant_number,
                **({"is_group": True} if is_group else {})
            },
            supabase=supabase
        )

        return WebhookRouteResponse(
            success=True, chat_id=result["chat_id"], message_id=result["message_id"],
            customer_id=result["customer_id"], is_new_chat=result["is_new_chat"],
            was_reopened=result.get("was_reopened", False), handled_by=result["handled_by"],
            status=result["status"], channel="whatsapp", message=_generate_status_message(result)
        )

    except Exception as e:
        logger.error(f"❌ Unofficial Webhook Critical Error: {e}")
        return JSONResponse(status_code=200, content={"success": False, "error": str(e)})


# ============================================
# 3. TELEGRAM USERBOT WEBHOOK (Updated)
# ============================================

async def _handle_message_content_for_telegram(message_data: dict, data_type: str) -> Tuple[str, str, Optional[str]]:
    """Safe Content Handler for Telegram (Fixed for Userbot Media)"""
    content = ""
    msg_type = "text"
    media_url = None
    
    if data_type == "media":
        # 1. Unwrap messageMedia
        media_obj = message_data.get("messageMedia", {}) or message_data
        
        # 2. Get Base64: Check 'data', 'body', AND '_data.body' (The Fix)
        base64_data = media_obj.get("data") or media_obj.get("body")
        if not base64_data:
            base64_data = media_obj.get("_data", {}).get("body") # <--- Found it here

        if not base64_data: 
            # Check for thumbnail as last resort
            base64_data = media_obj.get("thumbnail") or media_obj.get("_data", {}).get("thumbnail")

        if not base64_data: 
            return "[Image/Media - Download Failed]", "text", None

        # [FIX] Sanitize Bad Base64 (The "Incorrect Padding" Fix)
        if "," in base64_data: base64_data = base64_data.split(",")[1]
        base64_data = base64_data.strip().replace("\n", "").replace("\r", "").replace(" ", "")

        # 3. Metadata
        mime = media_obj.get("mimetype", "application/octet-stream")
        content = media_obj.get("caption", "")

        if "image" in mime: msg_type = "image"
        elif "video" in mime: msg_type = "video"
        elif "audio" in mime: msg_type = "audio"
        elif "pdf" in mime: msg_type = "document"
        else: msg_type = "file"

        ext = "bin"
        if "/" in mime: ext = mime.split("/")[-1].replace("jpeg", "jpg")
        
        # 4. Upload using YOUR helper (No new imports)
        media_url = await _upload_media_to_supabase(base64_data, mime, ext)
    else:
        # Text
        data_wrapper = message_data.get("message", {}) or {}
        data_content = data_wrapper.get("_data", {}) or data_wrapper
        content = data_content.get("body", "")
        msg_type = "text"

    return content, msg_type, media_url

@router.post("/telegram-userbot", response_model=WebhookRouteResponse)
async def telegram_userbot_webhook(
    payload: WhatsAppUnofficialWebhookMessage, 
    secret: str = Depends(get_webhook_secret)
):
    try:
        agent_id = payload.sessionId
        
        # [DEBUG LOG] See exactly who is calling
        logger.info(f"⚡ Telegram Webhook Triggered. Session ID: {agent_id}")

        # 1. Agent Verification (The source of your 404)
        supabase = get_supabase_client()
        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        
        if not agent_res.data:
            error_msg = f"❌ Agent ID '{agent_id}' not found in DB. Please register it."
            logger.error(error_msg)
            # Return 404 but with a helpful message
            raise HTTPException(status_code=404, detail=error_msg)
            
        agent = agent_res.data[0]
        
        # Load settings
        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        # 2. Content Extraction
        raw_data = payload.data
        data_content = {}
        search_candidates = [
            raw_data, raw_data.get("message", {}), raw_data.get("_data", {}), raw_data.get("message", {}).get("_data", {})
        ]
        for candidate in search_candidates:
            if isinstance(candidate, dict) and (candidate.get("from") or candidate.get("to")):
                data_content = candidate
                break
        if not data_content: data_content = raw_data

        # 3. Identity & Group Logic
        sender_id = str(data_content.get("from", ""))
        sender_name = data_content.get("notifyName") or f"User {sender_id}"
        
        is_group_tele = data_content.get("is_group", False)
        contact_target = sender_id 
        
        if is_group_tele:
            # Strict Mention Check
            if not data_content.get("mentioned", False):
                return JSONResponse(content={"status": "ignored_no_mention"})
            
            contact_target = str(data_content.get("to", "")) 
            logger.info(f"✅ Group Mention: {contact_target} from {sender_id}")

        if not contact_target or contact_target == "None":
            contact_target = data_content.get("id", {}).get("remote")
            if not contact_target:
                return JSONResponse(content={"status": "ignored_missing_id"})

        # 4. Payload Prep & Media Extraction
        msg_id = data_content.get("id", {}).get("id") or str(uuid.uuid4())
        timestamp_unix = data_content.get("t")
        
        # Use the ROBUST handler
        text, type_str, url = await _handle_message_content_for_telegram(
            raw_data, 
            payload.dataType
        )
        
        if text: text = re.sub(r"\[.*?\]\(tg://user\?id=\d+\)\s*", "", text).strip()

        msg_meta = {
            "telegram_message_id": msg_id,
            "telegram_sender_id": sender_id,
            "is_group": is_group_tele,
            "media_url": url,
            "message_type": type_str,
            "sender_display_name": sender_name,
            "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None
        }

        cust_meta = { 
            "phone": sender_id, 
            "source": "telegram_userbot",
            "is_group": is_group_tele
        }

        # 5. Route
        result = await process_webhook_message_v2(
            agent=agent,
            channel="telegram",
            contact=contact_target,
            message_content=text,
            customer_name=sender_name,
            message_metadata=msg_meta,
            customer_metadata=cust_meta,
            supabase=supabase
        )

        return WebhookRouteResponse(
            success=True,
            chat_id=result.get("chat_id"),
            message_id=result.get("message_id"),
            customer_id=result.get("customer_id"),
            is_new_chat=result.get("is_new_chat", False),
            was_reopened=result.get("was_reopened", False),
            handled_by=result.get("handled_by", "manual_only"),
            status=result.get("status", "open"),
            channel="telegram",
            message=_generate_status_message(result)
        )

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"❌ Telegram Webhook Error: {e}", exc_info=True)
        return JSONResponse(content={"success": False, "error": str(e)})
                
# ============================================
# 4. WEBHOOK AI Ticket Update(Updated)
# ============================================

@router.put(
    "/ai/ticket/update",  # <--- FIXED: Removed {ticket_id} from path
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="AI Ticket Update Action",
    description="Allow AI to modify ticket details. STRICTLY GUARDED: Only works if current priority is LOW."
)
async def ai_update_ticket_webhook(
    payload: AITicketUpdatePayload, # <--- FIXED: Removed ticket_id arg
    secret: str = Depends(get_webhook_secret) 
):
    """
    Webhook for the AI Agent to autonomously update a ticket.
    """
    try:
        supabase = get_supabase_client()
        ticket_service = get_ticket_service()

        logger.info(f"🤖 AI Action: Attempting to update Ticket {payload.ticket_id}")

        existing = supabase.table("tickets")\
            .select("*")\
            .eq("id", payload.ticket_id)\
            .single()\
            .execute()

        if not existing.data:
            raise HTTPException(404, "Ticket not found")

        ticket = existing.data
        # Robustly handle missing priority key
        current_prio = str(ticket.get("priority") or "low").lower()

        # 2. 🛡️ GUARD: Low Priority Check
        # AI can ONLY touch 'low' priority tickets.
        if current_prio != "low":
            logger.warning(f"⛔ AI Guard Triggered: Attempted to modify {current_prio} ticket {payload.ticket_id}")
            return JSONResponse(
                status_code=403, 
                content={
                    "success": False,
                    "error": "AI_GUARD_VIOLATION",
                    "message": f"AI permission denied. Ticket priority is '{current_prio}', but AI can only modify 'low' priority tickets."
                }
            )

        # 3. Prepare Update Data
        update_data = TicketUpdate()
        changes_made = []

        if payload.title and payload.title != ticket.get("title"):
            update_data.title = payload.title
            changes_made.append(f"Title: {payload.title}")
        
        if payload.category and payload.category != ticket.get("category"):
            update_data.category = payload.category
            changes_made.append(f"Category: {payload.category}")

        if payload.description and payload.description != ticket.get("description"):
            update_data.description = payload.description
            changes_made.append("Description updated")
            
        if payload.priority and payload.priority.value != current_prio:
            update_data.priority = payload.priority
            changes_made.append(f"Priority: {payload.priority.value}")

        if not changes_made:
            return {"success": True, "message": "No changes detected", "ticket": ticket}

        # 4. Execute Update via Service
        # We explicitly log the actor as 'AI' (using None as ID, ActorType.AI)
        updated_ticket = await ticket_service.update_ticket(
            ticket_id=payload.ticket_id,
            update_data=update_data,
            actor_id=None, 
            actor_type=ActorType.AI 
        )

        # 5. Log the Reason specifically
        await ticket_service.log_activity(
            ticket_id=payload.ticket_id,
            action="ai_update",
            description=f"AI updated: {', '.join(changes_made)}. Reason: {payload.reason}",
            actor_id=None,
            actor_type=ActorType.AI
        )

        logger.info(f"✅ AI Updated Ticket {payload.ticket_id}: {payload.reason}")

        return {
            "success": True,
            "message": "Ticket updated successfully",
            "changes": changes_made,
            "ticket": updated_ticket
        }

    except Exception as e:
        logger.error(f"❌ AI Ticket Update Error: {e}")
        return JSONResponse(
            status_code=500, 
            content={"success": False, "error": str(e)}
        )