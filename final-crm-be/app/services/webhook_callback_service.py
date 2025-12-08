"""
Webhook Callback Service
Sends webhook callbacks to external WhatsApp/Telegram/Email services
"""
import logging
import os

import httpx
import re
from typing import Dict, Any, Optional
from datetime import datetime
from app.config.settings import settings

logger = logging.getLogger(__name__)


class WebhookCallbackService:
    """
    Service for sending webhook callbacks to external messaging services.

    This service sends AI/Human agent responses to external WhatsApp/Telegram/Email
    services via HTTP webhooks.

    Supported Channels:
    - WhatsApp: Sends to WHATSAPP_WEBHOOK_URL
    - Telegram: Sends to TELEGRAM_WEBHOOK_URL
    - Email: Sends to EMAIL_WEBHOOK_URL

    Payload Format:
    - WhatsApp: {"session_id": "...", "phone_number": "...", "message": "..."}
    - Telegram: {"bot_token": "...", "telegram_id": "...", "message": "..."}
    - Email: {"from_email": "...", "to_email": "...", "subject": "...", "message": "..."}
    """

    def __init__(self):
        """Initialize Webhook Callback Service"""
        self.timeout = 30.0  # 30 seconds timeout for webhook calls

    def _normalize_phone_number(self, phone: str) -> str:
        """
        Normalize phone number to international format without + sign.

        Handles various input formats:
        - +6281288888888 → 6281288888888
        - 081288888888 → 6281288888888
        - 6281288888888 → 6281288888888

        Args:
            phone: Phone number in any format

        Returns:
            Normalized phone number (international format without +)

        Example:
            _normalize_phone_number("+6281288888888") → "6281288888888"
            _normalize_phone_number("081288888888") → "6281288888888"
        """
        # Remove any whitespace
        phone = phone.strip()

        # Remove + if present
        if phone.startswith("+"):
            phone = phone[1:]

        # If starts with 0, replace with Indonesia country code 62
        if phone.startswith("0"):
            phone = "62" + phone[1:]

        return phone

    def _format_whatsapp_chat_id(self, phone: str) -> str:
        """
        Format phone number to WhatsApp chatId format.

        Personal chat: {phone}@c.us (phone starts with country code)
        Group chat: {phone}@g.us (phone doesn't start with country code)

        Common country codes: 62 (ID), 1 (US), 44 (UK), 91 (IN), 86 (CN), etc.

        Args:
            phone: Normalized phone number (international format)

        Returns:
            WhatsApp chatId (e.g., "6281288888888@c.us" or "120363xxx@g.us")

        Example:
            _format_whatsapp_chat_id("6281288888888") → "6281288888888@c.us"
            _format_whatsapp_chat_id("120363xxx") → "120363xxx@g.us"
        """
        # Common country codes pattern (1-3 digits)
        # 1 (US/CA), 7 (RU), 20 (EG), 27 (ZA), 30-49 (Europe), 60-66 (Asia), 81 (JP), 82 (KR), 86 (CN), 91 (IN), etc.
        country_code_pattern = r'^(1|7|2[0-7]|3[0-9]|4[0-4]|4[6-9]|5[1-8]|6[0-6]|8[1-6]|9[0-8])'

        if re.match(country_code_pattern, phone):
            # Personal chat
            return f"{phone}@c.us"
        else:
            # Group chat
            return f"{phone}@g.us"

    async def send_callback(
        self,
        chat: Dict[str, Any],
        message_content: str,
        supabase
    ) -> Dict[str, Any]:
        """
        Send webhook callback based on chat channel.

        Automatically routes to the appropriate channel-specific method.

        Args:
            chat: Chat data dict (must include channel, customer_id, sender_agent_id)
            message_content: Message text to send
            supabase: Supabase client for lookups

        Returns:
            Result dict: {"success": bool, "channel": str, "response": dict}
        """
        channel = chat.get("channel")

        try:
            if channel == "whatsapp":
                return await self.send_whatsapp_callback(chat, message_content, supabase)
            elif channel == "telegram":
                return await self.send_telegram_callback(chat, message_content, supabase)
            elif channel == "email":
                return await self.send_email_callback(chat, message_content, supabase)
            else:
                logger.warning(f"Unsupported channel for webhook: {channel}")
                return {
                    "success": False,
                    "reason": "unsupported_channel",
                    "channel": channel
                }

        except Exception as e:
            logger.error(f"Error in send_callback for channel {channel}: {e}")
            return {
                "success": False,
                "reason": "callback_error",
                "error": str(e)
            }

    async def send_whatsapp_callback(
        self,
        chat: Dict[str, Any],
        message_content: str,
        supabase
    ) -> Dict[str, Any]:
        """
        Send webhook callback to WhatsApp service.

        Uses WhatsApp service API endpoint:
        POST {WHATSAPP_API_URL}/client/sendMessage/{sessionId}

        Payload sent:
        {
            "chatId": "6281288888888@c.us",  // Personal chat
            "contentType": "string",
            "content": "Hello World!"
        }

        Args:
            chat: Chat data (must include sender_agent_id, customer_id)
            message_content: Message to send
            supabase: Supabase client

        Returns:
            {"success": bool, "channel": "whatsapp", "response": dict}
        """
        try:
            logger.info(f"📱 Sending WhatsApp message for chat: {chat['id']}")

            # Get customer phone number
            customer_response = supabase.table("customers") \
                .select("phone") \
                .eq("id", chat["customer_id"]) \
                .execute()

            print("☎️ customer_id : "+str(chat["customer_id"]))
            print("☎️ customer_response : "+str(customer_response))


            if not customer_response.data:
                raise Exception(f"Customer {chat['customer_id']} not found")

            raw_phone = customer_response.data[0].get("phone", "")

            if not raw_phone:
                raise Exception(f"Customer {chat['customer_id']} has no phone number")

            # Normalize phone number (remove +, handle 08xx)
            normalized_phone = self._normalize_phone_number(raw_phone)

            # Format to WhatsApp chatId (add @c.us or @g.us)
            chat_id = self._format_whatsapp_chat_id(normalized_phone)

            logger.info(f"📞 Phone: {raw_phone} → Normalized: {normalized_phone} → chatId: {chat_id}")

            # Get sender agent ID (use sender_agent_id if available, fallback to ai_agent_id)
            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")

            if not sender_agent_id:
                raise Exception(f"No sender_agent_id or ai_agent_id for chat {chat['id']}")

            # Prepare WhatsApp service payload
            payload = {
                "chatId": chat_id,
                "contentType": "string",
                "content": message_content
            }

            # Build endpoint URL
            base_url = settings.WHATSAPP_API_URL
            if not base_url:
                logger.warning("WHATSAPP_API_URL not configured in settings")
                return {
                    "success": False,
                    "reason": "api_url_not_configured",
                    "channel": "whatsapp"
                }

            # Remove trailing slash from base_url if present
            base_url = base_url.rstrip("/")

            # Build full endpoint: POST /client/sendMessage/{sessionId}
            endpoint_url = f"{base_url}/client/sendMessage/{sender_agent_id}"

            logger.info(f"📤 Sending WhatsApp message to: {endpoint_url}")
            logger.debug(f"Payload: {payload}")

            # Prepare headers
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "AIgent-CRM/1.0",
                "x-api-key": os.getenv("WHATSAPP_API_KEY")
            }

            # Add Authorization header if API key exists
            if settings.WHATSAPP_API_KEY:
                headers["Authorization"] = f"Bearer {settings.WHATSAPP_API_KEY}"

            print("PAYLOAD : "+str(payload))
            # Send message via HTTP POST
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    endpoint_url,
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()

                response_data = response.json() if response.text else {}

                logger.info(
                    f"✅ WhatsApp message sent successfully to {chat_id}: "
                    f"status={response.status_code}"
                )

                return {
                    "success": True,
                    "channel": "whatsapp",
                    "chat_id": chat_id,
                    "session_id": sender_agent_id,
                    "endpoint_url": endpoint_url,
                    "response": response_data
                }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"❌ WhatsApp API HTTP error: {e.response.status_code} - {e.response.text}"
            )
            return {
                "success": False,
                "reason": "http_error",
                "status_code": e.response.status_code,
                "error": e.response.text
            }
        except httpx.RequestError as e:
            logger.error(f"❌ WhatsApp API request error: {e}")
            return {
                "success": False,
                "reason": "request_error",
                "error": str(e)
            }
        except Exception as e:
            logger.error(f"❌ WhatsApp message send failed: {e}")
            return {
                "success": False,
                "reason": "unknown_error",
                "error": str(e)
            }

    async def send_telegram_callback(
        self,
        chat: Dict[str, Any],
        message_content: str,
        supabase
    ) -> Dict[str, Any]:
        """
        Send webhook callback to Telegram.
        Handles both Standard Bots (via Token) and Userbots (via Worker).
        """
        try:
            logger.info(f"✈️ Processing Telegram callback for chat: {chat['id']}")

            # 1. Get Customer Telegram ID
            customer_response = supabase.table("customers").select("metadata").eq("id", chat["customer_id"]).execute()
            if not customer_response.data:
                raise Exception(f"Customer {chat['customer_id']} not found")

            customer_metadata = customer_response.data[0].get("metadata", {})
            telegram_id = customer_metadata.get("telegram_id")
            if not telegram_id:
                raise Exception(f"Customer {chat['customer_id']} has no telegram_id")

            # 2. Get Agent Config
            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            integration_response = supabase.table("agent_integrations").select("config").eq("agent_id", sender_agent_id).eq("channel", "telegram").eq("enabled", True).execute()

            if not integration_response.data:
                raise Exception(f"No Telegram integration found for agent {sender_agent_id}")

            integration_config = integration_response.data[0].get("config", {})
            bot_token = integration_config.get("botToken")

            # --- 3. DECISION LOGIC: Userbot vs Standard Bot ---
            
            # If NO Token is found, assume it is a Userbot/Worker
            if not bot_token:
                logger.info(f"🤖 No botToken found. Switching to Userbot/Worker mode for agent {sender_agent_id}")
                return await self.send_telegram_userbot_callback(
                    agent_id=sender_agent_id,
                    telegram_id=telegram_id,
                    message_content=message_content
                )

            # If Token IS found, use Standard Bot API
            payload = {
                "bot_token": bot_token,
                "telegram_id": telegram_id,
                "message": message_content,
                "metadata": {"chat_id": chat["id"], "timestamp": datetime.utcnow().isoformat()}
            }

            webhook_url = settings.TELEGRAM_WEBHOOK_URL
            
            # Fallback check: If URL is missing, try worker
            if not webhook_url:
                if settings.TELEGRAM_API_URL:
                     logger.warning("TELEGRAM_WEBHOOK_URL missing, trying Userbot fallback...")
                     return await self.send_telegram_userbot_callback(
                        agent_id=sender_agent_id,
                        telegram_id=telegram_id,
                        message_content=message_content
                    )
                return {"success": False, "reason": "webhook_url_not_configured"}

            logger.info(f"📤 Sending to Standard Bot Webhook: {webhook_url}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    webhook_url, 
                    json=payload, 
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                return {"success": True, "channel": "telegram", "response": response.json()}

        except Exception as e:
            logger.error(f"❌ Telegram callback failed: {e}")
            return {"success": False, "reason": "error", "error": str(e)}
    
    async def send_email_callback(
        self,
        chat: Dict[str, Any],
        message_content: str,
        supabase
    ) -> Dict[str, Any]:
        """
        Send webhook callback to Email service.

        Payload sent to EMAIL_WEBHOOK_URL:
        {
            "from_email": "support@example.com",  // From agent_integrations
            "to_email": "customer@example.com",  // Customer email
            "subject": "Re: Your inquiry",
            "message": "AI response text",
            "metadata": {
                "chat_id": "chat-uuid",
                "timestamp": "2025-10-22T10:30:00Z"
            }
        }

        Args:
            chat: Chat data
            message_content: Message to send
            supabase: Supabase client

        Returns:
            {"success": bool, "channel": "email", "response": dict}
        """
        try:
            logger.info(f"📧 Sending Email webhook callback for chat: {chat['id']}")

            # Get customer email
            customer_response = supabase.table("customers") \
                .select("email") \
                .eq("id", chat["customer_id"]) \
                .execute()

            if not customer_response.data:
                raise Exception(f"Customer {chat['customer_id']} not found")

            customer_email = customer_response.data[0].get("email")

            if not customer_email:
                raise Exception(f"Customer {chat['customer_id']} has no email")

            # Get sender agent integration to get from_email
            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")

            integration_response = supabase.table("agent_integrations") \
                .select("config") \
                .eq("agent_id", sender_agent_id) \
                .eq("channel", "email") \
                .eq("enabled", True) \
                .execute()

            if not integration_response.data:
                raise Exception(
                    f"No Email integration found for agent {sender_agent_id}"
                )

            integration_config = integration_response.data[0].get("config", {})
            from_email = integration_config.get("email")

            if not from_email:
                raise Exception("Email integration has no email in config")

            # Prepare webhook payload
            payload = {
                "from_email": from_email,
                "to_email": customer_email,
                "subject": "Re: Your inquiry",  # TODO: Make subject dynamic
                "message": message_content,
                "metadata": {
                    "chat_id": chat["id"],
                    "timestamp": datetime.utcnow().isoformat()
                }
            }

            # Get webhook URL from settings
            webhook_url = settings.EMAIL_WEBHOOK_URL

            if not webhook_url:
                logger.warning("EMAIL_WEBHOOK_URL not configured in settings")
                return {
                    "success": False,
                    "reason": "webhook_url_not_configured",
                    "channel": "email"
                }

            logger.info(f"📤 Sending Email webhook to: {webhook_url}")

            # Send webhook via HTTP POST
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    webhook_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "AIgent-CRM/1.0"
                    }
                )
                response.raise_for_status()

                response_data = response.json() if response.text else {}

                logger.info(
                    f"✅ Email webhook sent successfully to {customer_email}: "
                    f"status={response.status_code}"
                )

                return {
                    "success": True,
                    "channel": "email",
                    "to_email": customer_email,
                    "webhook_url": webhook_url,
                    "response": response_data
                }

        except Exception as e:
            logger.error(f"❌ Email webhook failed: {e}")
            return {
                "success": False,
                "reason": "error",
                "error": str(e)
            }

    async def send_telegram_userbot_callback(self, agent_id: str, telegram_id: str, message_content: str) -> Dict[str, Any]:
        """
        Send message via the Python Userbot Worker.
        Target: {TELEGRAM_API_URL}/webhook/send
        """
        try:
            # 1. Get Base URL
            base_url = settings.TELEGRAM_API_URL
            if not base_url:
                raise Exception("TELEGRAM_API_URL is not configured in .env")
            
            # Clean the URL
            base_url = base_url.rstrip("/")
            
            # 2. Construct Endpoint (Fixing the 404 double /api issue)
            # If TELEGRAM_API_URL ends in /api, we just add /webhook/send
            # If TELEGRAM_API_URL is root, we add /api/webhook/send
            if base_url.endswith("/api"):
                endpoint_url = f"{base_url}/webhook/send"
            else:
                endpoint_url = f"{base_url}/api/webhook/send"
            
            payload = {
                "agent_id": agent_id,
                "chat_id": telegram_id,
                "text": message_content
            }
            
            # 3. FIX AUTH HEADER (Fixing the 403 Forbidden issue)
            # The Worker middleware expects "X-Service-Key", not "X-API-Key"
            secret = settings.TELEGRAM_SECRET_KEY_SERVICE
            
            headers = {
                "Content-Type": "application/json",
                "X-Service-Key": secret  # <--- CHANGED FROM X-API-Key
            }

            logger.info(f"📤 Sending to Userbot Worker: {endpoint_url}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                
                if response.status_code != 200:
                    logger.error(f"⚠️ Worker returned {response.status_code}: {response.text}")
                    return {"success": False, "reason": "worker_error", "error": response.text}

                return {"success": True, "channel": "telegram_userbot", "response": response.json()}

        except Exception as e:
            logger.error(f"❌ Userbot send failed: {e}")
            raise e

# Singleton instance getter
_webhook_callback_service: Optional[WebhookCallbackService] = None


def get_webhook_callback_service() -> WebhookCallbackService:
    """
    Get or create WebhookCallbackService singleton instance.

    Returns:
        WebhookCallbackService instance
    """
    global _webhook_callback_service
    if _webhook_callback_service is None:
        _webhook_callback_service = WebhookCallbackService()
    return _webhook_callback_service
