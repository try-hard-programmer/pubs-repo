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
from app.models.webhook import WhatsAppUnofficialWebhookMessage, WebhookRouteResponse, WhatsAppEventPayload
from app.models.ticket import TicketCreate, ActorType, TicketPriority, TicketDecision
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
    logger.info(f"🔄 [5. LID RESOLVER] Attempting to resolve LID: {contact}")
    try:
        logger.info(f"🔄 Resolving LID {contact} to real number...")
        
        from app.services.whatsapp_service import get_whatsapp_service
        wa_svc = get_whatsapp_service()
        
        # Attempt to resolve LID
        lookup = await wa_svc.get_contact_by_id(agent_id, contact)
        
        if lookup.get("success") and lookup.get("number"):
            resolved = lookup["number"]
            logger.info(f"✅ LID Resolved: {contact} → {resolved}")
            
            # TODO: Cache resolved number in customer metadata
            # This prevents needing to resolve the same LID multiple times
            logger.info(f"🔄 [5. LID RESOLVER] Result: {lookup.get('number', 'FAILED')}")
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
    Upload media file to Supabase storage bucket 'tmp'

    Args:
        media_data: Base64 encoded media data
        mime_type: MIME type of the media (e.g., "image/jpeg", "audio/ogg")
        file_extension: File extension (e.g., "jpg", "ogg")

    Returns:
        Signed public URL with token

    Raises:
        Exception: If upload fails
    """
    try:
        # Decode base64 data
        file_content = base64.b64decode(media_data)

        # Generate unique file ID
        file_id = str(uuid.uuid4())
        filename = f"{file_id}.{file_extension}"

        # Get Supabase client
        supabase = get_supabase_client()

        # Upload to tmp bucket
        bucket_name = "tmp"
        storage_path = filename

        # Upload file
        response = supabase.storage.from_(bucket_name).upload(
            path=storage_path,
            file=file_content,
            file_options={
                "content-type": mime_type,
                "cache-control": "3600",
                "upsert": "false"
            }
        )

        logger.info(f"✅ Uploaded media to storage: {bucket_name}/{storage_path}")

        # Get signed URL (valid for 1 hour)
        url_response = supabase.storage.from_(bucket_name).create_signed_url(
            storage_path,
            3600  # 1 hour
        )

        public_url = url_response.get("signedURL")
        logger.info(f"✅ Generated signed URL for media: {public_url}")

        return public_url

    except Exception as e:
        logger.error(f"Failed to upload media to Supabase: {e}")
        raise Exception(f"Media upload failed: {str(e)}")

def _extract_phone_number(whatsapp_id: str) -> str:
    if not whatsapp_id: return ""
    if "@lid" in whatsapp_id: return whatsapp_id
    if "@g.us" in whatsapp_id: return whatsapp_id # Keep Group IDs intact
    
    clean_id = whatsapp_id.split("@")[0].split(":")[0]
    return clean_id

async def _convert_unofficial_to_standard(unofficial: WhatsAppUnofficialWebhookMessage) -> WhatsAppWebhookMessage:
    """
    Unified converter: Extracts Real Sender Number & Enforces 'Group - Name' format.
    Fixes 'None' display by ensuring notifyName/pushName are populated.
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
        
    # [FIX] Logic for "Customer Name" -> "Group Name - Sender Name"
    if "@g.us" in chat_id:
        # 1. Get Group Name
        group_subject = (
            identity_source.get("chat", {}).get("name") or 
            identity_source.get("groupInfo", {}).get("subject") or
            wrapper.get("chat", {}).get("name")
        )
        
        # [FIX] Clean "Group g.us" or Empty Subject
        # If subject is missing OR it looks like an ID (contains @g.us), use a clean fallback
        if not group_subject or "@g.us" in str(group_subject):
            # Use last 4 digits of the numeric ID for a clean look "Group 9066"
            short_id = phone[-4:] if len(phone) > 4 else phone
            group_subject = f"Group {short_id}"
            
        # 2. Combine format: "Group Name - Sender Name"
        final_customer_name = f"{group_subject} - {real_sender_name}"
    else:
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
            "real_contact_number": real_number,    # Used for logic
            "real_sender_name": real_sender_name,  # Used for Display
            "notifyName": real_sender_name,        # [FIX] Ensure UI finds a name
            "pushName": real_sender_name,          # [FIX] Ensure UI finds a name
            "original_sender_id": sender_id,
            "whatsapp_name": real_sender_name
        }
    )

async def _handle_message_content_for_telegram(message_data: dict, data_type: str) -> Tuple[str, str, Optional[str]]:
    """Safe Content Handler for Telegram"""
    content = ""
    msg_type = "text"
    media_url = None
    
    if data_type == "media":
        # [FIX] Unwrap messageMedia if present (Tele-service nests it)
        media_obj = message_data.get("messageMedia", {}) or message_data
        
        base64_data = media_obj.get("data") or media_obj.get("body")
        if not base64_data: raise ValueError("Media data missing")

        mime = media_obj.get("mimetype", "application/octet-stream")
        content = media_obj.get("caption", "")

        if "image" in mime: msg_type = "image"
        elif "video" in mime: msg_type = "video"
        elif "audio" in mime: msg_type = "audio"
        elif "pdf" in mime: msg_type = "document"
        else: msg_type = "file"

        ext = "bin"
        if "/" in mime: ext = mime.split("/")[-1].replace("jpeg", "jpg")
        
        media_url = await _upload_media_to_supabase(base64_data, mime, ext)
    else:
        # Text
        data_wrapper = message_data.get("message", {}) or {}
        data_content = data_wrapper.get("_data", {}) or data_wrapper
        content = data_content.get("body", "")
        msg_type = "text"

    return content, msg_type, media_url

async def process_webhook_message_v2(
    agent: Dict, channel: str, contact: str, message_content: str,
    customer_name: Optional[str], message_metadata: Dict, customer_metadata: Dict, supabase
) -> Dict:
    """
    V2 Processor: 
    1. Route Message (One Chat = One Ticket/Session)
    2. Broadcast to WebSocket (with Media Polling)
    3. Busy & Schedule Checks (Blocking)
    4. Ticket Logic (with Redis Locking for safety)
    5. Enqueue AI
    """
    agent_id = agent["id"]
    org_id = agent["organization_id"]

    if agent.get("status") == "inactive":
        return {"success": False, "status": "dropped_inactive"}

    # =========================================================================
    # 1. ROUTING & STORAGE
    # =========================================================================
    router = get_message_router_service(supabase)
    
    # [GROUP SUPPORT] The router now receives 'participant' and 'is_group' via metadata
    res = await router.route_incoming_message(
        agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata
    )
    chat_id, cust_id, msg_id = res["chat_id"], res["customer_id"], res["message_id"]

    # 2. UPDATE CUSTOMER PHONE (Only for legitimate users, not Groups)
    is_group = message_metadata.get("is_group", False)
    if customer_metadata.get("phone") and cust_id and not is_group:
        try: 
            supabase.table("customers").update({"phone": customer_metadata["phone"]}).eq("id", cust_id).execute()
        except: 
            pass

    # =========================================================================
    # 3. WEBSOCKET BROADCAST (With High-Res Media Polling)
    # =========================================================================
    if app_settings.WEBSOCKET_ENABLED:
        try:
            fresh_metadata = message_metadata
            
            # [POLLING FIX] WhatsApp often sends a low-res thumbnail URL first, then updates DB.
            # We wait up to 3s to catch the high-res URL for the frontend.
            if channel == "whatsapp" and message_metadata.get("message_type") in ["image", "video", "document"]:
                initial_url = message_metadata.get("media_url")
                for _ in range(3): # 3 attempts * 1s = 3 seconds max
                    await asyncio.sleep(1)
                    try:
                        msg_res = supabase.table("messages").select("metadata").eq("id", msg_id).single().execute()
                        if msg_res.data and msg_res.data.get("metadata"):
                            db_meta = msg_res.data["metadata"]
                            # If URL changed (updated by backend storage service), use it
                            if db_meta.get("media_url") and db_meta.get("media_url") != initial_url:
                                fresh_metadata = db_meta
                                break
                    except: pass
            
            # Construct Attachment Data for Frontend
            attachment_data = None
            if fresh_metadata.get("media_url"):
                attachment_data = {
                    "url": fresh_metadata["media_url"], 
                    "type": fresh_metadata.get("message_type", "image"),
                    "name": "Media Attachment"
                }

            # [GROUP SUPPORT] Broadcast who actually sent the message
            sender_display = customer_name
            if is_group:
                participant = message_metadata.get("participant", "")
                sender_display = f"{participant} (in {customer_name})"

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

    # Stop if it was a merged chunk (don't re-process logic)
    if res.get("is_merged_event"): return res

    # 4. STOP IF HANDLED BY HUMAN
    if res.get("handled_by") == "human": 
        # Optional: Log WHO it is assigned to for debugging
        logger.info(f"🛑 Chat {chat_id} is assigned to Human. Skipping AI processing.")
        return res

    # =========================================================================
    # 5. BUSY CHECK (Blocking)
    # =========================================================================
    if agent.get("status") == "busy":
        logger.info(f"⛔ Agent Busy. Sending Auto-Reply and STOPPING AI.")
        msg = "Maaf, saat ini kami sedang sibuk."
        contact_info = {"phone": contact, "telegram_id": contact}
        
        await send_auto_reply(supabase, channel, agent_id, contact_info, msg)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "system_busy"}

    # =========================================================================
    # 6. SCHEDULE CHECK (Blocking)
    # =========================================================================
    schedule = await get_agent_schedule_config(agent_id, supabase)
    is_within, _ = is_within_schedule(schedule, datetime.now(ZoneInfo("UTC")))
    
    if not is_within:
        logger.info(f"🌙 Agent Out of Schedule. Sending Auto-Reply and STOPPING AI.")
        msg = "Maaf kami sedang tutup saat ini."
        
        # Flag user message as OOO
        try: supabase.table("messages").update({"metadata": {**message_metadata, "out_of_schedule": True}}).eq("id", msg_id).execute()
        except: pass
        
        chat_data = {"id": chat_id, "channel": channel, "sender_agent_id": agent_id}
        cust_data = {"phone": contact, "metadata": {"telegram_id": contact}}
        
        await send_message_via_channel(chat_data, cust_data, msg, supabase)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "ooo_system"}

    # =========================================================================
    # 7. LOGIC: TICKET CHECK & AI TRIGGER (LOCKED FOR SAFETY)
    # =========================================================================
    
    queue_svc = get_llm_queue()
    ticket_svc = get_ticket_service()
    ai_priority = "medium"

    # [CRITICAL] Redis Lock to prevent Double Ticket Creation
    async with acquire_lock(f"webhook:ticket:{chat_id}", expire=30, wait_time=5) as acquired:
        if not acquired:
            logger.warning(f"🔒 Lock Timeout for Chat {chat_id}. Assuming race condition handled.")
            ai_priority = "medium"
        else:
            try:
                active_ticket = None
                ticket_query = supabase.table("tickets").select("id, ticket_number, assigned_agent_id, priority")\
                    .eq("customer_id", cust_id).in_("status", ["open", "in_progress"]).limit(1).execute()
                
                if ticket_query.data:
                    # TICKET EXISTS
                    active_ticket = ticket_query.data[0]
                    if active_ticket.get("assigned_agent_id"):
                        return {**res, "handled_by": "human_ticket"}
                    ai_priority = str(active_ticket.get("priority", "medium")).lower()
                else:
                    # CREATE TICKET (NO GREETING)
                    logger.info(f"🆕 No Active Ticket. Creating New Default...")
                    ticket_config = parse_agent_config(agent.get("ticketing_config"))
                    
                    new_ticket_data = TicketCreate(
                        chat_id=chat_id,
                        customer_id=cust_id,
                        title=f"[LOW] New Interaction - {message_content[:40]}",
                        description=f"First Message: {message_content}\n\n[Auto-created: Low Priority Default]",
                        priority=TicketPriority.LOW,
                        category="General"
                    )
                    
                    await ticket_svc.create_ticket(
                        data=new_ticket_data,
                        organization_id=org_id,
                        ticket_config=ticket_config,
                        actor_id=None,
                        actor_type=ActorType.SYSTEM
                    )
                    
                    logger.info("✅ Default Low Priority Ticket Created (Silent).")
                    ai_priority = "low"

            except Exception as e:
                logger.error(f"❌ Ticket Creation Flow Error: {e}")
                
    # 8. QUEUE AI (Universal)
    await queue_svc.enqueue(
        chat_id=chat_id, 
        message_id=msg_id, 
        supabase_client=supabase, 
        priority=ai_priority
    )
    
    return {**res, "handled_by": "ai_v2_queued"}

# ============================================
# 2. WHATSAPP UNOFFICIAL WEBHOOK (Updated)
# ============================================

@router.post(
    "/wa-unofficial",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive WhatsApp message (Unofficial API)"
)
async def whatsapp_unofficial_webhook(
    message: WhatsAppUnofficialWebhookMessage,
    secret: str = Depends(get_webhook_secret)
):
    try:
        supabase = get_supabase_client()
        agent_id = message.sessionId

        # System Events
        system_events = ["qr", "authenticated", "ready", "disconnected", "loading_screen", "message_ack", "message_revoke", "status_find_partner"]
        if message.dataType in system_events:
            # logger.info(f"📡 System Event ({message.dataType}) - Ignored")
            agent_res = supabase.table("agents").select("organization_id").eq("id", agent_id).execute()
            if agent_res.data and app_settings.WEBSOCKET_ENABLED:
                org_id = agent_res.data[0]["organization_id"]
                await get_connection_manager().broadcast_to_organization(
                    message={"type": "whatsapp_status_update", "data": {"agent_id": agent_id, "status": message.dataType}},
                    organization_id=org_id
                )
            return JSONResponse(content={"success": True, "status": "processed_system_event"})

        # Structure Check
        data_wrapper = message.data.get("message", {}) or message.data.get("messageMedia", {})
        data_content = data_wrapper.get("_data", {}) or data_wrapper 
        whatsapp_id = data_content.get("id", {}).get("id")
        if not whatsapp_id:
             whatsapp_id = message.data.get("id", {}).get("id")

        if not whatsapp_id and message.dataType not in ["ready", "authenticated"]:
             return JSONResponse(content={"status": "ignored", "reason": "malformed_structure"})

        if data_content.get("isStatus") is True or data_content.get("isNotification") is True:
             return JSONResponse(content={"status": "ignored", "reason": "status_broadcast"})
        
        if data_content.get("id", {}).get("fromMe", False) or data_content.get("fromMe", False):
             return JSONResponse(content={"status": "ignored", "reason": "from_me"})

        # Content Check
        msg_body = data_content.get("body", "")
        has_media = message.dataType == "media" or data_content.get("mimetype") or message.data.get("mimetype")

        if not str(msg_body).strip() and not has_media:
            return JSONResponse(content={"status": "ignored", "reason": "zero_information_payload"})

        # Redis Dedup
        dedup_key = f"{whatsapp_id}_{message.dataType}"
        if await is_duplicate_message(dedup_key):
            # logger.info(f"⚡ Redis Dedup: Skipping duplicate {dedup_key}")
            return JSONResponse(content={"status": "ignored", "reason": "duplicate_redis_cache"})

        if message.dataType != "media":
            existing = supabase.table("messages").select("id").eq("metadata->>whatsapp_message_id", whatsapp_id).execute()
            if existing.data:
                return JSONResponse(content={"status": "ignored", "reason": "duplicate_db"})

        # Agent Verify
        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        if not agent_res.data: 
            return JSONResponse(status_code=200, content={"status": "error", "message": "Agent not found"})
        agent = agent_res.data[0]

        integration_res = supabase.table("agent_integrations").select("*").eq("agent_id", agent_id).eq("channel", "whatsapp").execute()
        if not integration_res.data or integration_res.data[0].get("enabled") is False:
            return JSONResponse(content={"status": "ignored", "reason": "integration_disabled"})

        # Settings & Standardize
        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        # -------------------------------------------------------------
        # 1. STANDARDIZE & GROUP CHECK
        # -------------------------------------------------------------
        standard_message = await _convert_unofficial_to_standard(message)
        meta = standard_message.metadata
        is_group = meta.get("is_group", False)

        # -------------------------------------------------------------
        # 2. GROUP MENTION FILTER
        # -------------------------------------------------------------
        if is_group:
            # Gather all possible IDs for "Me"
            potential_ids = set()
            
            # A. From Database
            if agent.get("phone"):
                potential_ids.add(str(agent.get("phone")).replace("+", "").strip())
                
            # B. From Session ID
            potential_ids.add(_extract_phone_number(agent_id))
            
            # C. From Injected 'me' object (Node.js)
            raw_me = message.data.get("me", {})
            if raw_me.get("wid"): potential_ids.add(_extract_phone_number(raw_me["wid"]))
            if raw_me.get("lid"): potential_ids.add(_extract_phone_number(raw_me["lid"]))
            
            # Extract Pushname
            pushname = raw_me.get("pushname")

            mentions = meta.get("mentioned_ids", [])
            
            is_mentioned = False
            
            # Check 1: Exact ID Match (Metadata)
            for my_id in potential_ids:
                if any(my_id in str(m) for m in mentions):
                    is_mentioned = True
                    logger.info(f"✅ Mention Metadata Match: {my_id}")
                    break
            
            # Check 2: Body Text Fallback (Smart Scan)
            final_content = standard_message.message or ""
            
            if not is_mentioned and final_content:
                # A. Check Phone IDs in text
                for my_id in potential_ids:
                    if f"@{my_id}" in final_content:
                        is_mentioned = True
                        final_content = final_content.replace(f"@{my_id}", "").strip() # Strip ID
                        logger.info(f"✅ Mention Body Match (ID): @{my_id}")
                        break
                
                # B. Check Pushname
                if not is_mentioned and pushname and f"@{pushname}" in final_content:
                    is_mentioned = True
                    final_content = final_content.replace(f"@{pushname}", "").strip() # Strip Name
                    logger.info(f"✅ Mention Body Match (Name): @{pushname}")

                # C. Heuristic LID Match (Backup)
                if not is_mentioned:
                    import re
                    # Find LID (@2...) pattern
                    lid_matches = re.findall(r"@\s?(2\d{10,17})", final_content)
                    if lid_matches:
                        detected_lid = lid_matches[0]
                        is_mentioned = True
                        # Cleanly strip the mention substring
                        final_content = re.sub(r"@\s?" + detected_lid, "", final_content).strip()
                        logger.info(f"🦸‍♂️ Heuristic Match: Found LID @{detected_lid}. Stripped & Processing.")

            if not is_mentioned:
                # logger.info(f"🔇 Dropping Group Msg: No matching mention found.")
                return JSONResponse(content={"status": "ignored", "reason": "group_no_mention"})
            
            # Update the message content to the cleaned version (Mention Removed)
            standard_message.message = final_content

        # -------------------------------------------------------------
        # 3. ZOMBIE CHECK (Ignore old messages)
        # -------------------------------------------------------------
        if standard_message.timestamp:
            try:
                msg_time = datetime.fromisoformat(standard_message.timestamp)
                if msg_time.tzinfo is None: msg_time = msg_time.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - msg_time).total_seconds()
                if age_seconds > 120:
                    logger.warning(f"⏳ Ignoring Old Message (Age: {int(age_seconds)}s). Zombie History Sync Protection.")
                    return JSONResponse(content={"status": "ignored", "reason": "too_old"})
            except Exception: pass

        # -------------------------------------------------------------
        # 4. RESOLVE IDENTITY
        # -------------------------------------------------------------
        contact_id = standard_message.phone_number # Default to 'from'
        
        # Resolve LID only if it's NOT a group
        if not is_group:
            contact_id = await resolve_lid_to_real_number(standard_message.phone_number, agent_id, "whatsapp", supabase)
        
        # Display Name Logic
        sender_name = standard_message.sender_name
        if sender_name and ("@lid" in sender_name or not sender_name.strip()):
             sender_name = contact_id

        # Clean ID: Strip @c.us for users, but KEEP @g.us for groups (to be safe/explicit)
        if "@c.us" in contact_id: 
            contact_id = contact_id.split("@")[0]
        
        # -------------------------------------------------------------
        # 5. PREPARE GROUP CONTEXT (Resolve LID & Inject Sender)
        # -------------------------------------------------------------
        final_message_content = standard_message.message
        
        if is_group:
            # 1. Get raw LID (e.g. 271...@lid)
            raw_participant = meta.get("participant", "")
            participant_id = raw_participant.split("@")[0]
            
            # 2. Resolve to Real Number (e.g. 628...@c.us)
            if "@lid" in raw_participant:
                real_number = await resolve_lid_to_real_number(raw_participant, agent_id, "whatsapp", supabase)
                if real_number and real_number != raw_participant:
                    participant_id = real_number.split("@")[0]

            # 3. Create Clean Context string
            # Database receives: "[Kurnia] (@628...): hello" (Clean)
            # Instead of: "[Kurnia] (@271...): hello" (Garbage)
            if participant_id:
                sender_display = sender_name or "Unknown"
                final_message_content = f"[{sender_display}] (@{participant_id}): {standard_message.message}"

        # -------------------------------------------------------------
        # 6. EXECUTE PROCESSOR V2
        # -------------------------------------------------------------
        result = await process_webhook_message_v2(
            agent=agent,
            channel="whatsapp",
            contact=contact_id, # Group ID (@g.us) OR User ID (clean)
            message_content=final_message_content, # <--- Contains the "Mention Back" context
            customer_name=sender_name or contact_id,
            message_metadata={
                "whatsapp_message_id": standard_message.message_id,
                "message_type": standard_message.message_type,
                "timestamp": standard_message.timestamp,
                "is_lid": "@lid" in standard_message.phone_number,
                "media_url": standard_message.media_url,
                "is_group": is_group,
                "participant": meta.get("participant"),
                "sender_display_name": sender_name,
                **standard_message.metadata
            },
            customer_metadata={
                "whatsapp_name": sender_name,
                "is_group": is_group
            },
            supabase=supabase
        )

        return WebhookRouteResponse(
            success=True, chat_id=result["chat_id"], message_id=result["message_id"],
            customer_id=result["customer_id"], is_new_chat=result["is_new_chat"],
            was_reopened=result["was_reopened"], handled_by=result["handled_by"],
            status=result["status"], channel="whatsapp", message=_generate_status_message(result)
        )

    except Exception as e:
        logger.error(f"❌ Unofficial Webhook Critical Error: {e}")
        return JSONResponse(status_code=200, content={"success": False, "error": str(e)})
# ============================================
# 3. TELEGRAM USERBOT WEBHOOK (Updated)
# ============================================

@router.post("/telegram-userbot", response_model=WebhookRouteResponse)
async def telegram_userbot_webhook(
    payload: WhatsAppUnofficialWebhookMessage, 
    secret: str = Depends(get_webhook_secret)
):
    try:
        agent_id = payload.sessionId
        raw_data = payload.data
        
        # 1. Adapt Content Source
        if payload.dataType == "media":
            content_source = raw_data
            data_wrapper = raw_data.get("message", {}) or {}
            data_content = data_wrapper.get("_data", {}) or data_wrapper
        else:
            data_wrapper = raw_data.get("message", {}) or {}
            data_content = data_wrapper.get("_data", {}) or data_wrapper
            content_source = raw_data

        # 2. Basic Validation
        if not data_content and payload.dataType != "media": 
            raise HTTPException(status_code=400, detail="Invalid JSON structure")
        
        # 3. Redis Idempotency Check
        msg_id = data_content.get("id", {}).get("id")
        if msg_id:
            dedup_key = f"tg_{msg_id}" # Unique prefix for Telegram
            if await is_duplicate_message(dedup_key):
                logger.info(f"⚡ Redis Dedup: Skipping duplicate Telegram ID {msg_id}")
                return JSONResponse(content={"success": True, "status": "ignored_duplicate"})

        # 4. Agent Verification
        supabase = get_supabase_client()
        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        if not agent_res.data: 
            raise HTTPException(404, "Agent not found")
        agent = agent_res.data[0]
        
        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        # 5. Identity & Group Extraction
        # Note: In Telethon 'incoming' events:
        # 'from' = User ID (Sender)
        # 'to' = Chat ID (Group) OR Agent ID (if DM)
        
        sender_id = str(data_content.get("from", ""))
        sender_display_name = data_content.get("notifyName") or f"User {sender_id}"
        timestamp_unix = data_content.get("t")
        
        raw_to = str(data_content.get("to", ""))
        
        # Check flags passed by listener.py
        is_group_tele = data_content.get("is_group") or (raw_to != str(agent_id) and raw_to != "")
        is_mentioned = data_content.get("mentioned", False)
        
        # 6. Group Filtering Logic
        contact_target = sender_id # Default to DM
        participant_id = None
        
        if is_group_tele:
            # STRICT FILTER: Must be mentioned
            if not is_mentioned:
                return JSONResponse(content={"success": True, "status": "ignored_group_no_mention"})
            
            # Contact becomes the Group ID
            contact_target = raw_to 
            participant_id = sender_id
            logger.info(f"🔔 Telegram Group Mention in {contact_target} by {sender_id}")

        # Phone logic (Only applicable for DMs usually)
        raw_phone = data_content.get("phone")
        final_phone = sender_id
        if raw_phone and str(raw_phone).lower() not in ["none", "null", ""]:
            final_phone = str(raw_phone)

        # 7. Content Extraction
        try:
            message_text, msg_type, media_url = await _handle_message_content_for_telegram(content_source, payload.dataType)
        except Exception as e:
            logger.error(f"❌ Telegram Content Parse Error: {e}")
            message_text = ""
            msg_type = "text"
            media_url = None

        # 8. Prepare Metadata
        msg_meta = {
            "source_format": "wa_unofficial_json", 
            "telegram_message_id": msg_id, 
            "telegram_sender_id": sender_id, 
            "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None,
            "media_url": media_url,
            "message_type": msg_type,
            "is_group": is_group_tele,
            "participant": participant_id,
            "sender_display_name": sender_display_name
        }
        
        cust_meta = { 
            "telegram_id": contact_target, 
            "phone": final_phone if not is_group_tele else None, # Don't map group ID to phone
            "source": "telegram_userbot",
            "is_group": is_group_tele
        }

        # 9. Process via V2
        result = await process_webhook_message_v2(
            agent=agent, 
            channel="telegram", 
            contact=contact_target, # User ID or Group ID
            message_content=message_text, 
            customer_name=sender_display_name, # User Name
            message_metadata=msg_meta, 
            customer_metadata=cust_meta, 
            supabase=supabase
        )
        
        # 10. Return Response
        return WebhookRouteResponse(
            success=True, 
            chat_id=result.get("chat_id"), 
            message_id=result.get("message_id"), 
            customer_id=result.get("customer_id"), 
            is_new_chat=result.get("is_new_chat", False),
            was_reopened=result.get("was_reopened", False), 
            handled_by=result.get("handled_by", "ai_v2"), 
            status=result.get("status", "open"), 
            channel="telegram", 
            message=_generate_status_message(result)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Telegram Userbot Critical: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    