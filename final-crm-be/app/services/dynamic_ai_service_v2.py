import logging
import asyncio
import json
import time
from typing import Dict, Any, Optional

from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
from app.agents.dynamic_crm_agent_v2 import get_dynamic_crm_agent_v2
from app.services.webhook_callback_service import get_webhook_callback_service
from app.services.websocket_service import get_connection_manager
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType

logger = logging.getLogger(__name__)

class DynamicAIServiceV2:
    # [STABLE] Class-level tracker for alert rate limiting (In-Memory)
    _alert_tracker: Dict[str, float] = {}

    def __init__(self, supabase):
        self.supabase = supabase
        
        try:
            self.reader = get_crm_chroma_service_v2()
        except Exception as e:
            logger.warning(f"⚠️ Chroma Service Unavailable (Init Failed): {e}")
            self.reader = None

        self.speaker = get_dynamic_crm_agent_v2()
        self.webhook_service = get_webhook_callback_service()
        self.credit_service = get_credit_service()

    async def _broadcast_response(self, chat: Dict, message_db_record: Dict, agent_name: str):
        """
        Helper to send updates to WebSocket and Webhook.
        [FIX] Uses the standard WebSocketService.broadcast_new_message to ensure UI compatibility.
        """
        try:
            # 1. WebSocket (Real-time UI)
            if chat and "organization_id" in chat:
                manager = get_connection_manager()
                
                # Use the standard method defined in your WebSocketService
                await manager.broadcast_new_message(
                    organization_id=chat["organization_id"],
                    chat_id=chat["id"],
                    message_id=message_db_record["id"],
                    customer_id=chat.get("customer_id"),
                    customer_name=chat.get("customer_name", "Customer"),
                    message_content=message_db_record["content"],
                    channel=chat.get("channel", "web"),
                    handled_by=chat.get("handled_by", "ai"),
                    sender_type="ai",
                    sender_id=message_db_record["sender_id"],
                    sender_name=agent_name,
                    is_new_chat=False,
                    was_reopened=False,
                    metadata=message_db_record.get("metadata", {}),
                    attachment=None
                )

            # 2. Webhook (WhatsApp/Telegram)
            content = message_db_record.get("content", "")
            if content:
                asyncio.create_task(
                    self.webhook_service.send_callback(
                        chat=chat,
                        message_content=content,
                        supabase=self.supabase
                    )
                )
        except Exception as e:
            logger.error(f"⚠️ Broadcast failed: {e}")

    def _check_and_update_alert_cooldown(self, chat_id: str) -> bool:
        now = time.time()
        last_time = self._alert_tracker.get(chat_id, 0)
        
        # 15 Seconds Cooldown for System Alerts
        if now - last_time < 15:
            return False
        
        self._alert_tracker[chat_id] = now
        return True

    def _extract_agent_name(self, agent_settings: Dict) -> str:
        """Helper to safely extract agent name for UI display"""
        try:
            if not agent_settings: return "AI Assistant"
            config = agent_settings.get("persona_config", {})
            if isinstance(config, str):
                config = json.loads(config)
            return config.get("name", "AI Assistant")
        except:
            return "AI Assistant"

    async def process_and_respond(self, chat_id: str, msg_id: str, priority: str = "medium") -> Dict[str, Any]:
        """
        Orchestrate the AI response:
        1. Contextualize (DB + RAG)
        2. Generate (LLM Proxy V2 + Vision)
        3. Deliver (DB + WebSocket + Webhook)
        4. Billing (Credit Deduction)
        """
        chat = None
        agent_id = None
        agent_name = "AI Assistant"

        try:
            logger.info(f"🤖 Manager V2: Processing Chat {chat_id} (Msg {msg_id})")

            # 1. Fetch Chat Data
            chat_res = await asyncio.to_thread(
                lambda: self.supabase.table("chats").select("*").eq("id", chat_id).execute()
            )
            
            if not chat_res.data:
                return {"success": False, "reason": "chat_not_found"}
            chat = chat_res.data[0]

            # [FIX] RESOLVE REAL CUSTOMER NAME
            # The 'chats' table usually only has 'customer_id'. We need the real name from 'customers'.
            real_customer_name = "Customer"
            if chat.get("customer_id"):
                try:
                    cust_res = await asyncio.to_thread(
                        lambda: self.supabase.table("customers")
                        .select("name")
                        .eq("id", chat.get("customer_id"))
                        .single()
                        .execute()
                    )
                    if cust_res.data and cust_res.data.get("name"):
                        real_customer_name = cust_res.data.get("name")
                except Exception:
                    logger.warning(f"⚠️ Could not resolve name for customer {chat.get('customer_id')}")

            # 2. Fetch Agent Settings & Name
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
                    for k in ["id", "created_at", "updated_at", "agent_id"]:
                        agent_settings.pop(k, None)
            
            # Extract Name for UI Broadcast
            agent_name = self._extract_agent_name(agent_settings)

            # 3. Get User Message AND Metadata [FIXED]
            # We must select 'metadata' to access the image URL
            prompt_res = await asyncio.to_thread(
                lambda: self.supabase.table("messages")
                .select("content, metadata") 
                .eq("id", msg_id).execute()
            )
            
            user_prompt = ""
            msg_metadata = {}
            
            if prompt_res.data:
                user_prompt = prompt_res.data[0].get("content", "") or ""
                msg_metadata = prompt_res.data[0].get("metadata", {}) or {}

            # 4. Get History
            history_limit = 5
            if isinstance(agent_settings.get("advanced_config"), dict):
                history_limit = int(agent_settings["advanced_config"].get("historyLimit", 5))
            
            msgs_res = await asyncio.to_thread(
                lambda: self.supabase.table("messages").select("*").eq("chat_id", chat_id).order("created_at", desc=True).limit(history_limit).execute()
            )
            history = msgs_res.data[::-1] 

            # 5. CALL READER V2 (RAG)
            context = ""
            should_rag = (priority != "low" and len(user_prompt.split()) > 2)

            if should_rag:
                if self.reader: 
                    try:
                        context = await self.reader.query_context(query=user_prompt, agent_id=agent_id)
                        if context:
                            logger.info(f"📖 Reader V2 found context ({len(context)} chars)")
                    except Exception as rag_err:
                        logger.warning(f"⚠️ RAG Skipped (Service Error): {rag_err}")
                        context = "" 
                else:
                    logger.warning("⚠️ RAG Skipped: Reader service unavailable (Chroma Down).")
            else:
                logger.info("⏩ Smart RAG: Skipped (Trivial Message)")

            # 👁️ VISION BRIDGE: Extract Image URL
            media_url = msg_metadata.get("media_url")
            # Only pass URL if it's an image
            valid_image_url = media_url if (media_url and "image" in msg_metadata.get("media_type", "")) else None

            # 6. CALL SPEAKER V2 (Generation)
            # Passes text + optional image URL to the LLM Proxy
            response_data = await self.speaker.process_message(
                chat_id=chat_id,
                customer_message=user_prompt,
                chat_history=history,
                agent_settings=agent_settings,
                organization_id=chat.get("organization_id", ""), 
                rag_context=context,
                category=priority, 
                name_user=real_customer_name, # <--- [FIXED] Using the real name
                image_url=valid_image_url     # <--- [FIXED] Passing the image
            )
            
            reply_text = response_data.get("content", "Maaf, saya tidak dapat menjawab saat ini.")
            usage = response_data.get("usage", {})
            metadata = response_data.get("metadata", {})
            
            # [STABLE RATE LIMIT]
            if metadata.get("is_error", False):
                if not self._check_and_update_alert_cooldown(chat_id):
                    logger.warning(f"🛑 Suppression: System Alert rate limit active for {chat_id}")
                    return {"success": False, "reason": "alert_rate_limit"}

            # [DUPLICATE GUARD]
            if history and history[-1]["content"] == reply_text:
                logger.warning(f"🛑 Suppression: Detected exact duplicate response: '{reply_text[:20]}...'")
                return {"success": True, "duplicate": True}

            # 7. Save Response
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
                    "token_usage": usage,
                    "is_error": metadata.get("is_error", False)
                }
            }

            res = await asyncio.to_thread(
                lambda: self.supabase.table("messages").insert(ai_msg).execute()
            )
            
            full_db_record = res.data[0]
            ai_message_id = full_db_record["id"]

            # 8. Broadcast (Using Standard Method)
            await self._broadcast_response(chat, full_db_record, agent_name)

            # 9. TRACK CREDITS
            is_system_error = metadata.get("is_error", False)
            if usage and chat.get("organization_id") and not is_system_error:
                try:
                    total_tokens = usage.get("total_tokens", 0)
                    cost = total_tokens * 0.000002
                    
                    if cost > 0:
                        await self.credit_service.add_transaction(CreditTransactionCreate(
                            organization_id=chat["organization_id"],
                            amount=-cost, 
                            description=f"AI Response (Tokens: {total_tokens})",
                            transaction_type=TransactionType.USAGE,
                            metadata={"chat_id": chat_id, "message_id": ai_message_id}
                        ))
                except Exception as e:
                    logger.error(f"⚠️ Credit tracking failed: {e}")
            elif is_system_error:
                logger.info("💳 Credit deduction skipped (System Error Response)")

            return {"success": True, "ai_message_id": ai_message_id}

        except Exception as e:
            logger.error(f"❌ Manager V2 Critical Failure: {e}")
            
            # FALLBACK MECHANISM
            if chat:
                try:
                    # [FALLBACK RATE LIMIT]
                    if not self._check_and_update_alert_cooldown(chat_id):
                        logger.warning("🛑 Fallback Suppression: Rate limit active.")
                        return {"success": False, "error": str(e), "suppressed": True}

                    fallback_agent_id = agent_id if agent_id else "ai_agent_v2"
                    error_msg = "Maaf, sistem sedang mengalami gangguan teknis. Mohon coba lagi nanti."
                    
                    fallback_payload = {
                        "chat_id": chat_id,
                        "sender_type": "ai",
                        "sender_id": fallback_agent_id, 
                        "content": error_msg,
                        "metadata": {"error": str(e), "fallback": True}
                    }
                    
                    res = await asyncio.to_thread(
                        lambda: self.supabase.table("messages").insert(fallback_payload).execute()
                    )
                    
                    # [FIX] Broadcast Fallback with Standard Method
                    await self._broadcast_response(chat, res.data[0], agent_name or "System AI")
                    
                except Exception as final_err:
                     logger.error(f"💀 Final Fallback Failed: {final_err}")

            return {"success": False, "error": str(e)}
        
             
def process_dynamic_ai_response_v2(chat_id: str, msg_id: str, supabase: Any, priority: str = "medium"):
    service = DynamicAIServiceV2(supabase)
    return service.process_and_respond(chat_id, msg_id, priority)