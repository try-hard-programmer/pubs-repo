"""
Ticket Service
Manages ticket creation, updates, and numbering logic
"""
import json
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

logger = logging.getLogger(__name__)

class TicketService:
    def __init__(self):
        self.supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

    # =========================================================
    # 1. GENERATE NUMBER (Reads Prefix from Config)
    # =========================================================
    async def _generate_ticket_number(self, organization_id: str, ticket_config: Dict = None) -> str:
        """
        Generate ticket number using the parsed ticket config.
        """
        prefix = "TKT-" # Default
        
        # [FIX] No more JSON parsing here. We expect a clean Dictionary.
        if ticket_config and isinstance(ticket_config, dict):
            custom_prefix = ticket_config.get("ticketPrefix")
            if custom_prefix:
                prefix = custom_prefix.strip()
        
        logger.info(f"🔢 Generating Ticket with Prefix: '{prefix}'") # Debug Log

        try:
            res = self.supabase.table("tickets") \
                .select("ticket_number") \
                .eq("organization_id", organization_id) \
                .ilike("ticket_number", f"{prefix}%") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()

            next_num = 1
            if res.data:
                last_ticket = res.data[0]["ticket_number"]
                try:
                    number_part = last_ticket.replace(prefix, "")
                    next_num = int(number_part) + 1
                except ValueError:
                    next_num = 1
            
            return f"{prefix}{next_num:03d}"

        except Exception as e:
            logger.error(f"Failed to generate ticket number: {e}")
            return f"{prefix}{int(datetime.now(timezone.utc).timestamp())}"
    
    # =========================================================
    # 2. CREATE TICKET (Strict Schema Match)
    # =========================================================
    async def create_ticket(
        self, 
        data: TicketCreate, 
        organization_id: str, 
        ticket_config: Dict = None, # [CHANGED] Receive parsed config directly
        actor_id: Optional[str] = None,
        actor_type: ActorType = ActorType.SYSTEM
    ) -> Ticket:
        
        # 1. Generate Number using the passed config
        ticket_num = await self._generate_ticket_number(organization_id, ticket_config)
        logger.info(f"🎫 Creating Ticket {ticket_num} for Chat {data.chat_id}")

        # 2. Prepare Insert Data
        insert_data = {
            "organization_id": organization_id,
            "customer_id": data.customer_id,
            "chat_id": data.chat_id,
            "ticket_number": ticket_num,
            "title": data.title,
            "description": data.description,
            "category": data.category,
            "priority": data.priority.value if hasattr(data.priority, "value") else data.priority,
            "status": TicketStatus.OPEN.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # 3. Insert
        res = self.supabase.table("tickets").insert(insert_data).execute()
        if not res.data: 
            raise Exception("Failed to insert ticket")
        
        new_ticket = Ticket(**res.data[0])

        # 4. Log Activity
        await self.log_activity(
            ticket_id=new_ticket.id, 
            action="created", 
            description=f"Ticket created by {actor_type.value}", 
            actor_id=actor_id, 
            actor_type=actor_type
        )

        # 5. Broadcast
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
    
    # =========================================================
    # 3. HELPERS
    # =========================================================
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

    async def update_ticket(self, ticket_id: str, update_data: TicketUpdate, actor_id: str, actor_type: ActorType = ActorType.HUMAN) -> Ticket:
        old_res = self.supabase.table("tickets").select("*").eq("id", ticket_id).single().execute()
        if not old_res.data: raise Exception("Ticket not found")
        old_ticket = old_res.data

        payload = update_data.model_dump(exclude_unset=True)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        if payload.get("status") == TicketStatus.RESOLVED:
            payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
        elif payload.get("status") == TicketStatus.CLOSED:
            payload["closed_at"] = datetime.now(timezone.utc).isoformat()

        res = self.supabase.table("tickets").update(payload).eq("id", ticket_id).execute()
        if not res.data: raise Exception("Failed to update ticket")
        updated_ticket = res.data[0]

        if payload.get("status") and payload["status"] != old_ticket["status"]:
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