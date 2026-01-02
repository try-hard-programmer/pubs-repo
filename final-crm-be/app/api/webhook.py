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

# [CHANGE] Import the V2 AI Trigger
from app.services.dynamic_ai_service_v2 import process_dynamic_ai_response_v2 
# Keep V1 for backward compatibility if needed
from app.services.ai_response_service import process_ai_response_async

from app.config import settings as app_settings
from app.models.webhook import WhatsAppUnofficialWebhookMessage, WebhookRouteResponse, WhatsAppEventPayload
from app.models.ticket import TicketCreate, ActorType, TicketPriority, TicketDecision
from app.services.ticket_service import get_ticket_service
from app.api.crm_chats import send_message_via_channel
from app.utils.ml_guard import ml_guard
from app.utils.schedule_validator import get_agent_schedule_config, is_within_schedule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


class SimpleCache:
    """
    Optimized dictionary-based cache with TTL for deduplication.
    [FIX] Performance optimized to avoid O(N) scan on every request.
    """
    def __init__(self, ttl_seconds=300): 
        self.cache = {}
        self.ttl = ttl_seconds
        self.last_cleanup = time.time()

    def is_duplicate(self, key):
        now = time.time()
        
        if now - self.last_cleanup > 60:
            self._cleanup(now)

        if key in self.cache:
            if now - self.cache[key] > self.ttl:
                self.cache[key] = now # Refresh and treat as new
                return False
            return True
        
        self.cache[key] = now
        return False

    def _cleanup(self, now):
        self.last_cleanup = now
        keys_to_remove = [k for k, t in self.cache.items() if now - t > self.ttl]
        for k in keys_to_remove:
            del self.cache[k]

dedup_cache = SimpleCache(ttl_seconds=300)

# ============================================
# HELPER FUNCTIONS
# ============================================

def _generate_status_message(result: dict) -> str:
    if result.get("status") == "dropped_inactive": return "Message dropped (Agent Inactive)"
    if result.get("handled_by") == "system_busy": return "Auto-reply sent (Agent Busy)"
    return "Message processed"

async def process_auto_ticket_async(
    chat_id: str,
    customer_id: str,
    organization_id: str,
    customer_name: str,
    message_content: str,
    decision: TicketDecision, 
    supabase
):
    """
    Background task to auto-create tickets based on the Guard's decision.
    """
    try:
        # 1. Check for existing active ticket to avoid duplicates
        active_tickets = supabase.table("tickets") \
            .select("id, ticket_number") \
            .eq("chat_id", chat_id) \
            .in_("status", ["open", "in_progress"]) \
            .execute()
        
        if active_tickets.data:
            existing_ticket = active_tickets.data[0]
            logger.info(f"ℹ️  Skipping auto-ticket: Active ticket {existing_ticket['ticket_number']} already exists for chat {chat_id}")
            return # Ticket exists, do nothing

        logger.info(f"🎫 Creating Auto-Ticket for {chat_id}. Priority: {decision.suggested_priority}")

        # 2. Use the TicketService to create (handles DB + Logging)
        ticket_service = get_ticket_service()
        
        new_ticket_data = TicketCreate(
            chat_id=chat_id,
            customer_id=customer_id,
            title=f"Support Request: {customer_name}",
            description=f"Message: {message_content}\n\n[Auto-created: {decision.reason}]",
            priority=decision.suggested_priority or TicketPriority.MEDIUM, 
            category=decision.suggested_category or "inquiry"
        )

        new_ticket = await ticket_service.create_ticket(
            data=new_ticket_data,
            organization_id=organization_id,
            actor_id=None,
            actor_type=ActorType.SYSTEM
        )

        logger.info(f"✅ Auto-Ticket created successfully: {new_ticket.ticket_number}")

    except Exception as e:
        logger.error(f"❌ Auto-Ticket creation failed: {e}")

def is_agent_busy(agent: dict) -> bool:
    """
    Check if agent is busy

    Args:
        agent: Agent data from database

    Returns:
        True if agent status is 'busy', False otherwise
    """
    return agent.get("status") == "busy"

async def send_busy_agent_auto_reply(
    agent: dict,
    channel: str,
    contact: str,
    supabase
) -> bool:
    """
    Send automatic reply when agent is busy

    Args:
        agent: Agent data from database
        channel: Channel type (whatsapp, telegram, email)
        contact: Customer contact (phone number, telegram_id, or email)
        supabase: Supabase client

    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        # Check if agent status is 'busy'
        if not is_agent_busy(agent):
            return False

        agent_id = agent["id"]
        logger.info(f"🔔 Agent {agent['name']} is BUSY - sending auto-reply to {contact}")

        # Get agent integration for this channel
        integration_response = supabase.table("agent_integrations").select("*").eq(
            "agent_id", agent_id
        ).eq("channel", channel).execute()

        if not integration_response.data:
            logger.warning(f"⚠️  No integration found for agent {agent_id} on channel {channel}")
            return False

        integration = integration_response.data[0]

        # Check if integration is enabled and connected
        if not integration.get("enabled") or integration.get("status") != "connected":
            logger.warning(f"⚠️  Integration not enabled or not connected for agent {agent_id}")
            return False

        # Prepare auto-reply message
        auto_reply_message = "Mohon maaf agent saat ini sedang sibuk dan akan menghubungi anda dalam beberapa saat kemudian"

        # Send message based on channel
        if channel == "whatsapp":
            # Get WhatsApp service
            from app.services.whatsapp_service import get_whatsapp_service
            whatsapp_service = get_whatsapp_service()

            # Send text message via WhatsApp
            try:
                result = await whatsapp_service.send_text_message(
                    session_id=agent_id,  # session_id is same as agent_id
                    phone_number=contact,
                    message=auto_reply_message
                )

                if result.get("success"):
                    logger.info(f"✅ Auto-reply sent to {contact} via WhatsApp")
                    return True
                else:
                    logger.error(f"❌ Failed to send auto-reply via WhatsApp: {result}")
                    return False

            except Exception as e:
                logger.error(f"❌ Error sending auto-reply via WhatsApp: {e}")
                return False

        elif channel == "telegram":
            # TODO: Implement Telegram auto-reply when telegram service is available
            logger.warning(f"⚠️  Telegram auto-reply not implemented yet")
            return False

        elif channel == "email":
            # TODO: Implement Email auto-reply when email service is available
            logger.warning(f"⚠️  Email auto-reply not implemented yet")
            return False

        else:
            logger.warning(f"⚠️  Unknown channel: {channel}")
            return False

    except Exception as e:
        logger.error(f"❌ Error in send_busy_agent_auto_reply: {e}")
        return False

async def send_out_of_schedule_message(
    agent: dict,
    channel: str,
    contact: str,
    supabase
) -> bool:
    """
    Send automatic message when agent is outside working hours.

    Only sends message for AI agents. Human agents are expected to handle manually.

    Args:
        agent: Agent data from database
        channel: Channel type (whatsapp, telegram, email)
        contact: Customer contact (phone number, telegram_id, or email)
        supabase: Supabase client

    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        # Only send message for AI agents
        if agent.get("user_id") is not None:
            logger.debug(f"ℹ️  Agent {agent['name']} is human - skipping out-of-schedule message")
            return False

        agent_id = agent["id"]
        logger.info(f"⏰ Agent {agent['name']} is OUT OF SCHEDULE - sending auto-message to {contact}")

        # Get agent integration for this channel
        integration_response = supabase.table("agent_integrations").select("*").eq(
            "agent_id", agent_id
        ).eq("channel", channel).execute()

        if not integration_response.data:
            logger.warning(f"⚠️  No integration found for agent {agent_id} on channel {channel}")
            return False

        integration = integration_response.data[0]

        # Check if integration is enabled and connected
        if not integration.get("enabled") or integration.get("status") != "connected":
            logger.warning(f"⚠️  Integration not enabled or not connected for agent {agent_id}")
            return False

        # Prepare out-of-schedule message
        out_of_schedule_message = "Mohon di tunggu sebentar akan di arahkan ke admin kami"

        # Send message based on channel
        if channel == "whatsapp":
            # Get WhatsApp service
            from app.services.whatsapp_service import get_whatsapp_service
            whatsapp_service = get_whatsapp_service()

            # Send text message via WhatsApp
            try:
                result = await whatsapp_service.send_text_message(
                    session_id=agent_id,  # session_id is same as agent_id
                    phone_number=contact,
                    message=out_of_schedule_message
                )

                if result.get("success"):
                    logger.info(f"✅ Out-of-schedule message sent to {contact} via WhatsApp")
                    return True
                else:
                    logger.error(f"❌ Failed to send out-of-schedule message via WhatsApp: {result}")
                    return False

            except Exception as e:
                logger.error(f"❌ Error sending out-of-schedule message via WhatsApp: {e}")
                return False

        elif channel == "telegram":
            # TODO: Implement Telegram out-of-schedule message when telegram service is available
            logger.warning(f"⚠️  Telegram out-of-schedule message not implemented yet")
            return False

        elif channel == "email":
            # TODO: Implement Email out-of-schedule message when email service is available
            logger.warning(f"⚠️  Email out-of-schedule message not implemented yet")
            return False

        else:
            logger.warning(f"⚠️  Unknown channel: {channel}")
            return False

    except Exception as e:
        logger.error(f"❌ Error in send_out_of_schedule_message: {e}")
        return False

async def flag_message_as_out_of_schedule(
    message_id: str,
    reason: str,
    supabase
) -> bool:
    """
    Flag message as out-of-schedule by updating message metadata.

    This allows frontend/admin to identify messages that need human attention
    because they arrived outside working hours.

    Args:
        message_id: UUID of the message to flag
        reason: Reason why message is out of schedule (e.g., "Outside working hours: Sunday")
        supabase: Supabase client

    Returns:
        True if flagging successful, False otherwise
    """
    try:
        logger.info(f"🚩 Flagging message {message_id} as out-of-schedule: {reason}")

        # Prepare metadata update
        # Note: We're using JSONB merge, so existing metadata won't be lost
        flag_metadata = {
            "out_of_schedule": True,
            "out_of_schedule_reason": reason,
            "out_of_schedule_flagged_at": datetime.utcnow().isoformat()
        }

        # First, get existing metadata
        existing_response = supabase.table("messages") \
            .select("metadata") \
            .eq("id", message_id) \
            .execute()

        if not existing_response.data:
            logger.warning(f"⚠️  Message {message_id} not found")
            return False

        # Merge with existing metadata
        existing_metadata = existing_response.data[0].get("metadata", {})
        merged_metadata = {**existing_metadata, **flag_metadata}

        # Update message metadata
        update_response = supabase.table("messages") \
            .update({"metadata": merged_metadata}) \
            .eq("id", message_id) \
            .execute()

        if update_response.data:
            logger.info(f"✅ Message {message_id} flagged as out-of-schedule successfully")
            return True
        else:
            logger.error(f"❌ Failed to flag message {message_id}")
            return False

    except Exception as e:
        logger.error(f"❌ Error flagging message as out-of-schedule: {e}")
        return False

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

async def check_telegram_idempotency(supabase, msg_id: str) -> bool:
    if not msg_id: return False
    res = supabase.table("messages").select("id").eq("metadata->>telegram_message_id", str(msg_id)).execute()
    if res.data:
        logger.warning(f"🛑 Duplicate Message ID {msg_id}")
        return True
    return False

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
    [FIX] Now detects message_type based on MIME type, not just the generic 'type' label.
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
        
        # [FIX] Detect Type from MIME (More accurate than raw_type)
        if "image" in mime:
            msg_type = "image"
        elif "video" in mime:
            msg_type = "video"
        elif "audio" in mime:
            msg_type = "audio"
        elif "pdf" in mime:
            msg_type = "document"
        else:
            msg_type = "file"
            
        # [FIX] Improved Extension Logic (Simple fallback)
        ext = "bin"
        if "/" in mime:
            ext = mime.split("/")[-1].replace("jpeg", "jpg")
        else:
            ext_map = {"image": "jpg", "ptt": "ogg", "document": "pdf", "audio": "mp3", "video": "mp4"}
            ext = ext_map.get(raw_type, "bin")
        
        media_url = await _upload_media_to_supabase(base64_data, mime, ext)
        
        # Content is ONLY caption
        content = media_obj.get("caption", "")
    else:
        # Handle Text
        body = message_data.get("body") or message_data.get("_data", {}).get("body", "")
        
        # Check for Base64 sneaking in as text
        if body and isinstance(body, str) and (body.startswith("/9j/") or (len(body) > 500 and " " not in body[:50])):
            try:
                media_url = await _upload_media_to_supabase(body, "image/jpeg", "jpg")
                content = "" 
                msg_type = "image"
            except: content = body
        else:
            content = body
            
    return content, msg_type, media_url

# ============================================
# MAIN PROCESSOR (FIXED)
# ============================================

def get_supabase_client():
    from supabase import create_client
    if not app_settings.is_supabase_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase is not configured")
    return create_client(app_settings.SUPABASE_URL, app_settings.SUPABASE_SERVICE_KEY)

async def process_webhook_message(
    agent: Dict, channel: str, contact: str, message_content: str,
    customer_name: Optional[str], message_metadata: Dict, customer_metadata: Dict, supabase
) -> Dict:
    
    agent_id = agent["id"]
    org_id = agent["organization_id"]

    if agent.get("status") == "inactive":
        return {"success": False, "status": "dropped_inactive"}

    # 1. ROUTING
    router = get_message_router_service(supabase)
    res = await router.route_incoming_message(agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata)
    chat_id, cust_id, msg_id = res["chat_id"], res["customer_id"], res["message_id"]

    # 2. UPDATE PHONE
    if customer_metadata.get("phone") and cust_id:
        try: supabase.table("customers").update({"phone": customer_metadata["phone"]}).eq("id", cust_id).execute()
        except: pass

    # 3. BROADCAST TO FRONTEND
    if app_settings.WEBSOCKET_ENABLED:
        try:
            attachment_data = None
            if message_metadata.get("media_url"):
                attachment_data = {
                    "url": message_metadata["media_url"],
                    "type": message_metadata.get("message_type", "image"),
                    "name": "Media Attachment"
                }

            await get_connection_manager().broadcast_new_message(
                organization_id=org_id, chat_id=chat_id, message_id=msg_id,
                customer_id=cust_id, customer_name=customer_name or "Unknown",
                message_content=message_content, channel=channel, handled_by=res["handled_by"],
                sender_type="customer", sender_id=cust_id, is_new_chat=res["is_new_chat"],
                was_reopened=res.get("was_reopened", False), metadata=message_metadata,
                attachment=attachment_data
            )
        except Exception as e: logger.error(f"❌ WS Broadcast Failed: {e}")

    if res.get("is_merged_event"): return res

    # ==========================================================================
    # [FIX] STOP IF HANDLED BY HUMAN
    # Ini mencegah bot membalas jika chat sudah di-assign ke agen manusia
    # ==========================================================================
    if res.get("handled_by") == "human":
        logger.info(f"🛑 Chat {chat_id} is handled by Human. AI/Guard stopped.")
        return res

    # 5. BUSY CHECK
    if agent.get("status") == "busy":
        msg = "Maaf, saat ini kami sedang sibuk."
        contact_info = {"phone": contact, "telegram_id": contact}
        await send_auto_reply(supabase, channel, agent_id, contact_info, msg)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "system_busy"}

    # ACTIVE TICKET CHECK (Backup check, biasanya sudah tercover oleh handled_by)
    active_ticket = None
    ticket_query = supabase.table("tickets").select("id, assigned_agent_id").eq("customer_id", cust_id).in_("status", ["open", "in_progress"]).limit(1).execute()
    if ticket_query.data:
        active_ticket = ticket_query.data[0]
        if active_ticket.get("assigned_agent_id"):
            return {**res, "handled_by": "human_ticket"}

    # SCHEDULE CHECK
    schedule = await get_agent_schedule_config(agent_id, supabase)
    is_within, _ = is_within_schedule(schedule, datetime.now(ZoneInfo("UTC")))
    if not is_within:
        msg = "Maaf kami sedang tutup saat ini."
        supabase.table("messages").update({"metadata": {**message_metadata, "out_of_schedule": True}}).eq("id", msg_id).execute()
        chat_data = {"id": chat_id, "channel": channel, "sender_agent_id": agent_id}
        cust_data = {"phone": contact, "metadata": {"telegram_id": contact}}
        await send_message_via_channel(chat_data, cust_data, msg, supabase)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel)
        return {**res, "handled_by": "ooo_system"}

    # INTELLIGENCE (AI GUARD)
    # Kode di bawah ini hanya akan jalan jika handled_by == "ai" atau "unassigned"
    ticket_config = agent.get("ticketing_config") or {}
    if isinstance(ticket_config, str):
        try: ticket_config = json.loads(ticket_config)
        except: ticket_config = {}

    should_trigger_ai = True

    if ticket_config.get("enabled"):
        should_ticket, pred_cat, prio_str, conf, reason, ticket_title = ml_guard.predict(message_content)
        
        if prio_str == "low":
            logger.info(f"🛡️ Low Priority ({reason}). Sending Greeting & Stopping AI.")
            resolved_contact = await resolve_lid_to_real_number(contact, agent_id, channel, supabase)
            
            # Clean Name Display
            display_name = customer_name or 'Kak'
            if "@lid" in str(display_name) or "User" in str(display_name):
                display_name = str(resolved_contact).split("@")[0]

            greeting_msg = (
                f"Halo {display_name}! 👋\n\n"
                "Pesan Anda telah kami terima melalui platform Syntra.\n"
                "Silakan jelaskan permasalahan yang Anda alami secara lebih rinci agar kami dapat membantu Anda dengan lebih baik."
            )
            chat_data = {"id": chat_id, "channel": channel, "sender_agent_id": agent_id}
            cust_data = {"phone": resolved_contact, "metadata": {"telegram_id": resolved_contact}}
            
            await send_message_via_channel(chat_data, cust_data, greeting_msg, supabase)
            await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, greeting_msg, channel, agent_name=agent["name"])
            
            # Auto create low priority ticket if configured
            if not active_ticket and ticket_config.get("autoCreateTicket"):
                try: final_prio = TicketPriority.LOW
                except: final_prio = "low"
                ticket_svc = get_ticket_service()
                ticket_data = TicketCreate(
                    chat_id=chat_id, customer_id=cust_id,
                    title=f"[LOW] {pred_cat.upper()} - {message_content[:40]}",
                    description=f"Message: {message_content}",
                    priority=final_prio, category=pred_cat
                )
                await ticket_svc.create_ticket(ticket_data, org_id, ticket_config, None, ActorType.SYSTEM)

            should_trigger_ai = False
            return {**res, "handled_by": "system_greeting"}

        elif should_ticket:
            # ... (Logic pembuatan tiket tetap sama) ...
            final_cat = pred_cat
            if ticket_config.get("categories") and pred_cat not in ticket_config["categories"]:
                final_cat = ticket_config["categories"][0]
            try: final_prio = TicketPriority(prio_str)
            except: final_prio = TicketPriority.MEDIUM

            if active_ticket:
                # Update existing ticket logic...
                pass     
            elif ticket_config.get("autoCreateTicket"):
                # Create new ticket logic...
                ticket_svc = get_ticket_service()
                ticket_data = TicketCreate(
                    chat_id=chat_id, customer_id=cust_id,
                    title=ticket_title, description=f"Message: {message_content}",
                    priority=final_prio, category=final_cat
                )
                await ticket_svc.create_ticket(ticket_data, org_id, ticket_config, None, ActorType.SYSTEM)

    if should_trigger_ai:
        asyncio.create_task(process_ai_response_async(chat_id, msg_id, supabase))
        return {**res, "handled_by": "ai"}
    
    return res

# ============================================
# WHATSAPP WEBHOOK
# ============================================

@router.post(
    "/whatsapp",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive WhatsApp message",
    description="Webhook endpoint to receive incoming messages from WhatsApp service"
)
async def whatsapp_webhook(
    payload: WhatsAppEventPayload,
    secret: str = Depends(get_webhook_secret)
):
    try:
        # 1. FILTER: Handle System Events (QR, Loading, Status) -> Ignore them
        if payload.qr:
            return JSONResponse(content={"status": "ignored", "reason": "qr_code"})
            
        if isinstance(payload.message, str):
            # If 'message' is a string (e.g. "WhatsApp"), it's a loading status
            return JSONResponse(content={"status": "ignored", "reason": "status_update"})

        if not isinstance(payload.message, dict):
            return JSONResponse(content={"status": "ignored", "reason": "no_message_data"})

        # Extract the core message object
        # The container usually sends: { "message": { "_data": { ... } } }
        msg_obj = payload.message
        msg_data = msg_obj.get("_data", msg_obj) # Fallback to msg_obj if _data missing

        # 2. FILTER: Ignore Status Broadcasts
        if msg_data.get("isStatus") is True or msg_data.get("type") == "e2e_notification":
             return JSONResponse(content={"status": "ignored", "reason": "status_broadcast"})

        # 3. FILTER: Ignore Self-Replies (Infinite Loop Prevention)
        # Check id.fromMe (official structure) or key.fromMe (some libraries)
        is_from_me = False
        if isinstance(msg_data.get("id"), dict):
            is_from_me = msg_data["id"].get("fromMe", False)
        elif isinstance(msg_data.get("key"), dict):
             is_from_me = msg_data["key"].get("fromMe", False)
        
        # Also check boolean flag directly if present
        if msg_data.get("fromMe") is True:
            is_from_me = True

        if is_from_me:
            logger.info("♻️ Ignoring message from self (fromMe=True)")
            return JSONResponse(content={"status": "ignored", "reason": "from_me"})

        logger.info(f"📱 WhatsApp webhook received")

        # 4. DATA EXTRACTION
        # 'from': "6281317966173@c.us" (Sender)
        # 'to': "6287874134867@c.us" (Agent/Receiver)
        raw_from = msg_data.get("from", "")
        raw_to = msg_data.get("to", "")
        
        # Clean numbers (remove @c.us / @g.us)
        phone_number = raw_from.split("@")[0] if "@" in raw_from else raw_from
        to_number = raw_to.split("@")[0] if "@" in raw_to else raw_to
        
        message_content = msg_data.get("body", "")
        
        # Sender Name Logic
        sender_name = msg_data.get("notifyName")
        if not sender_name:
            sender_name = f"User {phone_number}"

        message_id = msg_data.get("id", {}).get("id") if isinstance(msg_data.get("id"), dict) else None
        timestamp = msg_data.get("t")

        # 5. AGENT LOOKUP
        supabase = get_supabase_client()
        agent_finder = get_agent_finder_service(supabase)
        
        # Find agent by the 'to' number (the agent's number)
        agent = await agent_finder.find_agent_by_whatsapp_number(phone_number=to_number)

        if not agent:
            # Fallback: try finding by raw ID or partial match
            logger.warning(f"⚠️ Agent not found for {to_number}, retrying...")
            agent = await agent_finder.find_agent_by_whatsapp_number(phone_number=raw_to)
            
        if not agent:
            logger.error(f"❌ No agent integration found for WhatsApp number: {to_number}")
            # Return 404 so we know it failed, or 200 to stop retries if configured
            raise HTTPException(status_code=404, detail="Agent not found for this number")

        organization_id = agent["organization_id"]

        # 6. METADATA PREPARATION
        message_metadata = {
            "whatsapp_message_id": message_id,
            "original_from": raw_from,
            "timestamp": timestamp,
            "source_raw": "chrishubert_api"
        }
        
        customer_metadata = {
            "whatsapp_name": sender_name
        }

        # 7. PROCESS & ROUTE
        result = await process_webhook_message(
            agent=agent,
            channel="whatsapp",
            contact=phone_number,
            message_content=message_content,
            customer_name=sender_name,
            message_metadata=message_metadata,
            customer_metadata=customer_metadata,
            supabase=supabase
        )

        return WebhookRouteResponse(
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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing WhatsApp webhook: {e}")
        # Return 200 OK with error details to stop WhatsApp from retrying indefinitely on bad logic
        return JSONResponse(
            status_code=200, 
            content={"success": False, "error": str(e)}
        )

# ============================================
# WHATSAPP UNOFFICIAL WEBHOOK
# ============================================

# Reworking
# @router.post(
#     "/wa-unofficial",
#     response_model=WebhookRouteResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Receive WhatsApp message (Unofficial API)"
# )
# async def whatsapp_unofficial_webhook(
#     message: WhatsAppUnofficialWebhookMessage,
#     secret: str = Depends(get_webhook_secret)
# ):
#     try:
#         supabase = get_supabase_client()
#         agent_id = message.sessionId

#         # ==================================================================
#         # 1. VISIBILITY LAYER (Make the Invisible Visible)
#         # ==================================================================
#         # We log the raw payload structure so you can see exactly what the "Zombie" sends.
#         import json
#         try:
#             # Truncate huge base64 strings for cleaner logs
#             debug_payload = message.data.copy() if isinstance(message.data, dict) else {}
#             if "body" in debug_payload and len(str(debug_payload["body"])) > 200:
#                 debug_payload["body"] = "[BASE64_DATA_TRUNCATED]"
            
#             logger.info(f"📦 [RAW INCOMING] Session: {agent_id} | Type: {message.dataType} | Payload Keys: {list(debug_payload.keys())}")
#         except: 
#             logger.info(f"📦 [RAW INCOMING] Session: {agent_id} | Type: {message.dataType}")

#         # ==================================================================
#         # 2. SYSTEM EVENT FILTER (Dynamic)
#         # ==================================================================
#         # Hard system events that we know are not user messages
#         system_events = ["qr", "authenticated", "ready", "disconnected", "loading_screen", "message_ack", "message_revoke", "status_find_partner"]
        
#         if message.dataType in system_events:
#             logger.info(f"📡 System Event ({message.dataType}) - Ignored")
#             # Broadcast status updates to frontend if needed
#             agent_res = supabase.table("agents").select("organization_id").eq("id", agent_id).execute()
#             if agent_res.data and app_settings.WEBSOCKET_ENABLED:
#                 org_id = agent_res.data[0]["organization_id"]
#                 await get_connection_manager().broadcast_to_organization(
#                     message={"type": "whatsapp_status_update", "data": {"agent_id": agent_id, "status": message.dataType}},
#                     organization_id=org_id
#                 )
#             return JSONResponse(content={"success": True, "status": "processed_system_event"})

#         # ==================================================================
#         # 3. STRUCTURAL VALIDATION (The "Dynamic" Guard)
#         # ==================================================================
#         # Extract the inner data structure dynamically
#         data_wrapper = message.data.get("message", {}) or message.data.get("messageMedia", {})
#         data_content = data_wrapper.get("_data", {}) or data_wrapper 
        
#         # A valid message MUST have an ID. If not, it's garbage/ghost data.
#         whatsapp_id = data_content.get("id", {}).get("id")
#         if not whatsapp_id:
#              # Sometimes the ID is at the root level in certain events
#              whatsapp_id = message.data.get("id", {}).get("id")

#         if not whatsapp_id and message.dataType not in ["ready", "authenticated"]:
#              logger.warning("🗑️ Dropping Event: Malformed Structure (No Message ID found)")
#              return JSONResponse(content={"status": "ignored", "reason": "malformed_structure"})

#         # Check Flags (Status Updates, Broadcasts, Self-Messages)
#         if data_content.get("isStatus") is True or data_content.get("isNotification") is True:
#              return JSONResponse(content={"status": "ignored", "reason": "status_broadcast"})
        
#         if data_content.get("id", {}).get("fromMe", False) or data_content.get("fromMe", False):
#              return JSONResponse(content={"status": "ignored", "reason": "from_me"})

#         # ==================================================================
#         # 4. CONTENT INTEGRITY CHECK (Stop Empty Spam)
#         # ==================================================================
#         msg_body = data_content.get("body", "")
#         has_media = message.dataType == "media" or data_content.get("mimetype") or message.data.get("mimetype")

#         # If it has NO text content AND NO media, it is a Ghost Event (e.g. typing status, battery sync)
#         if not str(msg_body).strip() and not has_media:
#             logger.warning(f"👻 Ghost Message Detected (Empty Body + No Media). Rejection triggered.")
#             return JSONResponse(content={"status": "ignored", "reason": "zero_information_payload"})

#         # ==================================================================
#         # 5. DEDUPLICATION
#         # ==================================================================
#         dedup_key = f"{whatsapp_id}_{message.dataType}"
#         if dedup_cache.is_duplicate(dedup_key):
#             logger.info(f"⚡ Fast Dedup: Skipping duplicate {dedup_key}")
#             return JSONResponse(content={"status": "ignored", "reason": "duplicate_fast_cache"})

#         if message.dataType != "media":
#             existing = supabase.table("messages").select("id").eq("metadata->>whatsapp_message_id", whatsapp_id).execute()
#             if existing.data:
#                 return JSONResponse(content={"status": "ignored", "reason": "duplicate_db"})

#         # ==================================================================
#         # 6. AGENT VERIFICATION
#         # ==================================================================
#         agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
#         if not agent_res.data: 
#             return JSONResponse(status_code=200, content={"status": "error", "message": "Agent not found"})
#         agent = agent_res.data[0]

#         integration_res = supabase.table("agent_integrations").select("*").eq("agent_id", agent_id).eq("channel", "whatsapp").execute()
#         if not integration_res.data or integration_res.data[0].get("enabled") is False:
#             return JSONResponse(content={"status": "ignored", "reason": "integration_disabled"})

#         # ==================================================================
#         # 7. STANDARDIZATION & ZOMBIE TIME CHECK
#         # ==================================================================
#         settings_data = await fetch_agent_settings(supabase, agent_id)
#         if settings_data: agent.update(settings_data)

#         standard_message = await _convert_unofficial_to_standard(message)
        
#         # [ZOMBIE PROTECTION]: Ignore messages older than 2 minutes
#         if standard_message.timestamp:
#             try:
#                 msg_time = datetime.fromisoformat(standard_message.timestamp)
#                 if msg_time.tzinfo is None: msg_time = msg_time.replace(tzinfo=timezone.utc)
                
#                 age_seconds = (datetime.now(timezone.utc) - msg_time).total_seconds()
#                 if age_seconds > 120:
#                     logger.warning(f"⏳ Ignoring Old Message (Age: {int(age_seconds)}s). Zombie History Sync Protection.")
#                     return JSONResponse(content={"status": "ignored", "reason": "too_old"})
#             except Exception: pass

#         # ==================================================================
#         # 8. LID RESOLUTION & ROUTING
#         # ==================================================================
#         # Resolve LID to Real Number
#         contact_id = await resolve_lid_to_real_number(standard_message.phone_number, agent_id, "whatsapp", supabase)
        
#         # Fix Name Display
#         sender_name = standard_message.sender_name
#         if sender_name and ("@lid" in sender_name or not sender_name.strip()):
#              sender_name = contact_id # Fallback to number if name is ugly/empty
        
#         # Clean ID
#         if "@c.us" in contact_id: contact_id = contact_id.split("@")[0]

#         result = await process_webhook_message(
#             agent=agent,
#             channel="whatsapp",
#             contact=contact_id,
#             message_content=standard_message.message,
#             customer_name=sender_name or contact_id,
#             message_metadata={
#                 "whatsapp_message_id": standard_message.message_id,
#                 "message_type": standard_message.message_type,
#                 "timestamp": standard_message.timestamp,
#                 "is_lid": "@lid" in standard_message.phone_number,
#                 "media_url": standard_message.media_url,
#                 **standard_message.metadata
#             },
#             customer_metadata={"whatsapp_name": sender_name},
#             supabase=supabase
#         )

#         return WebhookRouteResponse(
#             success=True, chat_id=result["chat_id"], message_id=result["message_id"],
#             customer_id=result["customer_id"], is_new_chat=result["is_new_chat"],
#             was_reopened=result["was_reopened"], handled_by=result["handled_by"],
#             status=result["status"], channel="whatsapp", message=_generate_status_message(result)
#         )

#     except Exception as e:
#         logger.error(f"❌ Unofficial Webhook Critical Error: {e}")
#         return JSONResponse(status_code=200, content={"success": False, "error": str(e)})
            
# ============================================
# TELEGRAM WEBHOOK
# ============================================

@router.post("/telegram", response_model=WebhookRouteResponse)
async def telegram_webhook(message: TelegramWebhookMessage, secret: str = Depends(get_webhook_secret)):
    try:
        supabase = get_supabase_client()
        if await check_telegram_idempotency(supabase, str(message.message_id)):
            return JSONResponse(content={"success": True, "status": "ignored_duplicate"})

        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_telegram_bot(message.bot_token, message.bot_username)
        if not agent: raise HTTPException(404, "Agent not found")
        
        settings_data = await fetch_agent_settings(supabase, agent["id"])
        if settings_data: agent.update(settings_data)

        customer_name = f"@{message.username}" if message.username else message.first_name
        msg_meta = {"telegram_message_id": message.message_id, "telegram_chat_id": message.chat_id, "timestamp": message.timestamp, **message.metadata}
        cust_meta = {"telegram_username": message.username, "telegram_first_name": message.first_name, "phone": str(message.telegram_id)}

        result = await process_webhook_message(agent, "telegram", str(message.telegram_id), message.message, customer_name, msg_meta, cust_meta, supabase)
        
        return WebhookRouteResponse(
            success=True, chat_id=result.get("chat_id"), message_id=result.get("message_id"), 
            customer_id=result.get("customer_id"), is_new_chat=result.get("is_new_chat", False),
            was_reopened=result.get("was_reopened", False),
            handled_by=result.get("handled_by", "unknown"), status=result.get("status", "processed"), 
            channel="telegram", message=_generate_status_message(result)
        )
    except Exception as e:
        logger.error(f"Telegram Error: {e}")
        raise HTTPException(500, str(e))

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
        result = await process_webhook_message(
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
    logger.info(f"🧪 [1. EXTRACTOR] Input: '{whatsapp_id}'") # LOG THIS
    
    if not whatsapp_id:
        return ""
    
    if "@lid" in whatsapp_id:
        # This is where the @lid bypasses the cleaning logic
        logger.info(f"🧪 [1. EXTRACTOR] LID Detected. Bypassing clean. Result: '{whatsapp_id}'") 
        return whatsapp_id

    clean_id = whatsapp_id.split("@")[0].split(":")[0]
    logger.info(f"🧪 [1. EXTRACTOR] Standard Clean Result: '{clean_id}'")
    return clean_id

async def _convert_unofficial_to_standard(unofficial: WhatsAppUnofficialWebhookMessage) -> WhatsAppWebhookMessage:
    """
    Unified converter that correctly routes data sources for Text vs Media.
    """
    raw_payload = unofficial.data
    
    # 1. EXTRACT IDENTITY
    wrapper = raw_payload.get("message", {}) or raw_payload.get("messageMedia", {})
    identity_source = wrapper.get("_data", {}) or wrapper
    
    phone = _extract_phone_number(identity_source.get("from", ""))
    to_num = _extract_phone_number(identity_source.get("to", ""))
    sender = identity_source.get("notifyName") or phone
    msg_id = identity_source.get("id", {}).get("id") if isinstance(identity_source.get("id"), dict) else None
    ts = datetime.fromtimestamp(identity_source.get("t")).isoformat() if identity_source.get("t") else None
    
    # 2. EXTRACT CONTENT
    # [FIX] For Media, we need raw_payload. For Text, we prefer the inner identity_source.
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
        phone_number=phone, to_number=to_num, sender_name=sender,
        message=text, message_id=msg_id, message_type=type_str,
        media_url=url, timestamp=ts, metadata={"session_id": unofficial.sessionId}
    )

async def _handle_message_content_for_telegram(message_data: dict, data_type: str) -> Tuple[str, str, Optional[str]]:
    """Safe Content Handler for Telegram"""
    content = ""
    msg_type = "text"
    media_url = None
    
    if data_type == "media":
        base64_data = message_data.get("data") or message_data.get("body")
        if not base64_data: raise ValueError("Media data missing")

        mime = message_data.get("mimetype", "application/octet-stream")
        content = message_data.get("caption", "")

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

# app/api/webhook.py
# Reworking
# @router.post("/telegram-userbot", response_model=WebhookRouteResponse)
# async def telegram_userbot_webhook(
#     payload: WhatsAppUnofficialWebhookMessage, 
#     secret: str = Depends(get_webhook_secret)
# ):
#     try:
#         agent_id = payload.sessionId
#         raw_data = payload.data
        
#         if payload.dataType == "media":
#             logger.info(f"📸 Telegram Userbot: Received Media (Agent: {agent_id})")
#             content_source = raw_data
#             data_wrapper = raw_data.get("message", {}) or {}
#             data_content = data_wrapper.get("_data", {}) or data_wrapper
#         else:
#             logger.info(f"💬 Telegram Userbot: Received Text (Agent: {agent_id})")
#             data_wrapper = raw_data.get("message", {}) or {}
#             data_content = data_wrapper.get("_data", {}) or data_wrapper
#             content_source = raw_data

#         if not data_content and payload.dataType != "media": 
#             raise HTTPException(status_code=400, detail="Invalid JSON structure")
        
#         # Idempotency
#         msg_id = data_content.get("id", {}).get("id")
#         supabase = get_supabase_client()
#         if msg_id and await check_telegram_idempotency(supabase, str(msg_id)):
#              return JSONResponse(content={"success": True, "status": "ignored_duplicate"})

#         # Identity
#         sender_id = str(data_content.get("from", ""))
#         sender_display_name = data_content.get("notifyName") or f"User {sender_id}"
#         timestamp_unix = data_content.get("t")
#         raw_phone = data_content.get("phone")
#         final_phone = sender_id
#         if raw_phone and str(raw_phone).lower() not in ["none", "null", ""]:
#             final_phone = str(raw_phone)

#         # Agent & Settings
#         agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
#         if not agent_res.data: raise HTTPException(404, "Agent not found")
#         agent = agent_res.data[0]
#         settings_data = await fetch_agent_settings(supabase, agent_id)
#         if settings_data: agent.update(settings_data)

#         # Content Extraction
#         try:
#             message_text, msg_type, media_url = await _handle_message_content_for_telegram(content_source, payload.dataType)
#         except Exception as e:
#             logger.error(f"❌ Telegram Content Error: {e}")
#             message_text = ""
#             msg_type = "text"
#             media_url = None

#         msg_meta = {
#             "source_format": "wa_unofficial_json", 
#             "telegram_message_id": msg_id, 
#             "telegram_sender_id": sender_id, 
#             "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None,
#             "media_url": media_url,
#             "message_type": msg_type 
#         }
        
#         cust_meta = { "telegram_id": sender_id, "phone": final_phone, "source": "telegram_userbot" }

#         result = await process_webhook_message(agent, "telegram", sender_id, message_text, sender_display_name, msg_meta, cust_meta, supabase)
        
#         return WebhookRouteResponse(
#             success=True, chat_id=result.get("chat_id"), message_id=result.get("message_id"), 
#             customer_id=result.get("customer_id"), is_new_chat=result.get("is_new_chat", False),
#             was_reopened=result.get("was_reopened", False), handled_by=result.get("handled_by", "ai"), 
#             status=result.get("status", "open"), channel="telegram", message=_generate_status_message(result)
#         )
        
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"❌ Telegram Userbot Critical: {e}")
#         raise HTTPException(status_code=500, detail=str(e))




async def process_webhook_message_v2(
    agent: Dict, channel: str, contact: str, message_content: str,
    customer_name: Optional[str], message_metadata: Dict, customer_metadata: Dict, supabase
) -> Dict:
    """
    V2 Processor: Uses Local Proxy V2 for AI Responses.
    Strictly follows legacy logic for Routing, Phone Update, WS Broadcast, and Checks.
    """
    agent_id = agent["id"]
    org_id = agent["organization_id"]

    if agent.get("status") == "inactive":
        return {"success": False, "status": "dropped_inactive"}

    # 1. ROUTING (Save to DB)
    router = get_message_router_service(supabase)
    res = await router.route_incoming_message(agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata)
    chat_id, cust_id, msg_id = res["chat_id"], res["customer_id"], res["message_id"]

    # 2. UPDATE PHONE
    if customer_metadata.get("phone") and cust_id:
        try: supabase.table("customers").update({"phone": customer_metadata["phone"]}).eq("id", cust_id).execute()
        except: pass

    # 3. BROADCAST TO FRONTEND
    if app_settings.WEBSOCKET_ENABLED:
        try:
            attachment_data = None
            if message_metadata.get("media_url"):
                attachment_data = {
                    "url": message_metadata["media_url"],
                    "type": message_metadata.get("message_type", "image"),
                    "name": "Media Attachment"
                }

            await get_connection_manager().broadcast_new_message(
                organization_id=org_id, chat_id=chat_id, message_id=msg_id,
                customer_id=cust_id, customer_name=customer_name or "Unknown",
                message_content=message_content, channel=channel, handled_by=res["handled_by"],
                sender_type="customer", sender_id=cust_id, is_new_chat=res["is_new_chat"],
                was_reopened=res.get("was_reopened", False), metadata=message_metadata,
                attachment=attachment_data
            )
        except Exception as e: logger.error(f"❌ WS Broadcast Failed: {e}")

    if res.get("is_merged_event"): return res

    # 4. STOP IF HANDLED BY HUMAN
    if res.get("handled_by") == "human":
        logger.info(f"🛑 Chat {chat_id} is handled by Human. AI V2 stopped.")
        return res

    # 5. BUSY CHECK
    if agent.get("status") == "busy":
        msg = "Maaf, saat ini kami sedang sibuk."
        contact_info = {"phone": contact, "telegram_id": contact}
        await send_auto_reply(supabase, channel, agent_id, contact_info, msg)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel, agent_name=agent["name"])
        return {**res, "handled_by": "system_busy"}

    # 6. SCHEDULE CHECK
    schedule = await get_agent_schedule_config(agent_id, supabase)
    is_within, _ = is_within_schedule(schedule, datetime.now(ZoneInfo("UTC")))
    if not is_within:
        msg = "Maaf kami sedang tutup saat ini."
        # Update metadata to flag out of schedule
        try: supabase.table("messages").update({"metadata": {**message_metadata, "out_of_schedule": True}}).eq("id", msg_id).execute()
        except: pass
        
        chat_data = {"id": chat_id, "channel": channel, "sender_agent_id": agent_id}
        cust_data = {"phone": contact, "metadata": {"telegram_id": contact}}
        await send_message_via_channel(chat_data, cust_data, msg, supabase)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel)
        return {**res, "handled_by": "ooo_system"}

    # 7. TRIGGER AI V2 (LOCAL PROXY)
    # [CRITICAL CHANGE] This calls your new V2 Manager
    logger.info(f"⚡ Triggering AI V2 (Local Proxy) for Chat {chat_id}")
    asyncio.create_task(process_dynamic_ai_response_v2(chat_id, msg_id, supabase))
    
    return {**res, "handled_by": "ai_v2"}

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
            logger.info(f"📡 System Event ({message.dataType}) - Ignored")
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

        # Dedup
        dedup_key = f"{whatsapp_id}_{message.dataType}"
        if dedup_cache.is_duplicate(dedup_key):
            logger.info(f"⚡ Fast Dedup: Skipping duplicate {dedup_key}")
            return JSONResponse(content={"status": "ignored", "reason": "duplicate_fast_cache"})

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

        standard_message = await _convert_unofficial_to_standard(message)
        
        # Zombie Check
        if standard_message.timestamp:
            try:
                msg_time = datetime.fromisoformat(standard_message.timestamp)
                if msg_time.tzinfo is None: msg_time = msg_time.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - msg_time).total_seconds()
                if age_seconds > 120:
                    logger.warning(f"⏳ Ignoring Old Message (Age: {int(age_seconds)}s). Zombie History Sync Protection.")
                    return JSONResponse(content={"status": "ignored", "reason": "too_old"})
            except Exception: pass

        # Resolve LID
        contact_id = await resolve_lid_to_real_number(standard_message.phone_number, agent_id, "whatsapp", supabase)
        sender_name = standard_message.sender_name
        if sender_name and ("@lid" in sender_name or not sender_name.strip()):
             sender_name = contact_id
        if "@c.us" in contact_id: contact_id = contact_id.split("@")[0]

        # [CHANGE] Use V2 Processor
        result = await process_webhook_message_v2(
            agent=agent,
            channel="whatsapp",
            contact=contact_id,
            message_content=standard_message.message,
            customer_name=sender_name or contact_id,
            message_metadata={
                "whatsapp_message_id": standard_message.message_id,
                "message_type": standard_message.message_type,
                "timestamp": standard_message.timestamp,
                "is_lid": "@lid" in standard_message.phone_number,
                "media_url": standard_message.media_url,
                **standard_message.metadata
            },
            customer_metadata={"whatsapp_name": sender_name},
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
        
        if payload.dataType == "media":
            logger.info(f"📸 Telegram Userbot: Received Media (Agent: {agent_id})")
            content_source = raw_data
            data_wrapper = raw_data.get("message", {}) or {}
            data_content = data_wrapper.get("_data", {}) or data_wrapper
        else:
            logger.info(f"💬 Telegram Userbot: Received Text (Agent: {agent_id})")
            data_wrapper = raw_data.get("message", {}) or {}
            data_content = data_wrapper.get("_data", {}) or data_wrapper
            content_source = raw_data

        if not data_content and payload.dataType != "media": 
            raise HTTPException(status_code=400, detail="Invalid JSON structure")
        
        # Idempotency
        msg_id = data_content.get("id", {}).get("id")
        supabase = get_supabase_client()
        if msg_id and await check_telegram_idempotency(supabase, str(msg_id)):
             return JSONResponse(content={"success": True, "status": "ignored_duplicate"})

        # Identity
        sender_id = str(data_content.get("from", ""))
        sender_display_name = data_content.get("notifyName") or f"User {sender_id}"
        timestamp_unix = data_content.get("t")
        raw_phone = data_content.get("phone")
        final_phone = sender_id
        if raw_phone and str(raw_phone).lower() not in ["none", "null", ""]:
            final_phone = str(raw_phone)

        # Agent & Settings
        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        if not agent_res.data: raise HTTPException(404, "Agent not found")
        agent = agent_res.data[0]
        
        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        # Content Extraction
        try:
            message_text, msg_type, media_url = await _handle_message_content_for_telegram(content_source, payload.dataType)
        except Exception as e:
            logger.error(f"❌ Telegram Content Error: {e}")
            message_text = ""
            msg_type = "text"
            media_url = None

        msg_meta = {
            "source_format": "wa_unofficial_json", 
            "telegram_message_id": msg_id, 
            "telegram_sender_id": sender_id, 
            "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None,
            "media_url": media_url,
            "message_type": msg_type 
        }
        
        cust_meta = { "telegram_id": sender_id, "phone": final_phone, "source": "telegram_userbot" }

        # [CHANGE] Use V2 Processor
        result = await process_webhook_message_v2(
            agent=agent, 
            channel="telegram", 
            contact=sender_id, 
            message_content=message_text, 
            customer_name=sender_display_name, 
            message_metadata=msg_meta, 
            customer_metadata=cust_meta, 
            supabase=supabase
        )
        
        return WebhookRouteResponse(
            success=True, chat_id=result.get("chat_id"), message_id=result.get("message_id"), 
            customer_id=result.get("customer_id"), is_new_chat=result.get("is_new_chat", False),
            was_reopened=result.get("was_reopened", False), handled_by=result.get("handled_by", "ai_v2"), 
            status=result.get("status", "open"), channel="telegram", message=_generate_status_message(result)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Telegram Userbot Critical: {e}")
        raise HTTPException(status_code=500, detail=str(e))






