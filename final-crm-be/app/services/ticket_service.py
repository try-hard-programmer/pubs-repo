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
    # 2. CREATE TICKET (Strict Schema Match + Title Fix)
    # =========================================================
    async def create_ticket(
        self, 
        data: TicketCreate, 
        organization_id: str, 
        ticket_config: Dict = None, 
        actor_id: Optional[str] = None,
        actor_type: ActorType = ActorType.SYSTEM
    ) -> Ticket:
        
        # 1. Generate Number
        ticket_num = await self._generate_ticket_number(organization_id, ticket_config)
        logger.info(f"🎫 Creating Ticket {ticket_num} for Chat {data.chat_id}")

        # [FIX] Resolve Customer Name for Title Generation
        # Even if FE sends "UNKNOWN", we fix it here.
        customer_name = "Unknown Customer"
        if data.customer_id:
            try:
                cust_res = self.supabase.table("customers").select("name").eq("id", data.customer_id).single().execute()
                if cust_res.data:
                    customer_name = cust_res.data.get("name") or "Unknown Customer"
            except Exception as e:
                logger.warning(f"Failed to resolve customer name: {e}")

        # [FIX] Smart Title Generation
        # If title is missing OR explicitly looks like a placeholder, regenerate it.
        final_title = data.title
        
        is_placeholder = final_title and ("UNKNOWN" in final_title or "New Ticket" in final_title)
        
        if not final_title or is_placeholder:
            priority_val = data.priority.value if hasattr(data.priority, "value") else str(data.priority)
            priority_str = priority_val.upper()
            
            # Create snippet from description
            desc_text = data.description or "No Content"
            snippet = desc_text[:30] + "..." if len(desc_text) > 30 else desc_text
            
            # Format: [LOW] John Doe - Issue Description...
            final_title = f"[{priority_str}] {customer_name} - {snippet}"
            logger.info(f"✨ Auto-generated Title: {final_title}")

        # 2. Prepare Insert Data
        insert_data = {
            "organization_id": organization_id,
            "customer_id": data.customer_id,
            "chat_id": data.chat_id,
            "ticket_number": ticket_num,
            "title": final_title,  # Use the fixed title
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
        
        # --- ROBUST STATUS EXTRACTION ---
        # 1. Try to get from dumped payload
        new_status_raw = payload.get("status")
        
        # 2. Fallback: Try to get directly from the input object if missing in payload
        if new_status_raw is None and getattr(update_data, 'status', None) is not None:
             new_status_raw = update_data.status
             # Ensure it's in the payload for the DB update
             payload["status"] = new_status_raw

        # 3. Normalize to string
        new_status_str = ""
        if new_status_raw is not None:
            val = new_status_raw.value if hasattr(new_status_raw, "value") else new_status_raw
            new_status_str = str(val).lower().strip()

        # [DEBUG] Confirm we detected the status
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
        # [AUTO-RELEASE LOGIC]
        # Trigger: Status is "closed" or "resolved" -> Release Chat
        # ==============================================================================
        
        if new_status_str in ["closed", "resolved"] and new_status_str != old_status_str:
            try:
                chat_id = old_ticket.get("chat_id")
                if chat_id:
                    # A. Fetch Chat
                    chat_res = self.supabase.table("chats").select("*").eq("id", chat_id).single().execute()
                    
                    if chat_res.data:
                        chat = chat_res.data
                        
                        # Only release if currently handled by human
                        if chat.get("handled_by") == "human" or chat.get("human_agent_id"):
                            logger.info(f"🔄 Ticket Closed: Releasing Chat {chat_id} from Human...")
                            
                            ai_agent_id = chat.get("ai_agent_id")
                            
                            # RESET CHAT STATE
                            chat_update = {
                                "status": "open",               # Force Open
                                "human_agent_id": None,         # Remove Human
                                "handled_by": "unassigned",     # Default
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            }
                            
                            # Assign back to AI if possible
                            if ai_agent_id:
                                chat_update["handled_by"] = "ai"
                                chat_update["assigned_agent_id"] = ai_agent_id
                                logger.info(f"🤖 Chat assigned back to AI: {ai_agent_id}")
                            else:
                                chat_update["assigned_agent_id"] = None
                                logger.info(f"⚠️ Chat Unassigned (No AI Agent)")

                            self.supabase.table("chats").update(chat_update).eq("id", chat_id).execute()
                        else:
                            logger.info(f"ℹ️ Chat {chat_id} already released/not human handled.")
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