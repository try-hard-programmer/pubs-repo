"""
Ticket Service
Manages ticket creation, updates, and numbering logic
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import uuid

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

    def _generate_ticket_number(self, organization_id: str, ticket_config: Dict = None) -> str:
        """
        Generates a unique ticket ID based on Time + Randomness.
        Format: TKT-YYMMDDHHMMSS-RAND (e.g., TKT-240118120001-A1B2)
        """
        prefix = "TKT-"
        if ticket_config and isinstance(ticket_config, dict):
            prefix = ticket_config.get("ticketPrefix", "TKT-").strip()
            
        # 1. Get compact timestamp (Year, Month, Day, Hour, Minute, Second)
        timestamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
        
        # 2. Add entropy (4 random hex characters)
        # This guarantees uniqueness without needing a database lock
        random_part = uuid.uuid4().hex[:4].upper()
        
        return f"{prefix}{timestamp}-{random_part}"
    
    async def create_ticket(
        self, 
        data: TicketCreate, 
        organization_id: str, 
        ticket_config: Dict = None, 
        actor_id: Optional[str] = None,
        actor_type: ActorType = ActorType.SYSTEM
    ) -> Ticket:
        
        ticket_num = self._generate_ticket_number(ticket_config)
        
        logger.info(f"🎫 Creating Ticket {ticket_num} for Chat {data.chat_id}")

        # [FIX] Resolve Customer Name (This part was good, keep it)
        customer_name = "Unknown Customer"
        if data.customer_id:
            try:
                cust_res = self.supabase.table("customers").select("name").eq("id", data.customer_id).single().execute()
                if cust_res.data:
                    customer_name = cust_res.data.get("name") or "Unknown Customer"
            except Exception as e:
                logger.warning(f"Failed to resolve customer name: {e}")

        # [FIX] Smart Title Generation (This was also good)
        final_title = data.title
        is_placeholder = final_title and ("UNKNOWN" in final_title or "New Ticket" in final_title)
        
        if not final_title or is_placeholder:
            priority_val = data.priority.value if hasattr(data.priority, "value") else str(data.priority)
            desc_text = data.description or "No Content"
            snippet = desc_text[:30] + "..." if len(desc_text) > 30 else desc_text
            final_title = f"[{priority_val.upper()}] {customer_name} - {snippet}"

        # 3. Prepare Insert Data
        insert_data = {
            "organization_id": organization_id,
            "customer_id": data.customer_id,
            "chat_id": data.chat_id,
            "ticket_number": ticket_num,
            "title": final_title,
            "description": data.description,
            "category": data.category,
            "priority": data.priority.value if hasattr(data.priority, "value") else data.priority,
            "status": TicketStatus.OPEN.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # 4. Insert (One shot, no indentation level)
        res = self.supabase.table("tickets").insert(insert_data).execute()
        
        # Everything below here is correct...
        if not res.data: 
            raise Exception("Failed to insert ticket")
        
        new_ticket = Ticket(**res.data[0])

        # 5. Log Activity
        await self.log_activity(
            ticket_id=new_ticket.id, 
            action="created", 
            description=f"Ticket created by {actor_type.value}", 
            actor_id=actor_id, 
            actor_type=actor_type
        )

        # 6. Broadcast
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
            clean_status = payload['status'].value if hasattr(payload['status'], 'value') else str(payload['status'])
            log_desc = f"Status changed to {clean_status.upper()} by {actor_id}"
            await self.log_activity(ticket_id, "status_change", log_desc, actor_id, actor_type)
        
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