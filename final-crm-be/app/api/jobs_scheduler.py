import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from supabase import create_client

from app.config import settings
from app.services.ticket_service import get_ticket_service
from app.models.ticket import TicketUpdate, TicketStatus, ActorType

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
    """Cron job endpoint to auto-close tickets inactive for 1 hour."""

    logger.info("Auto-close stale tickets job started")

    supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    ticket_service = get_ticket_service()

    threshold_time = datetime.now(timezone.utc) - timedelta(hours=1)

    closed_count = 0
    errors = []

    # 1. Grab candidates
    res = supabase.table("tickets") \
        .select("id, chat_id, status, ticket_number") \
        .in_("status", ["open", "in_progress", "resolved"]) \
        .execute()

    candidates = res.data or []

    if not candidates:
        return {
            "status": "success",
            "message": "No active candidates",
            "closed_count": 0
        }

    tickets_to_close = []

    # 2. Verify against messages
    for ticket in candidates:
        chat_id = ticket.get("chat_id")

        if not chat_id:
            continue

        msg_res = supabase.table("messages") \
            .select("created_at") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if msg_res.data:
            last_msg_str = msg_res.data[0]["created_at"]

            if last_msg_str.endswith("+00"):
                last_msg_str = last_msg_str.replace("+00", "+00:00")
            elif last_msg_str.endswith("Z"):
                last_msg_str = last_msg_str.replace("Z", "+00:00")

            last_msg_time = datetime.fromisoformat(last_msg_str)

            if last_msg_time < threshold_time:
                tickets_to_close.append(ticket)
        else:
            tickets_to_close.append(ticket)

    # 3. Execute closure
    for t in tickets_to_close:
        try:
            await ticket_service.update_ticket(
                ticket_id=t["id"],
                update_data=TicketUpdate(status=TicketStatus.CLOSED),
                actor_id="SYSTEM_CRON",
                actor_type=ActorType.SYSTEM
            )
            closed_count += 1
        except Exception as e:
            logger.error(
                f"Failed to close ticket {t['ticket_number']}: {e}"
            )
            errors.append({
                "ticket": t["ticket_number"],
                "error": str(e)
            })

    logger.info(
        f"Auto-close job completed. Closed: {closed_count}, Errors: {len(errors)}"
    )

    return {
        "status": "success",
        "message": f"Closed {closed_count} tickets",
        "closed_count": closed_count,
        "errors": errors
    }