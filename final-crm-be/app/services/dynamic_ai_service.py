"""
Dynamic AI Service
Orchestrates AI responses with RAG and dynamic agent settings.
Fetches Ticket Category and Customer Name for specialized Proxy Routing.
Uses dedicated CRMChromaService for safe RAG retrieval.
"""
import logging
import asyncio
import json
from datetime import datetime
from typing import Dict, Any, Optional

# [MODIFIED] Switch to specialized CRM Chroma Service
from app.services.crm_chroma_service import get_crm_chroma_service
from app.agents.dynamic_crm_agent import get_dynamic_crm_agent
from app.services.websocket_service import get_connection_manager
from app.services.webhook_callback_service import WebhookCallbackService

logger = logging.getLogger(__name__)

class DynamicAIService:
    def __init__(self, supabase):
        self.supabase = supabase
        # [MODIFIED] Use the specialized safe service
        self.chroma_service = get_crm_chroma_service()
        self.agent = get_dynamic_crm_agent()
        self.webhook_service = WebhookCallbackService()

    async def process_and_respond(self, chat_id: str, customer_message_id: str) -> Dict[str, Any]:
        try:
            # 1. Fetch Chat Data
            chat_res = self.supabase.table("chats").select("*").eq("id", chat_id).execute()
            if not chat_res.data:
                return {"success": False, "reason": "chat_not_found"}
            chat = chat_res.data[0]
            customer_id = chat.get("customer_id")
            org_id = chat.get("organization_id")

            if chat.get("handled_by") != "ai":
                return {"success": False, "reason": "not_ai_chat"}

            # 2. Fetch Customer Message
            msg_res = self.supabase.table("messages").select("content").eq("id", customer_message_id).execute()
            if not msg_res.data:
                return {"success": False, "reason": "message_not_found"}
            customer_message = msg_res.data[0]["content"]

            # 3. Fetch Active Ticket (For Category)
            category = "inquiry" # Default
            try:
                ticket_res = self.supabase.table("tickets") \
                    .select("category") \
                    .eq("chat_id", chat_id) \
                    .in_("status", ["open", "in_progress"]) \
                    .limit(1) \
                    .execute()
                
                if ticket_res.data:
                    category = ticket_res.data[0].get("category") or "inquiry"
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch ticket category: {e}")

            # 4. Fetch Customer Name (For nameUser)
            name_user = "Customer"
            try:
                cust_res = self.supabase.table("customers").select("name").eq("id", customer_id).execute()
                if cust_res.data:
                    name_user = cust_res.data[0].get("name") or "Customer"
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch customer name: {e}")

            # 5. Fetch Agent Settings
            agent_id = chat.get("ai_agent_id") or chat.get("assigned_agent_id")
            agent_settings = {}
            if agent_id:
                settings_res = self.supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()
                if settings_res.data:
                    agent_settings = settings_res.data[0]

            # 6. Safe RAG (Retrieve Context)
            # [MODIFIED] The CRM service handles all safety checks internally
            rag_context = self.chroma_service.get_rag_context(
                query=customer_message,
                organization_id=org_id,
                top_k=3
            )
            if rag_context:
                logger.info(f"📚 RAG Context Loaded: {len(rag_context)} chars")

            # 7. Load History
            hist_res = self.supabase.table("messages") \
                .select("*") \
                .eq("chat_id", chat_id) \
                .order("created_at", desc=False) \
                .limit(20) \
                .execute()
            
            chat_history = []
            for m in hist_res.data:
                if m["id"] == customer_message_id: continue
                role = "user" if m["sender_type"] == "customer" else "assistant"
                chat_history.append({"role": role, "content": m["content"]})

            # 8. Call Dynamic Agent
            response_text = await self.agent.process_message(
                chat_id=chat_id,
                customer_message=customer_message,
                chat_history=chat_history,
                agent_settings=agent_settings,
                rag_context=rag_context,
                category=category,      
                name_user=name_user     
            )

            # 9. Save & Broadcast Response
            save_data = {
                "chat_id": chat_id,
                "sender_type": "ai",
                "sender_id": agent_id,
                "content": response_text,
                "metadata": {
                    "agent": "dynamic_crm_agent",
                    "rag_enabled": bool(rag_context),
                    "proxy_category": category
                }
            }
            saved_msg = self.supabase.table("messages").insert(save_data).execute()
            ai_message_id = saved_msg.data[0]["id"]

            self.supabase.table("chats").update({
                "last_message_at": datetime.utcnow().isoformat()
            }).eq("id", chat_id).execute()

            await self._broadcast_response(chat, ai_message_id, response_text, agent_id)

            return {"success": True, "ai_message_id": ai_message_id}

        except Exception as e:
            logger.error(f"❌ Dynamic AI Service Error: {e}")
            return {"success": False, "error": str(e)}

    async def _broadcast_response(self, chat, msg_id, content, agent_id):
        # WebSocket
        try:
            conn = get_connection_manager()
            await conn.broadcast_new_message(
                organization_id=chat.get("organization_id"),
                chat_id=chat["id"],
                message_id=msg_id,
                customer_id=chat.get("customer_id"),
                customer_name="AI Agent",
                message_content=content,
                channel=chat.get("channel"),
                handled_by="ai",
                sender_type="ai",
                sender_id=agent_id or "ai_agent"
            )
        except Exception as ws_err:
            logger.warning(f"WS Error: {ws_err}")

        # Webhook Callback
        try:
            asyncio.create_task(
                self.webhook_service.send_callback(
                    chat=chat,
                    message_content=content,
                    supabase=self.supabase
                )
            )
        except Exception as wh_err:
            logger.warning(f"Webhook Callback Error: {wh_err}")


async def process_dynamic_ai_response_async(chat_id: str, customer_message_id: str, supabase):
    service = DynamicAIService(supabase)
    await service.process_and_respond(chat_id, customer_message_id)