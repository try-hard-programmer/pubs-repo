"""
Dynamic AI Service V2
The "Manager" for Team V2.
Orchestrates Reader V2, Speaker V2, and Messenger V2.
"""
import logging
import asyncio
import json
from typing import Dict, Any

# [IMPORT V2 TEAM]
from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
from app.agents.dynamic_crm_agent_v2 import get_dynamic_crm_agent_v2
from app.services.webhook_callback_service import get_webhook_service
from app.services.websocket_service import get_connection_manager

logger = logging.getLogger(__name__)

class DynamicAIServiceV2:
    def __init__(self, supabase):
        self.supabase = supabase
        self.reader = get_crm_chroma_service_v2()
        self.speaker = get_dynamic_crm_agent_v2()
        self.webhook_service = get_webhook_service()

    async def process_and_respond(self, chat_id: str, customer_message_id: str) -> Dict[str, Any]:
        try:
            logger.info(f"🤖 Manager V2: Processing Chat {chat_id}")

            # 1. Fetch Chat Data
            chat_res = self.supabase.table("chats").select("*").eq("id", chat_id).execute()
            if not chat_res.data:
                return {"success": False, "reason": "chat_not_found"}
            chat = chat_res.data[0]
            
            # 2. Fetch Agent Settings (CORRECTED for DB Schema)
            agent_id = chat.get("sender_agent_id") 
            
            # Fallback: find agent in org if not set on chat
            if not agent_id:
                agent_res = self.supabase.table("agents").select("id").eq("organization_id", chat["organization_id"]).limit(1).execute()
                if agent_res.data:
                    agent_id = agent_res.data[0]["id"]
            
            agent_settings = {}
            if agent_id:
                # [FIX] Fetch from 'agent_settings' TABLE
                settings_res = self.supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()
                if settings_res.data:
                    agent_settings = settings_res.data[0]
                    # Clean up DB fields so they don't confuse the LLM
                    for k in ["id", "created_at", "updated_at", "agent_id"]:
                        agent_settings.pop(k, None)

            # 3. Get History
            msgs_res = self.supabase.table("messages").select("*").eq("chat_id", chat_id).order("created_at", desc=True).limit(5).execute()
            history = msgs_res.data[::-1] 

            # 4. Get User Message
            prompt_res = self.supabase.table("messages").select("message_content").eq("id", customer_message_id).execute()
            user_prompt = prompt_res.data[0]["message_content"] if prompt_res.data else ""

            # 5. CALL READER V2 (RAG)
            context = self.reader.query_context(
                query=user_prompt, 
                organization_id=chat["organization_id"]
            )
            if context:
                logger.info(f"📖 Reader V2 found context ({len(context)} chars)")

            # 6. CALL SPEAKER V2 (Generation)
            reply = await self.speaker.process_message(
                chat_id=chat_id,
                customer_message=user_prompt,
                chat_history=history,
                agent_settings=agent_settings,
                rag_context=context,
                category="general", 
                name_user=chat.get("customer_name", "Customer")
            )

            # 7. Save Response
            ai_msg = {
                "chat_id": chat_id,
                "sender_type": "ai",
                "sender_id": agent_id or "ai_agent_v2", 
                "message_content": reply,
                "is_internal": False
            }
            res = self.supabase.table("messages").insert(ai_msg).execute()
            ai_message_id = res.data[0]["id"]

            # 8. Broadcast (WS + Webhook)
            await self._broadcast_response(chat, ai_message_id, reply)

            return {"success": True, "ai_message_id": ai_message_id}

        except Exception as e:
            logger.error(f"❌ Manager V2 Failed: {e}")
            return {"success": False, "error": str(e)}

    async def _broadcast_response(self, chat, msg_id, content):
        # WebSocket (Frontend)
        try:
            conn = get_connection_manager()
            await conn.broadcast_new_message(
                organization_id=chat["organization_id"],
                chat_id=chat["id"],
                message_id=msg_id,
                customer_id=chat.get("customer_id"),
                message_content=content,
                sender_type="ai"
            )
        except Exception as e:
            logger.warning(f"Broadcast WS error: {e}")

        # Webhook (Userbot/WhatsApp)
        try:
            await self.webhook_service.send_callback(
                chat=chat,
                message_content=content,
                supabase=self.supabase
            )
        except Exception as e:
            logger.warning(f"Broadcast Webhook error: {e}")

async def process_dynamic_ai_response_v2(chat_id: str, msg_id: str, supabase):
    service = DynamicAIServiceV2(supabase)
    await service.process_and_respond(chat_id, msg_id)