import json
import logging
import os
import re
from datetime import datetime, timezone  # Updated import
from typing import Optional, List, Dict, Any

from supabase import create_client

from app.config import settings
from app.models.ticket import (
    Ticket, TicketCreate, TicketUpdate, 
    TicketActivityResponse, ActorType,
    TicketDecision, TicketPriority, TicketStatus
)
from app.agents.agent_registry import AgentRegistry
from app.agents.ticket_guard_agent import TicketGuardAgent
from app.services.websocket_service import get_connection_manager

logger = logging.getLogger(__name__)

# Fallback in case JSON file is missing
FALLBACK_RULES = {
    "negative_intents": ["hi", "hello", "halo", "hallo", "test", "p", "ping"],
    "positive_intents": ["help", "error", "problem"],
    "priority_keywords": {"urgent": ["urgent"], "high": ["billing"]}
}

class TicketService:
    def __init__(self):
        self.supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        self.rules = self._load_guard_rules()
        
        if not AgentRegistry.is_registered("ticket_guard_agent"):
            AgentRegistry.register("ticket_guard_agent", TicketGuardAgent)

    def _load_guard_rules(self) -> dict:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, "config", "ticket_rules.json")
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Could not load ticket rules: {e}")
        return FALLBACK_RULES

    # [FIX] Added message_count argument to match the caller in webhook.py
    async def evaluate_incoming_message(self, message: str, customer_name: str = "Customer", message_count: int = 0) -> TicketDecision:
        # --- LAYER 1: FAST GUARD (Local Regex) ---
        clean_msg = message.strip().lower()
        clean_msg_alpha = re.sub(r'[^\w\s]', '', clean_msg)
        
        negative_keywords = self.rules.get('negative_intents', [])
        
        # Check Exact Match or Short Spam
        is_greeting = clean_msg_alpha in negative_keywords
        is_short_spam = len(clean_msg_alpha) < 4 and clean_msg_alpha in ["p", "y", "yo", "tes", "test", "cek", "info"]

        # Only trigger greeting guard if it's the start of a conversation (low message count)
        if (is_greeting or is_short_spam):
            if message_count > 5:
                logger.info(f"ℹ️ Greeting detected but message_count is {message_count}. treating as normal.")
            else:
                logger.info(f"⚠️ Fast Guard detected greeting: '{clean_msg}'. Creating LOW priority ticket.")
                
                return TicketDecision(
                    should_create_ticket=True, 
                    reason="Initial Greeting (Fast Guard)",
                    suggested_priority=TicketPriority.LOW,
                    suggested_category="other",
                    auto_reply_hint=f"Hello {customer_name}! 👋\nI have received your message and opened a support ticket for you.\n\nPlease describe your issue in detail so I can assist you better."
                )

        # --- LAYER 2: SMART GUARD (Agent) ---
        try:
            agent = AgentRegistry.get_or_create("ticket_guard_agent")
            if not agent.is_initialized():
                await agent.initialize()

            response_text, _ = await agent.run(user_id="system_guard", query=message)
            cleaned_text = self._clean_json_string(response_text)
            data = json.loads(cleaned_text)
            return TicketDecision(**data)

        except Exception as e:
            logger.error(f"Ticket Guard Agent failed: {e}")
            return TicketDecision(
                should_create_ticket=True, 
                reason="Agent Error (Fallback)", 
                suggested_priority=TicketPriority.LOW 
            )
    def _clean_json_string(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n", "", text)
            text = re.sub(r"\n```$", "", text)
        return text
    
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
                # [FIX] Use timezone-aware UTC
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            self.supabase.table("ticket_activities").insert(data).execute()
        except Exception as e:
            logger.error(f"Failed to log activity: {e}")

    async def create_ticket(self, data: TicketCreate, organization_id: str, actor_id: Optional[str], actor_type: ActorType) -> Ticket:
        # [FIX] Use timezone-aware timestamp
        num_res = self.supabase.rpc("generate_ticket_number", {"prefix": "TKT-"}).execute()
        ticket_num = num_res.data or f"TKT-{int(datetime.now(timezone.utc).timestamp())}"

        payload = data.model_dump()
        payload.update({
            "organization_id": organization_id,
            "ticket_number": ticket_num,
            "status": TicketStatus.OPEN,
            # [FIX] Use timezone-aware UTC
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

        res = self.supabase.table("tickets").insert(payload).execute()
        if not res.data: raise Exception("Failed to insert ticket")
        new_ticket = Ticket(**res.data[0])

        await self.log_activity(
            ticket_id=new_ticket.id, 
            action="created", 
            description=f"Ticket created ({actor_type.value})", 
            actor_id=actor_id, 
            actor_type=actor_type
        )

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
        except Exception as e:
            logger.warning(f"Failed to broadcast ticket creation: {e}")

        return new_ticket
    
    async def update_ticket(self, ticket_id: str, update_data: TicketUpdate, actor_id: str, actor_type: ActorType = ActorType.HUMAN) -> Ticket:
        # 1. Fetch OLD Data (To compare)
        old_res = self.supabase.table("tickets").select("*").eq("id", ticket_id).single().execute()
        if not old_res.data:
            raise Exception("Ticket not found")
        old_ticket = old_res.data

        # 2. Prepare Update Payload
        payload = update_data.model_dump(exclude_unset=True)
        # [FIX] Use timezone-aware UTC
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        # Auto-timestamps
        if payload.get("status") == TicketStatus.RESOLVED:
            payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
        elif payload.get("status") == TicketStatus.CLOSED:
            payload["closed_at"] = datetime.now(timezone.utc).isoformat()

        # 3. Perform Update
        res = self.supabase.table("tickets").update(payload).eq("id", ticket_id).execute()
        if not res.data:
            raise Exception("Failed to update ticket in DB")
        
        updated_ticket = res.data[0]

        # 4. Compare & Log
        
        # Check Status Change
        if payload.get("status") and payload["status"] != old_ticket["status"]:
            await self.log_activity(
                ticket_id=ticket_id,
                action="status_change",
                description=f"Status changed from {old_ticket['status']} to {payload['status']}",
                actor_id=actor_id,
                actor_type=actor_type,
                metadata={"old": old_ticket["status"], "new": payload["status"]}
            )
        
        # Check Priority Change
        if payload.get("priority") and payload["priority"] != old_ticket["priority"]:
            await self.log_activity(
                ticket_id=ticket_id,
                action="priority_change",
                description=f"Priority changed from {old_ticket['priority']} to {payload['priority']}",
                actor_id=actor_id,
                actor_type=actor_type
            )

        # Check Assignment Change
        if "assigned_agent_id" in payload and payload["assigned_agent_id"] != old_ticket["assigned_agent_id"]:
             new_agent = payload["assigned_agent_id"] or "Unassigned"
             await self.log_activity(
                ticket_id=ticket_id,
                action="assignment_change",
                description=f"Assigned to {new_agent}",
                actor_id=actor_id,
                actor_type=actor_type
            )

        return Ticket(**updated_ticket)

    async def get_ticket_history(self, ticket_id: str) -> List[TicketActivityResponse]:
        res = self.supabase.table("ticket_activities").select("*").eq("ticket_id", ticket_id).order("created_at", desc=True).execute()
        logs = res.data
        
        resolved_logs = []
        for log in logs:
            name = "System"
            if log["actor_type"] == "human": name = "Human Agent"
            elif log["actor_type"] == "ai": name = "AI Agent"
            resolved_logs.append({**log, "actor_name": name})
            
        return [TicketActivityResponse(**l) for l in resolved_logs]

# Singleton instance
_ticket_service = TicketService()
def get_ticket_service(): return _ticket_service