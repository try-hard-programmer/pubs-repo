"""
Message Service
Centralized service for sending messages via different channels (WhatsApp, Telegram, Email)
Handles sender agent lookup to ensure consistent sender number/bot/email
"""
import logging
from typing import Optional, Dict, Any
from app.services.whatsapp_service import get_whatsapp_service

logger = logging.getLogger(__name__)


class MessageService:
    """Service for sending messages via different communication channels"""

    def __init__(self, supabase):
        """
        Initialize Message Service

        Args:
            supabase: Supabase client instance
        """
        self.supabase = supabase

    async def send_whatsapp_reply(
        self,
        chat_id: str,
        message_content: str,
        customer_phone: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send WhatsApp reply using correct agent integration.

        This ensures that replies are sent from the SAME WhatsApp number
        that the customer originally contacted, even if the chat has been
        escalated from AI agent to Human agent.

        Flow:
        1. Get chat data (sender_agent_id, customer_id)
        2. Lookup agent_integrations by sender_agent_id for WhatsApp channel
        3. Get WhatsApp number from integration config
        4. Get customer phone if not provided
        5. Send via WhatsApp service using sender_agent_id as session_id

        Args:
            chat_id: Chat UUID
            message_content: Message text content
            customer_phone: Optional customer phone (will be fetched if not provided)

        Returns:
            Send result with success status and details

        Raises:
            Exception: If sending fails

        Example:
            # Customer contacted +0811111 (AI Agent-A)
            # Chat escalated to Human Agent-B
            # When Human Agent-B replies:
            message_service = get_message_service(supabase)
            result = await message_service.send_whatsapp_reply(
                chat_id="chat-uuid",
                message_content="Hello from human agent"
            )
            # Message sent from +0811111 (Agent-A's number, not Agent-B's)
        """
        try:
            logger.info(f"📤 Sending WhatsApp reply for chat: {chat_id}")

            # Step 1: Get chat data
            chat_response = self.supabase.table("chats") \
                .select("sender_agent_id, customer_id, channel") \
                .eq("id", chat_id) \
                .execute()

            if not chat_response.data:
                raise Exception(f"Chat {chat_id} not found")

            chat = chat_response.data[0]
            sender_agent_id = chat.get("sender_agent_id")
            customer_id = chat.get("customer_id")
            channel = chat.get("channel")

            if not sender_agent_id:
                raise Exception(
                    f"Chat {chat_id} has no sender_agent_id. "
                    "This is required to determine which WhatsApp number to use."
                )

            if channel != "whatsapp":
                raise Exception(
                    f"Chat {chat_id} is not a WhatsApp chat (channel={channel}). "
                    "Use appropriate send method for this channel."
                )

            logger.info(f"✅ Chat found: sender_agent={sender_agent_id}, customer={customer_id}")

            # Step 2: Get agent integration for WhatsApp
            integration_response = self.supabase.table("agent_integrations") \
                .select("config, status") \
                .eq("agent_id", sender_agent_id) \
                .eq("channel", "whatsapp") \
                .eq("enabled", True) \
                .execute()

            if not integration_response.data:
                raise Exception(
                    f"No WhatsApp integration found for agent {sender_agent_id}. "
                    "Agent must have an active WhatsApp integration to send messages."
                )

            integration = integration_response.data[0]
            integration_config = integration.get("config", {})
            integration_status = integration.get("status")
            whatsapp_number = integration_config.get("phoneNumber")

            if not whatsapp_number:
                raise Exception(
                    f"WhatsApp integration for agent {sender_agent_id} "
                    "has no phoneNumber in config"
                )

            if integration_status != "connected":
                logger.warning(
                    f"WhatsApp integration for agent {sender_agent_id} "
                    f"status is '{integration_status}' (not 'connected')"
                )

            logger.info(
                f"✅ WhatsApp integration found: number={whatsapp_number}, "
                f"status={integration_status}"
            )

            # Step 3: Get customer phone if not provided
            if not customer_phone:
                customer_response = self.supabase.table("customers") \
                    .select("phone") \
                    .eq("id", customer_id) \
                    .execute()

                if not customer_response.data:
                    raise Exception(f"Customer {customer_id} not found")

                customer_phone = customer_response.data[0].get("phone")

            if not customer_phone:
                raise Exception(
                    f"Customer {customer_id} has no phone number. "
                    "Cannot send WhatsApp message."
                )

            # Normalize phone number (remove +)
            customer_phone = customer_phone.lstrip("+")

            logger.info(f"✅ Customer phone: {customer_phone}")

            # Step 4: Send via WhatsApp service
            # IMPORTANT: session_id = sender_agent_id
            # This ensures message is sent from the correct WhatsApp session
            session_id = sender_agent_id

            whatsapp_service = get_whatsapp_service()
            send_result = await whatsapp_service.send_text_message(
                session_id=session_id,
                phone_number=customer_phone,
                message=message_content
            )

            logger.info(
                f"✅ WhatsApp message sent successfully: "
                f"from={whatsapp_number} (session={session_id}), "
                f"to={customer_phone}, chat={chat_id}"
            )

            return {
                "success": True,
                "chat_id": chat_id,
                "sender_agent_id": sender_agent_id,
                "whatsapp_number": whatsapp_number,
                "customer_phone": customer_phone,
                "message_content": message_content,
                "send_result": send_result
            }

        except Exception as e:
            logger.error(f"❌ Failed to send WhatsApp reply for chat {chat_id}: {e}")
            raise

    async def send_telegram_reply(
        self,
        chat_id: str,
        message_content: str
    ) -> Dict[str, Any]:
        """
        Send Telegram reply using correct agent integration.

        Similar to send_whatsapp_reply but for Telegram.

        Args:
            chat_id: Chat UUID
            message_content: Message text content

        Returns:
            Send result

        Raises:
            Exception: If sending fails
        """
        # TODO: Implement Telegram sending logic
        # Similar pattern to WhatsApp:
        # 1. Get chat.sender_agent_id
        # 2. Lookup agent_integrations for Telegram
        # 3. Get bot_token from config
        # 4. Send via Telegram API
        raise NotImplementedError("Telegram sending not yet implemented")

    async def send_email_reply(
        self,
        chat_id: str,
        message_content: str,
        subject: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send Email reply using correct agent integration.

        Similar to send_whatsapp_reply but for Email.

        Args:
            chat_id: Chat UUID
            message_content: Message text content
            subject: Optional email subject

        Returns:
            Send result

        Raises:
            Exception: If sending fails
        """
        # TODO: Implement Email sending logic
        # Similar pattern to WhatsApp:
        # 1. Get chat.sender_agent_id
        # 2. Lookup agent_integrations for Email
        # 3. Get from_email from config
        # 4. Send via Email service (SMTP/SendGrid/etc)
        raise NotImplementedError("Email sending not yet implemented")


# Singleton instance
_message_service: Optional[MessageService] = None


def get_message_service(supabase) -> MessageService:
    """
    Get or create MessageService instance.

    Args:
        supabase: Supabase client instance

    Returns:
        MessageService instance
    """
    global _message_service
    if _message_service is None:
        _message_service = MessageService(supabase)
    return _message_service
