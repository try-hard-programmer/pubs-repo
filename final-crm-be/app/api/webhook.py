"""
Webhook API Endpoints
Receive incoming messages from external services (WhatsApp, Telegram, Email)
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import JSONResponse
import logging
import asyncio
import base64
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

from app.models.webhook import (
    WhatsAppWebhookMessage,
    WhatsAppUnofficialWebhookMessage,
    TelegramWebhookMessage,
    EmailWebhookMessage,
    WebhookRouteResponse,
    WebhookErrorResponse
)
from app.middleware.webhook_auth import get_webhook_secret
from app.services.message_router_service import get_message_router_service
from app.services.agent_finder_service import get_agent_finder_service
from app.services.websocket_service import get_connection_manager
from app.services.ai_response_service import process_ai_response_async
from app.config import settings as app_settings
from app.models.webhook import WhatsAppUnofficialWebhookMessage, WebhookRouteResponse
from app.models.ticket import TicketCreate, ActorType, TicketPriority, TicketDecision
from app.services.ticket_service import get_ticket_service
from app.api.crm_chats import send_message_via_channel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ============================================
# HELPER FUNCTIONS
# ============================================

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
        # 1. Check for existing active ticket
        active_tickets = supabase.table("tickets") \
            .select("id") \
            .eq("chat_id", chat_id) \
            .in_("status", ["open", "in_progress"]) \
            .execute()
        
        if active_tickets.data:
            return # Ticket exists

        logger.info(f"🎫 Creating Auto-Ticket for {chat_id}. Priority: {decision.suggested_priority}")

        # 2. Use the TicketService to create (handles DB + Logging)
        ticket_service = get_ticket_service()
        
        new_ticket_data = TicketCreate(
            chat_id=chat_id,
            customer_id=customer_id,
            title=f"Telegram: {customer_name}",
            description=f"Message: {message_content}\n\n[Auto-created: {decision.reason}]",
            priority=decision.suggested_priority or TicketPriority.MEDIUM, 
            category=decision.suggested_category or "inquiry"
        )

        await ticket_service.create_ticket(
            data=new_ticket_data,
            organization_id=organization_id,
            actor_id=None,
            actor_type=ActorType.SYSTEM
        )

        logger.info(f"✅ Auto-Ticket created successfully.")

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


# ... existing imports ...

# ============================================
# MAIN PROCESSOR (FIXED)
# ============================================

async def process_webhook_message(
    agent: Dict,
    channel: str,
    contact: str,
    message_content: str,
    customer_name: Optional[str],
    message_metadata: Optional[Dict[str, Any]],
    customer_metadata: Optional[Dict[str, Any]],
    supabase
) -> Dict[str, Any]:
    
    # 1. Busy Check & 2. Schedule Check (Keep existing code) ...
    from app.utils.schedule_validator import get_agent_schedule_config, is_within_schedule
    organization_id = agent["organization_id"]
    agent_is_busy = is_agent_busy(agent)
    if agent_is_busy: await send_busy_agent_auto_reply(agent, channel, contact, supabase)
    
    from zoneinfo import ZoneInfo
    schedule_config = await get_agent_schedule_config(agent["id"], supabase)
    current_time_utc = datetime.now(ZoneInfo("UTC"))
    is_within, out_of_schedule_reason = is_within_schedule(schedule_config, current_time_utc)

    # 3. Route Message (Creates Chat & Message)
    router_service = get_message_router_service(supabase)
    result = await router_service.route_incoming_message(
        agent=agent,
        channel=channel,
        contact=contact,
        message_content=message_content,
        customer_name=customer_name,
        message_metadata=message_metadata,
        customer_metadata=customer_metadata
    )

    # [Customer Phone Update Logic - Keep existing]
    if customer_metadata and customer_metadata.get("phone"):
        try:
            phone_num = customer_metadata.get("phone")
            cust_id = result.get("customer_id")
            if cust_id and phone_num:
                supabase.table("customers").update({"phone": phone_num}).eq("id", cust_id).execute()
        except: pass

    # ============================================================
    # STEP 4: TICKET GUARD & LOGIC
    # ============================================================
    ticket_service = get_ticket_service()
    
    decision = await ticket_service.evaluate_incoming_message(
        message=message_content, 
        customer_name=customer_name or "Customer"
    )

    should_trigger_ai = True 

    # --- A. HANDLE AUTO-REPLY (Hi / Greetings) ---
    if decision.auto_reply_hint:
        logger.info(f"🤖 Sending Guard Auto-Reply: {decision.auto_reply_hint}")
        
        # [FIX IS HERE] Added 'customer_id' so the sender knows who to reply to!
        chat_data = {
            "id": result["chat_id"], 
            "channel": channel, 
            "sender_agent_id": agent["id"],
            "customer_id": result["customer_id"], # <--- CRITICAL FIX
            "ai_agent_id": agent["id"] # Helpful context
        }
        
        # For WhatsApp/Email we pass contact, for Telegram the sender looks up via customer_id
        cust_data = {"phone": contact, "email": contact if "@" in contact else None} 
        
        await send_message_via_channel(
            chat_data=chat_data,
            customer_data=cust_data,
            message_content=decision.auto_reply_hint,
            supabase=supabase
        )
        
        # Save Reply to DB History
        supabase.table("messages").insert({
            "chat_id": result["chat_id"],
            "sender_type": "ai", 
            "sender_id": agent["id"],
            "content": decision.auto_reply_hint,
            "metadata": {"type": "auto_reply_guard"}
        }).execute()

        should_trigger_ai = False 

    # --- B. HANDLE TICKET CREATION ---
    if channel == "telegram" and result["success"] and decision.should_create_ticket:
        asyncio.create_task(
            process_auto_ticket_async(
                chat_id=result["chat_id"],
                customer_id=result["customer_id"],
                organization_id=organization_id,
                customer_name=customer_name or "Telegram User",
                message_content=message_content,
                decision=decision,
                supabase=supabase
            )
        )

    # Step 5: Handle out-of-schedule
    if not is_within:
        await flag_message_as_out_of_schedule(result["message_id"], out_of_schedule_reason, supabase)
        await send_out_of_schedule_message(agent, channel, contact, supabase)
        should_trigger_ai = False

    # Step 6: WebSocket
    if app_settings.WEBSOCKET_ENABLED:
        try:
            connection_manager = get_connection_manager()
            await connection_manager.broadcast_new_message(
                organization_id=organization_id,
                chat_id=result["chat_id"],
                message_id=result["message_id"],
                customer_id=result["customer_id"],
                customer_name=customer_name or "Unknown",
                message_content=message_content,
                channel=channel,
                handled_by=result["handled_by"],
                sender_type="customer", 
                sender_id=result["customer_id"],
                is_new_chat=result["is_new_chat"],
                was_reopened=result["was_reopened"]
            )
        except Exception as e:
            logger.warning(f"Failed to send WebSocket notification: {e}")

    # Step 7: AI Agent
    if should_trigger_ai and not agent_is_busy and is_within and result["handled_by"] == "ai":
        asyncio.create_task(
            process_ai_response_async(
                chat_id=result["chat_id"],
                customer_message_id=result["message_id"],
                supabase=supabase
            )
        )

    return result


def get_supabase_client():
    """Get Supabase client from settings"""
    from supabase import create_client

    if not app_settings.is_supabase_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase is not configured"
        )

    return create_client(app_settings.SUPABASE_URL, app_settings.SUPABASE_SERVICE_KEY)


def _generate_status_message(result: dict) -> str:
    if result["is_new_chat"]: return "New chat created"
    elif result["was_reopened"]: return "Chat reopened"
    else: return "Message added"

# ============================================
# WHATSAPP WEBHOOK
# ============================================

@router.post(
    "/whatsapp",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive WhatsApp message",
    description="Webhook endpoint to receive incoming messages from WhatsApp service",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "No agent integration found"},
        500: {"description": "Internal server error"}
    }
)
async def whatsapp_webhook(
    message: WhatsAppWebhookMessage,
    secret: str = Depends(get_webhook_secret)
):
    """
    Receive incoming WhatsApp message and route to correct chat.

    **Authentication:** Requires `X-API-Key` header with valid secret key.

    **Flow:**
    1. Validate webhook secret
    2. Find agent by WhatsApp destination number (integration lookup)
    3. Find or create customer by phone number
    4. Find active chat or create new chat
    5. Add message to chat (assign to agent)
    6. Update customer metadata
    7. Send WebSocket notification (if enabled)
    8. Return routing result

    **Request Example:**
    ```json
    {
        "phone_number": "+6281234567890",
        "to_number": "+6281111111",
        "sender_name": "John Doe",
        "message": "Hello, I need help",
        "message_id": "wamid.xxx123",
        "timestamp": "2025-10-21T15:30:00Z"
    }
    ```

    **Response Example:**
    ```json
    {
        "success": true,
        "chat_id": "chat-uuid-456",
        "message_id": "msg-uuid-789",
        "customer_id": "customer-uuid-abc",
        "is_new_chat": false,
        "was_reopened": true,
        "handled_by": "ai",
        "status": "open",
        "channel": "whatsapp",
        "message": "Message routed successfully"
    }
    ```

    Args:
        message: WhatsApp webhook message
        secret: Validated webhook secret (injected by dependency)

    Returns:
        WebhookRouteResponse with routing details

    Raises:
        HTTPException: If routing fails or no agent integration found
    """
    try:
        logger.info(
            f"📱 WhatsApp webhook received: "
            f"from={message.phone_number}, to={message.to_number}"
        )

        # Get Supabase client
        supabase = get_supabase_client()

        # STEP 1: Find agent by WhatsApp integration
        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_whatsapp_number(
            phone_number=message.to_number
        )

        if not agent:
            logger.error(f"❌ No agent integration found for WhatsApp number: {message.to_number}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent integration found for WhatsApp number {message.to_number}"
            )

        organization_id = agent["organization_id"]
        logger.info(
            f"✅ Agent found: {agent['name']} (org={organization_id}, "
            f"is_ai={agent['user_id'] is None}, status={agent.get('status')})"
        )

        # Normalize phone number (remove + if present, ensure starts with country code)
        phone_number = message.phone_number.lstrip("+")

        # Prepare message metadata
        message_metadata = {
            "whatsapp_message_id": message.message_id,
            "message_type": message.message_type,
            "media_url": message.media_url,
            "caption": message.caption,
            "timestamp": message.timestamp,
            **message.metadata
        }

        # Prepare customer metadata
        customer_metadata = {
            "whatsapp_name": message.sender_name
        }

        # STEP 2: Process webhook message (unified logic)
        result = await process_webhook_message(
            agent=agent,
            channel="whatsapp",
            contact=phone_number,
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
            f"✅ WhatsApp message routed: "
            f"chat={result['chat_id']}, is_new={result['is_new_chat']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing WhatsApp webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process WhatsApp message: {str(e)}"
        )


# ============================================
# WHATSAPP UNOFFICIAL WEBHOOK
# ============================================

@router.post(
    "/wa-unofficial",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive WhatsApp message (Unofficial API)",
    description="Webhook endpoint to receive incoming messages from WhatsApp unofficial service (whatsapp-web.js)",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "No agent integration found"},
        500: {"description": "Internal server error"}
    }
)
async def whatsapp_unofficial_webhook(
    message: WhatsAppUnofficialWebhookMessage,
    secret: str = Depends(get_webhook_secret)
):
    """
    Receive incoming WhatsApp message from unofficial API and route to correct chat.

    **Authentication:** Requires `X-API-Key` header with valid secret key.

    **Supported Message Types:**
    - Text messages: `dataType == "message" && data.message.type == "chat"`
    - Image messages: `dataType == "media" && data.dataType == "image"`
    - Voice messages: `dataType == "media" && data.dataType == "ptt"`

    **Flow:**
    1. Validate webhook secret
    2. Convert unofficial payload to standard WhatsApp format
    3. Upload media files to Supabase storage (if applicable)
    4. Find agent by WhatsApp destination number
    5. Route message to chat using existing logic
    6. Send WebSocket notification
    7. Trigger AI response if needed

    **Request Example (Text):**
    ```json
    {
        "dataType": "message",
        "data": {
            "message": {
                "_data": {
                    "id": {"fromMe": false, "remote": "6289505130799@c.us"},
                    "body": "Hello",
                    "type": "chat",
                    "from": "6289505130799@c.us",
                    "to": "62881024580401@c.us"
                }
            }
        },
        "sessionId": "ea799531-14ff-400a-9380-cd2a9c16af5c"
    }
    ```

    **Request Example (Image):**
    ```json
    {
        "dataType": "media",
        "data": {
            "messageMedia": {
                "mimetype": "image/jpeg",
                "data": "/9j/4AAQSkZJRg...",
                "type": "image",
                "caption": "Check this out"
            },
            "message": {
                "_data": {
                    "from": "6289505130799@c.us",
                    "to": "62881024580401@c.us"
                }
            }
        },
        "sessionId": "ea799531-14ff-400a-9380-cd2a9c16af5c"
    }
    ```

    Args:
        message: WhatsApp unofficial webhook message
        secret: Validated webhook secret (injected by dependency)

    Returns:
        WebhookRouteResponse with routing details

    Raises:
        HTTPException: If routing fails, message type unsupported, or no agent integration found
    """
    try:
        logger.info(
            f"📱 WhatsApp unofficial webhook received: "
            f"dataType={message.dataType}, sessionId={message.sessionId}"
        )

        # Convert unofficial payload to standard format
        standard_message = await _convert_unofficial_to_standard(message)

        logger.info(
            f"✅ Converted unofficial message: "
            f"from={standard_message.phone_number}, to={standard_message.to_number}, "
            f"type={standard_message.message_type}"
        )

        # Get Supabase client
        supabase = get_supabase_client()

        # STEP 1: Find agent by WhatsApp integration
        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_whatsapp_number(
            phone_number=standard_message.to_number
        )

        if not agent:
            logger.error(f"❌ No agent integration found for WhatsApp number: {standard_message.to_number}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent integration found for WhatsApp number {standard_message.to_number}"
            )

        organization_id = agent["organization_id"]
        logger.info(
            f"✅ Agent found: {agent['name']} (org={organization_id}, "
            f"is_ai={agent['user_id'] is None}, status={agent.get('status')})"
        )

        # Normalize phone number (remove + if present, ensure starts with country code)
        phone_number = standard_message.phone_number.lstrip("+")

        # Ensure customer name is not None or empty - fallback to phone number
        customer_name = standard_message.sender_name or phone_number

        # Prepare message metadata
        message_metadata = {
            "whatsapp_message_id": standard_message.message_id,
            "message_type": standard_message.message_type,
            "media_url": standard_message.media_url,
            "caption": standard_message.caption,
            "timestamp": standard_message.timestamp,
            **standard_message.metadata
        }

        # Prepare customer metadata
        customer_metadata = {
            "whatsapp_name": customer_name
        }

        # STEP 2: Process webhook message (unified logic)
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
            f"✅ WhatsApp unofficial message routed: "
            f"chat={result['chat_id']}, is_new={result['is_new_chat']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing WhatsApp unofficial webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process WhatsApp unofficial message: {str(e)}"
        )


# ============================================
# TELEGRAM WEBHOOK
# ============================================

@router.post(
    "/telegram",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive Telegram message",
    description="Webhook endpoint to receive incoming messages from Telegram bot",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "No agent integration found"},
        500: {"description": "Internal server error"}
    }
)
async def telegram_webhook(
    message: TelegramWebhookMessage,
    secret: str = Depends(get_webhook_secret)
):
    """
    Receive incoming Telegram message and route to correct chat.

    **Authentication:** Requires `X-API-Key` header with valid secret key.

    **Flow:** Same as WhatsApp webhook but for Telegram.

    **Request Example:**
    ```json
    {
        "telegram_id": "123456789",
        "bot_token": "123456:ABC-DEF...",
        "bot_username": "my_support_bot",
        "username": "johndoe",
        "first_name": "John",
        "last_name": "Doe",
        "message": "Hello from Telegram",
        "message_id": 999,
        "timestamp": "2025-10-21T15:30:00Z"
    }
    ```

    Args:
        message: Telegram webhook message
        secret: Validated webhook secret (injected by dependency)

    Returns:
        WebhookRouteResponse with routing details

    Raises:
        HTTPException: If routing fails or no agent integration found
    """
    try:
        logger.info(
            f"✈️ Telegram webhook received: "
            f"telegram_id={message.telegram_id}, bot={message.bot_username or 'token'}"
        )

        # Get Supabase client
        supabase = get_supabase_client()

        # STEP 1: Find agent by Telegram integration
        agent_finder = get_agent_finder_service(supabase)
        agent = await agent_finder.find_agent_by_telegram_bot(
            bot_token=message.bot_token,
            bot_username=message.bot_username
        )

        if not agent:
            logger.error(
                f"❌ No agent integration found for Telegram bot: "
                f"{message.bot_username or message.bot_token[:20]}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent integration found for Telegram bot"
            )

        organization_id = agent["organization_id"]
        logger.info(
            f"✅ Agent found: {agent['name']} (org={organization_id}, "
            f"is_ai={agent['user_id'] is None}, status={agent.get('status')})"
        )

        # Generate customer name from Telegram data
        customer_name = None
        if message.first_name:
            customer_name = message.first_name
            if message.last_name:
                customer_name += f" {message.last_name}"
        elif message.username:
            customer_name = f"@{message.username}"

        # Prepare message metadata
        message_metadata = {
            "telegram_message_id": message.message_id,
            "telegram_chat_id": message.chat_id,
            "message_type": message.message_type,
            "photo_url": message.photo_url,
            "document_url": message.document_url,
            "timestamp": message.timestamp,
            **message.metadata
        }

        # Prepare customer metadata
        customer_metadata = {
            "telegram_username": message.username,
            "telegram_first_name": message.first_name,
            "telegram_last_name": message.last_name
        }

        # STEP 2: Process webhook message (unified logic)
        result = await process_webhook_message(
            agent=agent,
            channel="telegram",
            contact=message.telegram_id,
            message_content=message.message,
            customer_name=customer_name,
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
            f"✅ Telegram message routed: "
            f"chat={result['chat_id']}, is_new={result['is_new_chat']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing Telegram webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process Telegram message: {str(e)}"
        )


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


# ============================================
# HELPER FUNCTIONS
# ============================================

def _generate_status_message(result: dict) -> str:
    """
    Generate human-readable status message from routing result.

    Args:
        result: Routing result dictionary

    Returns:
        Status message string
    """
    if result["is_new_chat"]:
        if result["handled_by"] == "ai":
            return "New chat created and assigned to AI agent"
        elif result["handled_by"] == "human":
            return "New chat created and assigned to human agent"
        else:
            return "New chat created (unassigned)"
    elif result["was_reopened"]:
        return f"Message routed to existing chat (chat was reopened, handled by {result['handled_by']})"
    else:
        return f"Message added to active chat (handled by {result['handled_by']})"

@router.post(
    "/telegram-userbot",
    response_model=WebhookRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive Telegram Userbot message",
)
async def telegram_userbot_webhook(
    payload: WhatsAppUnofficialWebhookMessage, 
    secret: str = Depends(get_webhook_secret)
):
    try:
        agent_id = payload.sessionId
        data_content = payload.data.get("message", {}).get("_data", {})
        
        if not data_content:
             raise HTTPException(status_code=400, detail="Invalid JSON structure")

        sender_id = data_content.get("from")  
        message_text = data_content.get("body", "")
        sender_display_name = data_content.get("notifyName", f"User {sender_id}")
        message_id = data_content.get("id", {}).get("id")
        timestamp_unix = data_content.get("t")
        
        # [NEW] Extract Phone Number
        sender_phone = data_content.get("phone")

        logger.info(f"🤖 Userbot Message: agent={agent_id} sender={sender_id} phone={sender_phone}")

        supabase = get_supabase_client()
        agent_response = supabase.table("agents").select("*").eq("id", agent_id).execute()
        
        if not agent_response.data:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        agent = agent_response.data[0]

        message_metadata = {
            "source_format": "wa_unofficial_json",
            "telegram_message_id": message_id,
            "telegram_sender_id": sender_id,
            "timestamp": datetime.fromtimestamp(timestamp_unix).isoformat() if timestamp_unix else None
        }
        
        # [NEW] Add phone to customer metadata
        customer_metadata = {
            "telegram_id": sender_id,
            "phone": sender_phone # This will be picked up by process_webhook_message
        }

        result = await process_webhook_message(
            agent=agent,
            channel="telegram", 
            contact=sender_id,
            message_content=message_text,
            customer_name=sender_display_name,
            message_metadata=message_metadata,
            customer_metadata=customer_metadata, # Passed here
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

    except HTTPException: raise
    except Exception as e:
        logger.error(f"❌ Userbot Webhook Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))