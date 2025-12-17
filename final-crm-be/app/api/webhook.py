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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

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

async def save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, content, channel, metadata=None):
    """Explicitly saves system/AI messages to DB and updates Frontend."""
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
                customer_id=None, customer_name="AI Agent",
                message_content=content, channel=channel, handled_by="ai",
                sender_type="ai", sender_id=agent_id
            )
    except Exception as e:
        logger.error(f"Failed to save/broadcast system message: {e}")

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

    # 1. INACTIVE CHECK
    if agent.get("status") == "inactive":
        return {"success": False, "status": "dropped_inactive"}

    # 2. ROUTING
    router = get_message_router_service(supabase)
    res = await router.route_incoming_message(agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata)
    chat_id, cust_id, msg_id = res["chat_id"], res["customer_id"], res["message_id"]

    # 2a. Update Phone
    if customer_metadata.get("phone") and cust_id:
        try: supabase.table("customers").update({"phone": customer_metadata["phone"]}).eq("id", cust_id).execute()
        except: pass

    # 2b. Broadcast Incoming
    if app_settings.WEBSOCKET_ENABLED:
        try:
            await get_connection_manager().broadcast_new_message(
                organization_id=org_id, chat_id=chat_id, message_id=msg_id,
                customer_id=cust_id, customer_name=customer_name or "Unknown",
                message_content=message_content, channel=channel, handled_by=res["handled_by"],
                sender_type="customer", sender_id=cust_id, is_new_chat=res["is_new_chat"],
                was_reopened=res.get("was_reopened", False)
            )
        except: pass

    # 3. BUSY CHECK
    if agent.get("status") == "busy":
        msg = "Maaf, saat ini kami sedang sibuk."
        contact_info = {"phone": contact} if channel == "whatsapp" else {"telegram_id": contact}
        await send_auto_reply(supabase, channel, agent_id, contact_info, msg)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel)
        return {**res, "handled_by": "system_busy"}

    # 4. ACTIVE TICKET CHECK
    active_ticket = supabase.table("tickets").select("id").eq("customer_id", cust_id).in_("status", ["open", "in_progress"]).limit(1).execute()
    if active_ticket.data:
        logger.info(f"🎫 Active Ticket Found.")
        return {**res, "handled_by": "human_ticket"}

    # 5. SCHEDULE CHECK
    schedule = await get_agent_schedule_config(agent_id, supabase)
    is_within, _ = is_within_schedule(schedule, datetime.now(ZoneInfo("UTC")))
    if not is_within:
        msg = "Maaf kami sedang tutup saat ini."
        supabase.table("messages").update({"metadata": {**message_metadata, "out_of_schedule": True}}).eq("id", msg_id).execute()
        contact_info = {"phone": contact} if channel == "whatsapp" else {"telegram_id": contact}
        await send_message_via_channel({"id": chat_id, "channel": channel, "sender_agent_id": agent_id}, contact_info, msg, supabase)
        await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, msg, channel)
        return {**res, "handled_by": "ooo_system"}

    # 6. INTELLIGENCE (Guard)
    ticket_config = parse_agent_config(agent.get("ticketing_config"))
    should_trigger_ai = True

    if ticket_config.get("enabled"):
        should_ticket, pred_cat, prio_str, conf, reason, ticket_title = ml_guard.predict(message_content)
        
        # [LOGIC A] LOW PRIORITY -> GREETING (Stop AI)
        if prio_str == "low":
            logger.info(f"🛡️ Low Priority ({reason}). Sending Greeting & Stopping AI.")
            greeting_msg = (
                f"Halo {customer_name or 'Kak'}! 👋\n\n"
                "Pesan Anda telah kami terima melalui platform Syntra.\n"
                "Silakan jelaskan permasalahan yang Anda alami secara lebih rinci agar kami dapat membantu Anda dengan lebih baik."
            )
            chat_data = {"id": chat_id, "channel": channel, "sender_agent_id": agent_id}
            cust_data = {"phone": contact} if channel == "whatsapp" else {"telegram_id": contact}
            await send_message_via_channel(chat_data, cust_data, greeting_msg, supabase)
            await save_and_broadcast_system_message(supabase, chat_id, agent_id, org_id, greeting_msg, channel)
            
            should_trigger_ai = False
            return {**res, "handled_by": "system_greeting"}

        # [LOGIC B] HIGH/MEDIUM -> CREATE TICKET ONLY (Allow AI to reply)
        elif should_ticket and ticket_config.get("autoCreateTicket"):
            final_cat = pred_cat
            if ticket_config.get("categories") and pred_cat not in ticket_config["categories"]:
                final_cat = ticket_config["categories"][0]

            logger.info(f"🤖 Creating Ticket: {final_cat} [{prio_str}]")
            try: final_prio = TicketPriority(prio_str)
            except: final_prio = TicketPriority.MEDIUM

            ticket_svc = get_ticket_service()
            ticket_data = TicketCreate(
                chat_id=chat_id, customer_id=cust_id,
                title=ticket_title, description=f"Message: {message_content}",
                priority=final_prio, category=final_cat
            )
            
            # Create Ticket (Background)
            await ticket_svc.create_ticket(ticket_data, org_id, ticket_config, None, ActorType.SYSTEM)
            
            # [FIX] REMOVED THE SYSTEM REPLY HERE
            # The Ticket is created, but we say NOTHING.
            # We let 'should_trigger_ai' remain True so the AI picks up the conversation naturally.

    # 7. AI RESPONSE
    if should_trigger_ai:
        logger.info(f"🤖 Triggering AI Response for Chat {chat_id}")
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
        # 1. LOGIC: Filter Unwanted Events
        # The unofficial API sends 'loading_screen', 'qr', 'authenticated', etc.
        # We only care about 'message' or 'message_create' that are actual chats.
        if message.dataType not in ["message", "message_create", "media"]:
             return JSONResponse(content={"status": "ignored", "reason": f"event_type_{message.dataType}"})

        # Extract inner data safely
        data_content = message.data.get("message", {}).get("_data", {})
        if not data_content:
             # Try fallback for media
             data_content = message.data.get("messageMedia", {})
        
        if not data_content:
             return JSONResponse(content={"status": "ignored", "reason": "empty_data"})

        # 2. LOGIC: Ignore Self-Replies (Infinite Loop Prevention)
        # 'id' is usually a dict { fromMe: boolean, ... }
        msg_id_obj = data_content.get("id", {})
        is_from_me = msg_id_obj.get("fromMe", False)
        
        if is_from_me:
            logger.info("♻️ Ignoring message from self (fromMe=True)")
            return JSONResponse(content={"status": "ignored", "reason": "from_me"})

        # 3. LOGIC: Idempotency (Prevent Double Processing)
        # The container might send both 'message' and 'message_create' for the same text.
        # We check if this message ID already exists in our DB.
        whatsapp_id = msg_id_obj.get("id")
        
        if whatsapp_id:
            supabase = get_supabase_client()
            # Check if we already stored this message
            existing = supabase.table("messages") \
                .select("id") \
                .eq("metadata->>whatsapp_message_id", whatsapp_id) \
                .execute()
            
            if existing.data:
                logger.info(f"♻️ Duplicate message ID {whatsapp_id}. Already processed.")
                return JSONResponse(content={"status": "ignored", "reason": "duplicate_message"})

        logger.info(
            f"📱 WhatsApp unofficial webhook received: "
            f"dataType={message.dataType}, sessionId={message.sessionId}"
        )

        # 4. Convert & Process
        standard_message = await _convert_unofficial_to_standard(message)

        # STEP 1: Find agent by WhatsApp integration
        supabase = get_supabase_client()
        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_whatsapp_number(
            phone_number=standard_message.to_number
        )

        if not agent:
            # Fallback check
            logger.warning(f"Agent not found for {standard_message.to_number}, trying raw...")
            # Sometimes the format differs slightly, try to match broadly if needed
            
            logger.error(f"❌ No agent integration found for WhatsApp number: {standard_message.to_number}")
            # Return 200 to stop the container from retrying 
            return JSONResponse(
                status_code=200, 
                content={"status": "error", "message": "Agent not found"}
            )

        organization_id = agent["organization_id"]
        
        # Normalize phone number
        phone_number = standard_message.phone_number.lstrip("+")
        customer_name = standard_message.sender_name or phone_number

        # Metadata
        message_metadata = {
            "whatsapp_message_id": standard_message.message_id,
            "message_type": standard_message.message_type,
            "media_url": standard_message.media_url,
            "caption": standard_message.caption,
            "timestamp": standard_message.timestamp,
            **standard_message.metadata
        }

        customer_metadata = {
            "whatsapp_name": customer_name
        }

        # STEP 2: Process webhook message
        result = await process_webhook_message(
            agent=agent,
            channel="whatsapp",
            contact=phone_number,
            message_content=standard_message.message,
            customer_name=customer_name,
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
        logger.error(f"❌ Error processing WhatsApp unofficial webhook: {e}")
        # Always return 200 OK to the webhook sender to prevent retry loops on logic errors
        return JSONResponse(status_code=200, content={"success": False, "error": str(e)})

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

@router.post("/telegram-userbot", response_model=WebhookRouteResponse)
async def telegram_userbot_webhook(payload: WhatsAppUnofficialWebhookMessage, secret: str = Depends(get_webhook_secret)):
    try:
        agent_id = payload.sessionId
        data_wrapper = payload.data.get("message", {}) or {}
        data_content = data_wrapper.get("_data", {})
        if not data_content: raise HTTPException(400, "Invalid JSON structure")
        
        msg_id = data_content.get("id", {}).get("id")
        supabase = get_supabase_client()
        if await check_telegram_idempotency(supabase, str(msg_id)):
             return JSONResponse(content={"success": True, "status": "ignored_duplicate"})

        sender_id = data_content.get("from")
        message_text = data_content.get("body", "")
        sender_display_name = data_content.get("notifyName", f"User {sender_id}")
        timestamp_unix = data_content.get("t")
        
        raw_phone = data_content.get("phone")
        final_phone = str(sender_id)
        if raw_phone and str(raw_phone).lower() not in ["none", "null", ""]:
            final_phone = str(raw_phone)

        logger.info(f"🤖 Userbot Message: agent={agent_id} sender={sender_id} phone={final_phone}")

        agent_res = supabase.table("agents").select("*").eq("id", agent_id).execute()
        if not agent_res.data: raise HTTPException(404, "Agent not found")
        agent = agent_res.data[0]

        settings_data = await fetch_agent_settings(supabase, agent_id)
        if settings_data: agent.update(settings_data)

        msg_meta = {"source_format": "wa_unofficial_json", "telegram_message_id": msg_id, "telegram_sender_id": sender_id, "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None}
        cust_meta = {"telegram_id": sender_id, "phone": final_phone}

        result = await process_webhook_message(agent, "telegram", str(sender_id), message_text, sender_display_name, msg_meta, cust_meta, supabase)
        
        return WebhookRouteResponse(
            success=True, chat_id=result["chat_id"], message_id=result["message_id"], 
            customer_id=result["customer_id"], is_new_chat=result["is_new_chat"], 
            was_reopened=result.get("was_reopened", False),
            handled_by=result["handled_by"], status=result["status"], 
            channel="telegram", message=_generate_status_message(result)
        )
    except Exception as e:
        logger.error(f"Userbot Error: {e}")
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
    """
    Extract phone number from WhatsApp ID format

    Args:
        whatsapp_id: WhatsApp ID (e.g., "6289505130799@c.us")

    Returns:
        Phone number without @c.us suffix
    """
    return whatsapp_id.split("@")[0] if "@" in whatsapp_id else whatsapp_id

async def _convert_unofficial_to_standard(
        
    unofficial_message: WhatsAppUnofficialWebhookMessage
) -> WhatsAppWebhookMessage:
    """
    Convert WhatsApp unofficial payload to standard WhatsAppWebhookMessage format

    Args:
        unofficial_message: WhatsApp unofficial webhook message

    Returns:
        Standard WhatsAppWebhookMessage

    Raises:
        HTTPException: If message type is not supported or conversion fails
    """
    try:
        data_type = unofficial_message.dataType
        data = unofficial_message.data

        # Text message
        if data_type == "message":
            message_obj = data.get("message", {})
            message_data = message_obj.get("_data", {})

            # Validate it's a chat message
            if message_data.get("type") != "chat":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsupported message type: {message_data.get('type')}"
                )

            # Extract data
            phone_number = _extract_phone_number(message_data.get("from", ""))
            to_number = _extract_phone_number(message_data.get("to", ""))
            message_text = message_data.get("body", "")
            # Fallback: notifyName → phone_number (jika tidak ada nama, gunakan nomor HP)
            sender_name = message_data.get("notifyName") or phone_number
            message_id = message_data.get("id", {}).get("id")
            timestamp = message_data.get("t")

            # Convert timestamp to ISO format
            timestamp_iso = None
            if timestamp:
                timestamp_iso = datetime.fromtimestamp(timestamp).isoformat()

            return WhatsAppWebhookMessage(
                phone_number=phone_number,
                to_number=to_number,
                sender_name=sender_name,
                message=message_text,
                message_id=message_id,
                message_type="text",
                timestamp=timestamp_iso,
                metadata={"session_id": unofficial_message.sessionId}
            )

        # Media message (image or voice)
        elif data_type == "media":
            message_media = data.get("messageMedia", {})
            message_obj = data.get("message", {})
            message_data = message_obj.get("_data", {}) if message_obj else message_media

            # Determine media type
            media_mime_type = message_media.get("mimetype", "")
            media_type_str = message_media.get("type", message_data.get("type", ""))

            # Validate media type
            if media_type_str == "image":
                allowed = True
                file_extension = "jpg"
                media_format = "IMG"
            elif media_type_str == "ptt":
                allowed = True
                file_extension = "ogg"
                media_format = "PTT"
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsupported media type: {media_type_str}"
                )

            # Extract data
            phone_number = _extract_phone_number(message_data.get("from", ""))
            to_number = _extract_phone_number(message_data.get("to", ""))

            # Extract sender name - check multiple locations based on payload structure
            # For image: notifyName is in messageMedia
            # For voice/ptt: notifyName is in message._data
            # Fallback: notifyName → phone_number (jika tidak ada nama, gunakan nomor HP)
            sender_name = (
                message_media.get("notifyName") or
                message_data.get("notifyName") or
                phone_number
            )

            message_id = message_data.get("id", {}).get("id") if isinstance(message_data.get("id"), dict) else None
            timestamp = message_data.get("t") or message_media.get("t")
            caption = message_media.get("caption", "")
            media_data_base64 = message_media.get("data", "")

            # Upload media to Supabase
            if not media_data_base64:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Media data is missing"
                )

            media_url = await _upload_media_to_supabase(
                media_data_base64,
                media_mime_type,
                file_extension
            )

            # Format message: "{caption}\n\n{IMG/PTT}:{url}"
            if caption:
                message_text = f"{caption}\n\n{media_format}:{media_url}"
            else:
                message_text = f"{media_format}:{media_url}"

            # Convert timestamp to ISO format
            timestamp_iso = None
            if timestamp:
                timestamp_iso = datetime.fromtimestamp(timestamp).isoformat()

            return WhatsAppWebhookMessage(
                phone_number=phone_number,
                to_number=to_number,
                sender_name=sender_name,
                message=message_text,
                message_id=message_id,
                message_type=media_type_str,
                media_url=media_url,
                caption=caption,
                timestamp=timestamp_iso,
                metadata={"session_id": unofficial_message.sessionId}
            )

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported dataType: {data_type}"
            )

    except HTTPException:
        raise
    except Exception as e:

        logger.error(f"Failed to convert unofficial message to standard format: {e}")
        raise HTTPException(

            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Message conversion failed: {str(e)}"
        )
    
