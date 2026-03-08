import json
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from supabase import create_client

from app.config import settings
from app.services.ticket_service import get_ticket_service
from app.models.ticket import TicketUpdate, TicketStatus, ActorType
from app.services.whatsapp_service import get_whatsapp_service 
from app.services.telegram_service import get_telegram_service 
from app.services.websocket_service import get_connection_manager
from app.services.webhook_callback_service import get_webhook_callback_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])

webhook_api_key_header = APIKeyHeader(name="X-Webhook-Secret", auto_error=True)

def verify_internal_secret(api_key: str = Security(webhook_api_key_header)):
    if api_key != settings.WEBHOOK_SECRET_KEY:
        logger.warning("Unauthorized attempt to hit internal job endpoint.")
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Secret")
    return api_key


@router.post("/auto-close-tickets")
async def auto_close_stale_tickets(api_key: str = Depends(verify_internal_secret)):
    """Cron job endpoint to follow-up on inactive tickets, and close them if they ignore the follow-up."""

    logger.info("Auto-close/Follow-up stale tickets job started")

    supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    ticket_service = get_ticket_service()
    wa_service = get_whatsapp_service() 
    # [REMOVED] tg_service initialization

    threshold_time = datetime.now(timezone.utc) - timedelta(minutes=15)

    closed_count = 0
    followed_up_count = 0
    followed_up_count = 0
    errors = []

    # 1. Grab candidates
    res = supabase.table("tickets") \
        .select("id, chat_id, customer_id, status, ticket_number, metadata, organization_id") \
        .in_("status", ["open", "in_progress", "resolved"]) \
        .execute()

    candidates = res.data or []

    if not candidates:
        return {"status": "success", "message": "No active candidates", "closed": 0, "followed_up": 0}

    tickets_to_process = []

    # 2. Verify against messages
    for ticket in candidates:
        chat_id = ticket.get("chat_id")
        if not chat_id: continue

        msg_res = supabase.table("messages") \
            .select("created_at") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if msg_res.data:
            last_msg_str = msg_res.data[0]["created_at"]
            if last_msg_str.endswith("+00"): last_msg_str = last_msg_str.replace("+00", "+00:00")
            elif last_msg_str.endswith("Z"): last_msg_str = last_msg_str.replace("Z", "+00:00")

            last_msg_time = datetime.fromisoformat(last_msg_str)

            if last_msg_time < threshold_time:
                tickets_to_process.append(ticket)
        else:
            tickets_to_process.append(ticket)

    # 3. Execute State Machine Logic
    for t in tickets_to_process:
        try:
            # --- PARSE METADATA ---
            meta = t.get("metadata", {})
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            
            is_group = meta.get("is_group", False)
            is_following_up = meta.get("following_up", False)

            # ==========================================
            # STAGE 2: THE EXECUTION (TIMEOUT REACHED)
            # ==========================================
            if is_following_up:
                # [RESTORED] Explicit check to protect group chats from instant closure
                follow_up_at_str = meta.get("follow_up_at")
                if follow_up_at_str:
                    try:
                        fu_time_str = follow_up_at_str.replace("Z", "+00:00")
                        if fu_time_str.endswith("+00"): fu_time_str = fu_time_str.replace("+00", "+00:00")
                        follow_up_at = datetime.fromisoformat(fu_time_str)
                        
                        if follow_up_at > threshold_time:
                            logger.info(f"⏳ Ticket {t['ticket_number']} is still in its grace period. Waiting.")
                            continue
                    except Exception as time_err:
                        logger.warning(f"⚠️ Could not parse follow_up_at for ticket {t['ticket_number']}: {time_err}")

                # 15 minutes have passed since the warning. Shut it down.
                await ticket_service.update_ticket(
                    ticket_id=t["id"],
                    update_data=TicketUpdate(status=TicketStatus.CLOSED),
                    actor_id="SYSTEM_CRON", 
                    actor_type=ActorType.SYSTEM
                )
                closed_count += 1
                logger.info(f"🔒 Closed ticket {t['ticket_number']} after follow-up timeout.")
                continue

            # ==========================================
            # STAGE 1: THE WARNING
            # ==========================================
            should_send_dm = not is_group and t.get("chat_id") and t.get("customer_id")

            if should_send_dm:
                try:
                    chat_res = supabase.table("chats").select("channel, sender_agent_id, handled_by, assigned_agent_id, ai_agent_id").eq("id", t["chat_id"]).single().execute()
                    cust_res = supabase.table("customers").select("name, phone, metadata").eq("id", t["customer_id"]).single().execute()
                    
                    if chat_res.data and cust_res.data:
                        channel = chat_res.data.get("channel")
                        session_id = chat_res.data.get("sender_agent_id")
                        handled_by = chat_res.data.get("handled_by", "ai")
                        
                        if handled_by == "human":
                            msg_sender_type = "agent"
                            msg_sender_id = chat_res.data.get("assigned_agent_id") or session_id
                        else:
                            msg_sender_type = "ai"
                            msg_sender_id = chat_res.data.get("ai_agent_id") or session_id

                        phone = cust_res.data.get("phone")
                        if not phone and cust_res.data.get("metadata"):
                            phone = cust_res.data["metadata"].get("whatsapp_lid") or cust_res.data["metadata"].get("telegram_id")

                        raw_name = cust_res.data.get("name")
                        customer_name = raw_name.strip() if raw_name and raw_name.strip() else "Customer"
                        if customer_name.lower() in ["new customer", "unknown customer", "null", "none"]:
                            customer_name = "Customer"

                        if session_id and phone and msg_sender_id:
                            dm_text = f"Dear {customer_name}, apabila tidak ada problem lagi maka kami akan menutup sesi chat ini dengan nomer ticket {t['ticket_number']} pada waktu 15 menit kedepan."
                            message_sent = False
                            
                            if channel == "whatsapp":
                                await wa_service.send_text_message(session_id=session_id, phone_number=phone, message=dm_text)
                                message_sent = True
                            elif channel == "telegram":
                                from app.services.webhook_callback_service import get_webhook_callback_service
                                webhook_service = get_webhook_callback_service()
                                mock_chat = {
                                    "id": t["chat_id"],
                                    "customer_id": t["customer_id"],
                                    "sender_agent_id": session_id,
                                    "ai_agent_id": None
                                }
                                tg_res = await webhook_service.send_telegram_callback(
                                    chat=mock_chat,
                                    message_content=dm_text,
                                    supabase=supabase
                                )
                                if tg_res.get("success"):
                                    message_sent = True
                                else:
                                    logger.error(f"Telegram sending failed: {tg_res.get('error')}")
                                
                            if message_sent:
                                logger.info(f"📤 Sent {channel.upper()} Follow-Up to {phone} for ticket {t['ticket_number']}")
                                
                                msg_meta_payload = json.dumps({"type": "follow_up_dm", "ticket_number": t["ticket_number"]})
                                
                                msg_insert = supabase.table("messages").insert({
                                    "chat_id": t["chat_id"],
                                    "sender_type": msg_sender_type,
                                    "sender_id": msg_sender_id,
                                    "content": dm_text,
                                    "metadata": msg_meta_payload 
                                }).execute()

                                if msg_insert.data and getattr(settings, "WEBSOCKET_ENABLED", True):
                                    created_msg = msg_insert.data[0]
                                    try:
                                        from app.services.websocket_service import get_connection_manager
                                        conn = get_connection_manager()
                                        await conn.broadcast_new_message(
                                            organization_id=t.get("organization_id"),
                                            chat_id=t["chat_id"],
                                            message_id=created_msg["id"],
                                            customer_id=t["customer_id"],
                                            customer_name="System", 
                                            message_content=created_msg.get("content", ""),
                                            channel=channel,
                                            handled_by=handled_by,
                                            sender_type=msg_sender_type,
                                            sender_id=msg_sender_id,
                                            created_at=created_msg.get("created_at"),
                                            metadata=created_msg.get("metadata")
                                        )
                                        logger.info(f"📡 Broadcasted system message to WS for chat {t['chat_id']}")
                                    except Exception as ws_e:
                                        logger.warning(f"⚠️ WS Broadcast failed for cron job: {ws_e}")

                except Exception as dm_err:
                    logger.error(f"⚠️ Failed to send Follow-Up DM for ticket {t['ticket_number']}: {dm_err}")

            # 7. Update Ticket State 
            meta["following_up"] = True
            meta["follow_up_at"] = datetime.now(timezone.utc).isoformat()
            
            ticket_meta_payload = json.dumps(meta) if isinstance(t.get("metadata"), str) else meta
            
            supabase.table("tickets").update({"metadata": ticket_meta_payload}).eq("id", t["id"]).execute()
            followed_up_count += 1
                
        except Exception as e:
            logger.error(f"Failed to process ticket {t['ticket_number']}: {e}")
            errors.append({"ticket": t["ticket_number"], "error": str(e)})

    logger.info(f"Job completed. Closed: {closed_count}, Warned: {followed_up_count}, Errors: {len(errors)}")

    return {
        "status": "success",
        "message": f"Closed {closed_count} tickets, Warned {followed_up_count} tickets",
        "closed_count": closed_count,
        "warned_count": followed_up_count,
        "errors": errors
    }
