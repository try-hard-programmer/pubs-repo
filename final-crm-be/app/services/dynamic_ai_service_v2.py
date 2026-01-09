import logging
import asyncio
import json
from typing import Dict, Any, Optional

from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
from app.agents.dynamic_crm_agent_v2 import get_dynamic_crm_agent_v2
from app.services.webhook_callback_service import get_webhook_callback_service
from app.services.websocket_service import get_connection_manager
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType

logger = logging.getLogger(__name__)

class DynamicAIServiceV2:
    def __init__(self, supabase):
        self.supabase = supabase
        self.reader = get_crm_chroma_service_v2()
        self.speaker = get_dynamic_crm_agent_v2()
        self.webhook_service = get_webhook_callback_service()
        self.credit_service = get_credit_service()

    async def _broadcast_response(self, chat: Dict, message_id: str, content: str):
        """Helper to send updates to WebSocket and Webhook"""
        try:
            # 1. WebSocket (Real-time UI)
            if chat and "organization_id" in chat:
                manager = get_connection_manager()
                await manager.broadcast_to_org(
                    chat["organization_id"],
                    {
                        "type": "new_message",
                        "chat_id": chat["id"],
                        "message": {
                            "id": message_id,
                            "content": content,
                            "sender_type": "ai",
                            "created_at": "now", # client will adjust
                        }
                    }
                )

            # 2. Webhook (WhatsApp/Telegram)
            # We fire and forget this wrapper
            asyncio.create_task(
                self.webhook_service.send_callback(
                    chat=chat,
                    message_content=content,
                    supabase=self.supabase
                )
            )
        except Exception as e:
            logger.error(f"⚠️ Broadcast failed: {e}")

    async def process_and_respond(self, chat_id: str, msg_id: str, priority: str = "medium") -> Dict[str, Any]:
        """
        Orchestrate the AI response:
        1. Contextualize (DB + RAG)
        2. Generate (LLM Proxy V2)
        3. Deliver (DB + WebSocket + Webhook)
        4. Billing (Credit Deduction)
        """
        # Initialize vars for error handling scope
        chat = None
        agent_id = None

        try:
            logger.info(f"🤖 Manager V2: Processing Chat {chat_id} (Msg {msg_id})")

            # 1. Fetch Chat Data (Non-blocking)
            chat_res = await asyncio.to_thread(
                lambda: self.supabase.table("chats").select("*").eq("id", chat_id).execute()
            )
            
            if not chat_res.data:
                return {"success": False, "reason": "chat_not_found"}
            chat = chat_res.data[0]
            
            # 2. Fetch Agent Settings (Non-blocking)
            agent_id = chat.get("sender_agent_id") 
            if not agent_id:
                agent_res = await asyncio.to_thread(
                    lambda: self.supabase.table("agents").select("id").eq("organization_id", chat["organization_id"]).limit(1).execute()
                )
                if agent_res.data:
                    agent_id = agent_res.data[0]["id"]
            
            agent_settings = {}
            if agent_id:
                settings_res = await asyncio.to_thread(
                    lambda: self.supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute()
                )
                if settings_res.data:
                    agent_settings = settings_res.data[0]
                    # Cleanup internal fields
                    for k in ["id", "created_at", "updated_at", "agent_id"]:
                        agent_settings.pop(k, None)

            # 3. Get User Message (Non-blocking)
            prompt_res = await asyncio.to_thread(
                lambda: self.supabase.table("messages").select("content").eq("id", msg_id).execute()
            )
            user_prompt = prompt_res.data[0]["content"] if prompt_res.data else ""

            # 4. Get History (Non-blocking)
            history_limit = 5
            if isinstance(agent_settings.get("advanced_config"), dict):
                history_limit = int(agent_settings["advanced_config"].get("historyLimit", 5))
            
            msgs_res = await asyncio.to_thread(
                lambda: self.supabase.table("messages").select("*").eq("chat_id", chat_id).order("created_at", desc=True).limit(history_limit).execute()
            )
            history = msgs_res.data[::-1] 

            # 5. CALL READER V2 (RAG) - [UPDATED: FAIL-SAFE]
            context = ""
            should_rag = (priority != "low" and len(user_prompt.split()) > 2)

            if should_rag:
                try:
                    # We wrap this in try-except so a 401 error doesn't kill the whole process
                    context = await self.reader.query_context(query=user_prompt, agent_id=agent_id)
                    if context:
                        logger.info(f"📖 Reader V2 found context ({len(context)} chars)")
                except Exception as rag_err:
                    logger.warning(f"⚠️ RAG Skipped (Service Error): {rag_err}")
                    context = "" # Proceed without context
            else:
                logger.info("⏩ Smart RAG: Skipped (Trivial Message)")

            # 6. CALL SPEAKER V2 (Generation)
            # This communicates with your Local Proxy
            response_data = await self.speaker.process_message(
                chat_id=chat_id,
                customer_message=user_prompt,
                chat_history=history,
                agent_settings=agent_settings,
                organization_id=chat.get("organization_id", ""), 
                rag_context=context,
                category=priority, 
                name_user=chat.get("customer_name", "Customer")
            )
            
            # Extract content and usage
            reply_text = response_data.get("content", "Maaf, saya tidak dapat menjawab saat ini.")
            usage = response_data.get("usage", {})

            # 7. Save Response (Non-blocking)
            ai_msg = {
                "chat_id": chat_id,
                "sender_type": "ai",
                "sender_id": agent_id or "ai_agent_v2", 
                "content": reply_text,
                "metadata": {
                    "is_internal": False,
                    "model": "v2_proxy_local",
                    "rag_enabled": bool(context),
                    "guard_priority": priority,
                    "token_usage": usage 
                }
            }

            res = await asyncio.to_thread(
                lambda: self.supabase.table("messages").insert(ai_msg).execute()
            )
            ai_message_id = res.data[0]["id"]

            # 8. Broadcast
            await self._broadcast_response(chat, ai_message_id, reply_text)

            # 9. TRACK CREDITS
            if usage and chat.get("organization_id"):
                try:
                    total_tokens = usage.get("total_tokens", 0)
                    # Pricing: $0.000002 per token
                    cost = total_tokens * 0.000002
                    
                    await self.credit_service.add_transaction(CreditTransactionCreate(
                        organization_id=chat["organization_id"],
                        amount=-cost, 
                        description=f"AI Response (Tokens: {total_tokens})",
                        transaction_type=TransactionType.USAGE,
                        metadata={"chat_id": chat_id, "message_id": ai_message_id}
                    ))
                    
                except Exception as e:
                    logger.error(f"⚠️ Credit tracking failed: {e}")

            return {"success": True, "ai_message_id": ai_message_id}

        except Exception as e:
            logger.error(f"❌ Manager V2 Critical Failure: {e}")
            
            # FALLBACK MECHANISM
            # If everything crashes, we must still reply to the user.
            if chat:
                try:
                    # Ensure we use the correct agent_id to avoid "Unknown" sender
                    fallback_agent = agent_id if agent_id else "ai_agent_v2"
                    
                    error_msg = "Maaf, sistem sedang mengalami gangguan teknis. Mohon coba lagi nanti."
                    
                    fallback_payload = {
                        "chat_id": chat_id,
                        "sender_type": "ai",
                        "sender_id": fallback_agent, # <--- Fixes the 'Unknown' name
                        "content": error_msg,
                        "metadata": {"error": str(e), "fallback": True}
                    }
                    
                    res = await asyncio.to_thread(
                        lambda: self.supabase.table("messages").insert(fallback_payload).execute()
                    )
                    
                    # Broadcast the error message so the UI updates
                    await self._broadcast_response(chat, res.data[0]["id"], error_msg)
                    
                except Exception as final_err:
                     logger.error(f"💀 Final Fallback Failed: {final_err}")

            return {"success": False, "error": str(e)}

# Wrapper for external calls (Queue Service)
def process_dynamic_ai_response_v2(chat_id: str, msg_id: str, supabase: Any, priority: str = "medium"):
    service = DynamicAIServiceV2(supabase)
    # We await the async method from this sync wrapper? 
    # No, the caller (Queue) is already async, so it should call the method directly.
    # But to keep the import clean in llm_queue_service, we return the coroutine.
    return service.process_and_respond(chat_id, msg_id, priority)