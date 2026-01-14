"""
Ticket Service
Manages ticket creation, updates, and numbering logic
"""
import asyncio
import random
import string
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from supabase import create_client
from app.config import settings
from app.models.ticket import (
    Ticket, TicketCreate, TicketUpdate, 
    TicketActivityResponse, ActorType,
    TicketStatus
)
from app.services.websocket_service import get_connection_manager
from app.services.redis_service import acquire_lock
from fastapi import HTTPException

logger = logging.getLogger(__name__)

class TicketService:
    def __init__(self):
        self.supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

    def _generate_parallel_ticket_number(self, prefix: str = "TPADI") -> str:
        """
        Generates a collision-safe ID.
        Format: TPADI-2601141108X9A (YYMMDDHHMM-XXX)
        Length: Prefix(5) + Date(10) + Suffix(3) = 18 chars
        """
        # 1. Get Time (YearMonthDayHourMinute) -> 2601141108
        now = datetime.now()
        time_str = now.strftime("%y%m%d%H%M") 
        
        # 2. Generate 3 Random Chars (A-Z, 0-9) -> X9A
        # 36^3 = 46,656 combinations per minute.
        chars = string.ascii_uppercase + string.digits
        rand_suffix = ''.join(random.choices(chars, k=3))
        
        return f"{prefix}-{time_str}{rand_suffix}"
    

    async def create_ticket(self, data: TicketCreate, organization_id: str, ticket_config: dict, actor_id: str = None, actor_type: str = "system"):
        """
        Creates a ticket using parallel-safe ID generation.
        Retries automatically if a collision occurs.
        """
        MAX_RETRIES = 3
        final_ticket = None
        last_error = None
        
        # 1. Get Prefix from config or default
        prefix = ticket_config.get("ticket_prefix", "TPADI")

        # 2. Attempt Loop (Safety Airbag)
        for attempt in range(MAX_RETRIES):
            try:
                # Generate ID locally (No DB call needed)
                candidate_number = self._generate_parallel_ticket_number(prefix)
                
                ticket_data = {
                    "ticket_number": candidate_number,
                    "organization_id": organization_id,
                    "customer_id": data.customer_id,
                    "chat_id": data.chat_id,
                    "title": data.title,
                    "description": data.description,
                    "priority": data.priority,
                    "category": data.category,
                    "status": "open",
                    "created_by": actor_id,
                    "created_by_type": actor_type
                }
                
                # Insert directly. If ID exists, DB throws "unique constraint" error.
                response = self.supabase.table("tickets").insert(ticket_data).execute()
                
                if response.data:
                    final_ticket = response.data[0]
                    logger.info(f"✅ Ticket Created: {candidate_number}")
                    break # Success! Exit loop.
                    
            except Exception as e:
                error_str = str(e).lower()
                # Check for Unique Violation (Postgres error codes usually involve 'duplicate key')
                if "duplicate key" in error_str or "unique constraint" in error_str:
                    logger.warning(f"⚠️ Ticket ID Collision ({candidate_number}). Retrying ({attempt+1}/{MAX_RETRIES})...")
                    continue # Try again with new random suffix
                else:
                    # Real error (Connection, Auth, etc.) -> Crash
                    last_error = e
                    break
        
        if not final_ticket:
            logger.error(f"❌ Failed to generate ticket after {MAX_RETRIES} attempts. Error: {last_error}")
            raise HTTPException(status_code=500, detail="Failed to generate unique ticket ID. Please retry.")

        return final_ticket
    
    async def log_activity(self, ticket_id: str, action: str, description: str, actor_id: Optional[str], actor_type: ActorType, metadata: dict = {}):
        try:
            data = {
                "ticket_id": ticket_id,
                "action": action,
                "description": description,
                "actor_type": actor_type.value,
                "human_actor_id": actor_id if actor_type == ActorType.HUMAN else None,
                "ai_actor_id": actor_id if actor_type == ActorType.AI else None,
                "metadata": metadata,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            self.supabase.table("ticket_activities").insert(data).execute()
        except Exception as e:
            logger.error(f"Failed to log activity: {e}")

    async def update_ticket(
        self, 
        ticket_id: str, 
        update_data: TicketUpdate, 
        actor_id: str, 
        actor_type: ActorType = ActorType.HUMAN
    ) -> Ticket:
        # [DEBUG] Force log to confirm function entry and data
        logger.info(f"🟢 [TicketService] Update Request for {ticket_id}")
        logger.info(f"📦 [TicketService] Incoming Data: {update_data}")

        # 1. Fetch Old Ticket
        old_res = self.supabase.table("tickets").select("*").eq("id", ticket_id).single().execute()
        if not old_res.data: 
            raise Exception("Ticket not found")
        old_ticket = old_res.data
        old_status_str = str(old_ticket.get("status", "")).lower().strip()

        # 2. Prepare Payload
        payload = update_data.model_dump(exclude_unset=True)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        if "priority" in payload:
            new_p = str(payload["priority"]).upper()
            old_p = str(old_ticket.get("priority", "LOW")).upper()
            
            # If we are trying to set LOW, but it wasn't LOW before...
            if new_p == "LOW" and old_p != "LOW":
                logger.warning(f"🛑 Priority Downgrade Blocked: {old_p} -> {new_p}")
                del payload["priority"] # Remove priority from update, keeping other changes
                # Alternatively: raise Exception("Cannot downgrade priority to LOW")
                
        # --- ROBUST STATUS EXTRACTION ---
        new_status_raw = payload.get("status")
        if new_status_raw is None and getattr(update_data, 'status', None) is not None:
             new_status_raw = update_data.status
             payload["status"] = new_status_raw

        new_status_str = ""
        if new_status_raw is not None:
            val = new_status_raw.value if hasattr(new_status_raw, "value") else new_status_raw
            new_status_str = str(val).lower().strip()

        logger.info(f"🧐 [TicketService] Status Check: Old='{old_status_str}' New='{new_status_str}'")

        # Handle Timestamps
        if new_status_str == "resolved":
            payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
        elif new_status_str == "closed":
            payload["closed_at"] = datetime.now(timezone.utc).isoformat()

        # 3. Update the Ticket in DB
        res = self.supabase.table("tickets").update(payload).eq("id", ticket_id).execute()
        if not res.data: 
            raise Exception("Failed to update ticket")
        updated_ticket = res.data[0]

        # ==============================================================================
        # [AUTO-RELEASE LOGIC] - THIS IS CRITICAL
        # ==============================================================================
        if new_status_str in ["closed", "resolved"] and new_status_str != old_status_str:
            try:
                chat_id = old_ticket.get("chat_id")
                if chat_id:
                    chat_res = self.supabase.table("chats").select("*").eq("id", chat_id).single().execute()
                    
                    if chat_res.data:
                        chat = chat_res.data
                        if chat.get("handled_by") == "human" or chat.get("human_agent_id"):
                            logger.info(f"🔄 Ticket Closed: Releasing Chat {chat_id} from Human...")
                            
                            ai_agent_id = chat.get("ai_agent_id")
                            
                            chat_update = {
                                "status": "open",
                                "human_agent_id": None,
                                "handled_by": "unassigned",
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            }
                            
                            if ai_agent_id:
                                chat_update["handled_by"] = "ai"
                                chat_update["assigned_agent_id"] = ai_agent_id
                                logger.info(f"🤖 Chat assigned back to AI: {ai_agent_id}")
                            else:
                                chat_update["assigned_agent_id"] = None
                                logger.info(f"⚠️ Chat Unassigned (No AI Agent)")

                            self.supabase.table("chats").update(chat_update).eq("id", chat_id).execute()
            except Exception as e:
                logger.error(f"❌ Failed to auto-release chat: {e}")

        # 4. Activity Logging
        if payload.get("status") and str(payload["status"]) != str(old_ticket["status"]):
            await self.log_activity(ticket_id, "status_change", f"Status changed to {payload['status']}", actor_id, actor_type)
        
        if payload.get("priority") and payload["priority"] != old_ticket["priority"]:
            await self.log_activity(ticket_id, "priority_change", f"Priority changed to {payload['priority']}", actor_id, actor_type)

        if "assigned_agent_id" in payload and payload["assigned_agent_id"] != old_ticket["assigned_agent_id"]:
             new_agent = payload["assigned_agent_id"] or "Unassigned"
             await self.log_activity(ticket_id, "assignment_change", f"Assigned to {new_agent}", actor_id, actor_type)

        return Ticket(**updated_ticket)
    
    async def get_ticket_history(self, ticket_id: str) -> List[TicketActivityResponse]:
        res = self.supabase.table("ticket_activities").select("*").eq("ticket_id", ticket_id).order("created_at", desc=True).execute()
        resolved_logs = []
        for log in res.data:
            name = "System"
            if log["actor_type"] == "human": name = "Human Agent"
            elif log["actor_type"] == "ai": name = "AI Agent"
            resolved_logs.append({**log, "actor_name": name})
        return [TicketActivityResponse(**l) for l in resolved_logs]

# Singleton
_ticket_service = TicketService()
def get_ticket_service(): return _ticket_service