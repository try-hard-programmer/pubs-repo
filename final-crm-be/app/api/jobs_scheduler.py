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
from app.services.telegram_service import get_telegram_service # <-- Telegram Import

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
    tg_service = get_telegram_service() # <-- Initialize Telegram Service

    # We sync this to 15 minutes to match the exact promise in the DM text
    threshold_time = datetime.now(timezone.utc) - timedelta(minutes=1)

    closed_count = 0
    followed_up_count = 0
    errors = []

    # 1. Grab candidates
    res = supabase.table("tickets") \
        .select("id, chat_id, customer_id, status, ticket_number, metadata") \
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
            meta = t.get("metadata", {})
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            
            is_group = meta.get("is_group", False)
            is_following_up = meta.get("following_up", False)

            # STAGE 1: THE WARNING
            if not is_following_up:
                if not is_group and t.get("chat_id") and t.get("customer_id"):
                    try:
                        chat_res = supabase.table("chats").select("channel, sender_agent_id").eq("id", t["chat_id"]).single().execute()
                        cust_res = supabase.table("customers").select("phone, metadata").eq("id", t["customer_id"]).single().execute()
                        
                        if chat_res.data and cust_res.data:
                            channel = chat_res.data.get("channel")
                            session_id = chat_res.data.get("sender_agent_id")
                            
                            # Standardized phone/contact extraction
                            phone = cust_res.data.get("phone")
                            if not phone and cust_res.data.get("metadata"):
                                phone = cust_res.data["metadata"].get("whatsapp_lid") or cust_res.data["metadata"].get("telegram_id")

                            if session_id and phone:
                                dm_text = f"Dear Customer, apabila tidak ada problem lagi maka kami akan menutup sesi chat ini dengan nomer ticket {t['ticket_number']} pada waktu 15 menit kedepan."
                                message_sent = False
                                
                                # Route based on channel
                                if channel == "whatsapp":
                                    await wa_service.send_text_message(session_id=session_id, phone_number=phone, message=dm_text)
                                    message_sent = True
                                    logger.info(f"📤 Sent WA Follow-Up to {phone} for ticket {t['ticket_number']}")
                                
                                elif channel == "telegram":
                                    # Ensure send_text_message exists in TelegramService!
                                    await tg_service.send_text_message(agent_id=session_id, chat_id=phone, message=dm_text)
                                    message_sent = True
                                    logger.info(f"📤 Sent TG Follow-Up to {phone} for ticket {t['ticket_number']}")

                                # Log to DB to reset the 15-minute timer
                                if message_sent:
                                    supabase.table("messages").insert({
                                        "chat_id": t["chat_id"],
                                        "sender_type": "system",
                                        "sender_id": "SYSTEM_CRON",
                                        "content": dm_text,
                                        "metadata": {"type": "follow_up_dm", "ticket_number": t["ticket_number"]}
                                    }).execute()

                    except Exception as dm_err:
                        logger.error(f"⚠️ Failed to send Follow-Up DM for ticket {t['ticket_number']}: {dm_err}")

                # Update metadata to mark that we sent the warning. DO NOT CLOSE YET.
                meta["following_up"] = True
                supabase.table("tickets").update({"metadata": meta}).eq("id", t["id"]).execute()
                followed_up_count += 1
                
            # STAGE 2: THE EXECUTION
            else:
                # 15 minutes have passed since the warning. They ignored it. Shut it down.
                await ticket_service.update_ticket(
                    ticket_id=t["id"],
                    update_data=TicketUpdate(status=TicketStatus.CLOSED),
                    actor_id="SYSTEM_CRON",
                    actor_type=ActorType.SYSTEM
                )
                
                closed_count += 1
                logger.info(f"🔒 Closed ticket {t['ticket_number']} after follow-up timeout.")

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