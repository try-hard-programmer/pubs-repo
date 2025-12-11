"""
AI Response Service
Orchestrates AI agent processing and manages the flow from customer message to AI response
"""
import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.services.websocket_service import get_connection_manager

logger = logging.getLogger(__name__)


class AIResponseService:
    """
    Service for orchestrating AI agent responses.

    Responsibilities:
    - Validate chat is handled by AI
    - Load chat history from database
    - Call CRM AI agent for response
    - Save AI response to database
    - Trigger webhook callback to external service

    Flow:
    1. Customer message arrives → Saved to DB
    2. Check if chat handled_by="ai"
    3. Load recent chat history (for context)
    4. Call CRM AI agent with history
    5. Save AI response to messages table
    6. Send webhook callback to WhatsApp/Telegram service
    """

    def __init__(self, supabase):
        """
        Initialize AI Response Service

        Args:
            supabase: Supabase client instance
        """
        self.supabase = supabase
        self.crm_agent = None

    async def _ensure_agent_initialized(self):
        """
        Lazy initialization of CRM AI agent.

        Only initializes agent when first needed, then reuses
        the singleton instance for better performance.
        """
        if self.crm_agent is None:
            from app.agents.crm_agent_ai import get_crm_agent
            self.crm_agent = await get_crm_agent()
            logger.info("CRM AI agent loaded in AI Response Service")

    async def process_and_respond(
        self,
        chat_id: str,
        customer_message_id: str
    ) -> Dict[str, Any]:
        """
        Process customer message with AI agent and send response.

        This is the main entry point for AI response generation.

        Flow:
        1. Validate chat is handled by AI (not human)
        2. Load chat history for context (last 10 messages)
        3. Get latest customer message
        4. Call CRM AI agent with history
        5. Save AI response to messages table
        6. Trigger webhook callback (async)

        Args:
            chat_id: Chat UUID
            customer_message_id: Latest customer message UUID

        Returns:
            Result dict:
            {
                "success": bool,
                "ai_message_id": str (if success),
                "webhook_triggered": bool,
                "reason": str (if failed)
            }

        Example:
            service = AIResponseService(supabase)
            result = await service.process_and_respond(
                chat_id="chat-uuid-123",
                customer_message_id="msg-uuid-456"
            )
        """
        try:
            logger.info(f"🤖 Processing AI response for chat: {chat_id}")

            # Step 1: Get chat data and validate
            chat_response = self.supabase.table("chats") \
                .select("*") \
                .eq("id", chat_id) \
                .execute()

            if not chat_response.data:
                logger.error(f"Chat {chat_id} not found")
                return {"success": False, "reason": "chat_not_found"}

            chat = chat_response.data[0]

            # Validate: must be handled by AI
            if chat.get("handled_by") != "ai":
                logger.info(
                    f"⏭️  Chat {chat_id} handled by {chat.get('handled_by')}, "
                    "skipping AI response"
                )
                return {
                    "success": False,
                    "reason": "not_ai_chat",
                    "handled_by": chat.get("handled_by")
                }

            logger.info(f"✅ Chat {chat_id} is handled by AI, proceeding...")

            # Step 2: Load chat history (last 10 messages for context)
            messages_response = self.supabase.table("messages") \
                .select("*") \
                .eq("chat_id", chat_id) \
                .order("created_at", desc=False) \
                .limit(10) \
                .execute()

            # Convert to chat history format
            chat_history = []
            for msg in messages_response.data:
                sender_type = msg.get("sender_type")

                if sender_type == "customer":
                    chat_history.append({
                        "role": "user",
                        "content": msg.get("content", "")
                    })
                elif sender_type in ["ai", "agent"]:
                    chat_history.append({
                        "role": "assistant",
                        "content": msg.get("content", "")
                    })

            logger.info(f"📚 Loaded {len(chat_history)} messages for context")

            # Step 3: Get latest customer message
            customer_msg_response = self.supabase.table("messages") \
                .select("content") \
                .eq("id", customer_message_id) \
                .execute()

            if not customer_msg_response.data:
                logger.error(f"Customer message {customer_message_id} not found")
                return {"success": False, "reason": "message_not_found"}

            customer_message = customer_msg_response.data[0].get("content", "")

            logger.info(
                f"💬 Customer message: "
                f"{customer_message[:100]}{'...' if len(customer_message) > 100 else ''}"
            )

            # Step 4: Ensure agent initialized and call it
            await self._ensure_agent_initialized()

            # Remove last message from history (it's the current one)
            context_history = chat_history[:-1] if len(chat_history) > 0 else []

            logger.info(f"🧠 Calling CRM AI agent with {len(context_history)} context messages...")

            ai_response = await self.crm_agent.process_message(
                chat_id=chat_id,
                customer_message=customer_message,
                chat_history=context_history
            )

            logger.info(
                f"🤖 AI response generated: "
                f"{ai_response[:100]}{'...' if len(ai_response) > 100 else ''}"
            )

            # Step 5: Save AI response to database
            message_data = {
                "chat_id": chat_id,
                "sender_type": "ai",
                "sender_id": chat.get("ai_agent_id"),
                "content": ai_response,
                "metadata": {
                    "model": "gemini-1.5-flash",
                    "agent": "crm_agent",
                    "processed_at": datetime.utcnow().isoformat()
                }
            }

            save_response = self.supabase.table("messages") \
                .insert(message_data) \
                .execute()

            if not save_response.data:
                logger.error(f"Failed to save AI response for chat {chat_id}")
                return {"success": False, "reason": "save_failed"}

            ai_message_id = save_response.data[0]["id"]

            logger.info(f"💾 AI response saved to DB: {ai_message_id}")

            # Update chat's last_message_at
            self.supabase.table("chats") \
                .update({"last_message_at": datetime.utcnow().isoformat()}) \
                .eq("id", chat_id) \
                .execute()
                    
            # [FIX] BROADCAST TO WEBSOCKET
            try:
                from app.services.websocket_service import get_connection_manager
                conn = get_connection_manager()
                
                await conn.broadcast_new_message(
                    organization_id=chat.get("organization_id"),
                    chat_id=chat_id,
                    message_id=ai_message_id,
                    customer_id=chat.get("customer_id"),
                    customer_name="AI Agent",
                    message_content=ai_response,
                    channel=chat.get("channel"),
                    handled_by=chat.get("handled_by"),
                    sender_type="ai",
                    sender_id=chat.get("ai_agent_id") or "ai_agent",
                    is_new_chat=False,
                    was_reopened=False
                )
            except Exception as ws_error:
                logger.warning(f"⚠️ WebSocket broadcast failed: {ws_error}")

            # Step 6: Trigger webhook callback (async, non-blocking)
            webhook_triggered = False
            try:
                from app.services.webhook_callback_service import WebhookCallbackService

                webhook_service = WebhookCallbackService()

                # Trigger webhook in background (don't wait for it)
                asyncio.create_task(
                    webhook_service.send_callback(
                        chat=chat,
                        message_content=ai_response,
                        supabase=self.supabase
                    )
                )

                webhook_triggered = True
                logger.info(f"📡 Webhook callback triggered for chat {chat_id}")

            except Exception as e:
                logger.warning(f"⚠️  Failed to trigger webhook callback: {e}")
                # Don't fail the whole process if webhook fails

            return {
                "success": True,
                "ai_message_id": ai_message_id,
                "webhook_triggered": webhook_triggered,
                "chat_id": chat_id
            }

        except Exception as e:
            logger.error(f"❌ Error in AI response processing for chat {chat_id}: {e}")
            return {
                "success": False,
                "reason": "processing_error",
                "error": str(e)
            }


# Background task wrapper for async processing
async def process_ai_response_async(
    chat_id: str,
    customer_message_id: str,
    supabase
):
    """
    Background task wrapper for processing AI response.

    This function is meant to be called via asyncio.create_task()
    to process AI response in the background without blocking
    the webhook response.

    Args:
        chat_id: Chat UUID
        customer_message_id: Customer message UUID
        supabase: Supabase client instance

    Example:
        asyncio.create_task(
            process_ai_response_async(
                chat_id="chat-uuid",
                customer_message_id="msg-uuid",
                supabase=supabase
            )
        )
    """
    try:
        logger.info(f"🔄 Background AI task started for chat: {chat_id}")

        service = AIResponseService(supabase)
        result = await service.process_and_respond(
            chat_id=chat_id,
            customer_message_id=customer_message_id
        )

        if result["success"]:
            logger.info(
                f"✅ Background AI task completed successfully for chat {chat_id}: "
                f"message_id={result.get('ai_message_id')}"
            )
        else:
            logger.warning(
                f"⚠️  Background AI task failed for chat {chat_id}: "
                f"reason={result.get('reason')}"
            )

    except Exception as e:
        logger.error(f"❌ Background AI task error for chat {chat_id}: {e}")


# Singleton instance getter
_ai_response_service: Optional[AIResponseService] = None


def get_ai_response_service(supabase) -> AIResponseService:
    """
    Get or create AIResponseService instance.

    Args:
        supabase: Supabase client instance

    Returns:
        AIResponseService instance
    """
    global _ai_response_service
    if _ai_response_service is None:
        _ai_response_service = AIResponseService(supabase)
    return _ai_response_service
