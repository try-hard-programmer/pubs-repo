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
# [FIX] Import the Redis Lock
from app.services.redis_service import acquire_lock

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
        """
        try:
            # 1. WebSocket (Real-time UI)
            if chat and "organization_id" in chat:
                manager = get_connection_manager()
                
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

    async def process_and_respond(self, chat_id: str, msg_id: str, priority: str = "low", ticket_id: str = None) -> Dict[str, Any]:
        """
        Orchestrate the AI response (Respects Configs: History Limit + Ticket Categories)
        """
        lock_key = f"ai_v2_lock:{chat_id}"
        
        async with acquire_lock(lock_key, expire=30) as acquired:
            if not acquired:
                logger.warning(f"🔒 AI V2 Locked for {chat_id}. Skipping.")
                return {"success": False, "reason": "locked_rate_limited"}

            chat = None
            agent_id = None
            agent_name = "AI Assistant"

            try:
                # 1. Fetch Chat Data
                chat_res = await asyncio.to_thread(lambda: self.supabase.table("chats").select("*").eq("id", chat_id).execute())
                if not chat_res.data: return {"success": False, "reason": "chat_not_found"}
                chat = chat_res.data[0]

                # Resolve Customer Name
                real_customer_name = "Customer"
                if chat.get("customer_id"):
                    try:
                        cust_res = await asyncio.to_thread(lambda: self.supabase.table("customers").select("name").eq("id", chat.get("customer_id")).single().execute())
                        if cust_res.data: real_customer_name = cust_res.data.get("name", "Customer")
                    except: pass

                # 2. Fetch Agent Settings (Source of Truth)
                agent_id = chat.get("sender_agent_id") 
                if not agent_id:
                    agent_res = await asyncio.to_thread(lambda: self.supabase.table("agents").select("id").eq("organization_id", chat["organization_id"]).limit(1).execute())
                    if agent_res.data: agent_id = agent_res.data[0]["id"]
                
                agent_settings = {}
                if agent_id:
                    settings_res = await asyncio.to_thread(lambda: self.supabase.table("agent_settings").select("*").eq("agent_id", agent_id).execute())
                    if settings_res.data: agent_settings = settings_res.data[0]
                
                def parse_cfg(key):
                    val = agent_settings.get(key, {})
                    if isinstance(val, str):
                        try: return json.loads(val)
                        except: return {}
                    return val if isinstance(val, dict) else {}

                persona_config = parse_cfg("persona_config")
                advanced_config = parse_cfg("advanced_config") # contains historyLimit
                ticketing_config = parse_cfg("ticketing_config") # contains categories
                
                agent_name = persona_config.get("name", "AI Assistant")

                # 3. DYNAMIC SNAPSHOT (Respects 'historyLimit' from advanced_config)
                history_limit = int(advanced_config.get("historyLimit", 10))
                                
                # Buffer fetch to find boundary
                fetch_buffer = history_limit * 2 
                msgs_res = await asyncio.to_thread(
                    lambda: self.supabase.table("messages")
                    .select("*")
                    .eq("chat_id", chat_id)
                    .order("created_at", desc=True)
                    .limit(fetch_buffer) 
                    .execute()
                )
                raw_snapshot = msgs_res.data

                # 4. LAST ASSISTANT FILTER (The "Test 3" Cleaner) *Option will adjust later dont erase it
                pending_user_messages = []
                last_assistant_message = None
                
                for msg in raw_snapshot:
                    sender = msg.get("sender_type")
                    if sender == "ai":
                        last_assistant_message = msg
                        break
                    else:
                        pending_user_messages.append(msg)

                if not last_assistant_message and not pending_user_messages:
                     pending_user_messages = raw_snapshot

                pending_user_messages = pending_user_messages[::-1] 

                if not pending_user_messages:
                    return {"success": False, "reason": "no_pending_messages"}

                full_user_prompt_text = "\n".join(
                    [m.get("content", "") for m in pending_user_messages if m.get("content")]
                )
                
                clean_history = []
                if last_assistant_message:
                    clean_history.append(last_assistant_message)
                
                # 5. VISION & RAG
                valid_image_urls = []
                for pm in pending_user_messages:
                    meta = pm.get("metadata") or {}
                    url = meta.get("media_url")
                    if url and ("image" in str(meta.get("media_type", "")).lower() or any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp'])):
                        valid_image_urls.append(url)
                
                vision_context = ""
                if valid_image_urls:
                    target_img = valid_image_urls[0] 
                    custom_vision = advanced_config.get("vision_prompt")
                    vision_prompt = custom_vision if custom_vision else "Analyze this image. Extract ALL text/codes. Describe context."
                    
                    try:
                        vision_desc = await self.speaker.analyze_image(
                            image_url=target_img, 
                            prompt=vision_prompt,
                            organization_id=chat.get("organization_id", "")
                        )
                        vision_context = f"\nSystem Analysis of User Image: {vision_desc}"
                    except: pass

                rag_query = f"{full_user_prompt_text} {vision_context}".strip()
                context = ""
                if rag_query and self.reader:
                    try:
                        context = await self.reader.query_context(query=rag_query, agent_id=agent_id)
                    except: pass

                # 6. CALL SPEAKER V2 (With TICKET CATEGORIES)
                ticket_categories = ticketing_config.get("categories", [])
                response_data = await self.speaker.process_message(
                    chat_id=chat_id,
                    customer_message=full_user_prompt_text, 
                    chat_history=clean_history,             
                    agent_settings=agent_settings,
                    organization_id=chat.get("organization_id", ""), 
                    rag_context=context,
                    category=priority, 
                    name_user=real_customer_name,
                    image_urls=valid_image_urls,
                    ticket_categories=ticket_categories,
                    ticket_id=ticket_id
                )
                
                reply_text = response_data.get("content", "Maaf, saya tidak dapat menjawab saat ini.")
                # AI might return a category like "elektronik"
                detected_category = response_data.get("category", priority) 
                usage = response_data.get("usage", {})
                metadata = response_data.get("metadata", {})
                
                if metadata.get("is_error", False):
                    if not self._check_and_update_alert_cooldown(chat_id):
                        return {"success": False, "reason": "alert_rate_limit"}

                # 8. Save Response (Include Category in Metadata for Webhook/Ticketing Service)
                ai_msg = {
                    "chat_id": chat_id,
                    "sender_type": "ai",
                    "sender_id": agent_id or "ai_agent_v2", 
                    "content": reply_text,
                    "metadata": {
                        "is_internal": False,
                        "model": "v2_proxy_local",
                        "rag_enabled": bool(context),
                        "guard_priority": detected_category, # AI's classification
                        "token_usage": usage,
                        "is_error": metadata.get("is_error", False)
                    }
                }

                res = await asyncio.to_thread(lambda: self.supabase.table("messages").insert(ai_msg).execute())
                full_db_record = res.data[0]
                await self._broadcast_response(chat, full_db_record, agent_name)

                # Billing
                if usage and chat.get("organization_id") and not metadata.get("is_error", False):
                    try:
                        total_tokens = usage.get("total_tokens", 0)
                        cost = total_tokens * 0.000002
                        if cost > 0:
                            await self.credit_service.add_transaction(CreditTransactionCreate(
                                organization_id=chat["organization_id"],
                                amount=-cost, 
                                description=f"AI Response (Tokens: {total_tokens})",
                                transaction_type=TransactionType.USAGE,
                                metadata={"chat_id": chat_id, "message_id": full_db_record["id"]}
                            ))
                    except: pass

                return {"success": True, "ai_message_id": full_db_record["id"]}

            except Exception as e:
                logger.error(f"❌ Manager V2 Critical Failure: {e}", exc_info=True)
                return {"success": False, "error": str(e)}
                                                 
def process_dynamic_ai_response_v2(chat_id: str, msg_id: str, supabase: Any, priority: str = "medium", ticket_id: str = None):
    service = DynamicAIServiceV2(supabase)
    return service.process_and_respond(chat_id, msg_id, priority, ticket_id)