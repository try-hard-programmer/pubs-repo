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

    async def send_whatsapp_callback(self, chat: Dict[str, Any], message_content: str, supabase) -> Dict[str, Any]:
        """Send webhook callback to WhatsApp service."""
        try:
            logger.info(f"📱 Sending WhatsApp message for chat: {chat['id']}")

            customer_response = supabase.table("customers").select("phone").eq("id", chat["customer_id"]).execute()

            if not customer_response.data:
                raise Exception(f"Customer {chat['customer_id']} not found")

            raw_phone = customer_response.data[0].get("phone", "")

            if not raw_phone:
                raise Exception(f"Customer {chat['customer_id']} has no phone number")

            normalized_phone = self._normalize_phone_number(raw_phone)
            chat_id = self._format_whatsapp_chat_id(normalized_phone)

            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            if not sender_agent_id:
                raise Exception(f"No sender_agent_id or ai_agent_id for chat {chat['id']}")

            payload = {
                "chatId": chat_id,
                "contentType": "string",
                "content": message_content
            }

            base_url = settings.WHATSAPP_API_URL
            if not base_url:
                logger.warning("WHATSAPP_API_URL not configured in settings")
                return {"success": False, "reason": "api_url_not_configured", "channel": "whatsapp"}

            base_url = base_url.rstrip("/")
            endpoint_url = f"{base_url}/client/sendMessage/{sender_agent_id}"

            logger.info(f"📤 Sending WhatsApp message to: {endpoint_url}")
            
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "AIgent-CRM/1.0",
                "x-api-key": os.getenv("WHATSAPP_API_KEY")
            }
            if settings.WHATSAPP_API_KEY:
                headers["Authorization"] = f"Bearer {settings.WHATSAPP_API_KEY}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                response.raise_for_status()
                response_data = response.json() if response.text else {}

                return {
                    "success": True, 
                    "channel": "whatsapp", 
                    "chat_id": chat_id, 
                    "session_id": sender_agent_id, 
                    "response": response_data
                }

        except Exception as e:
            logger.error(f"❌ WhatsApp message send failed: {e}")
            return {"success": False, "reason": "error", "error": str(e)}
    
    async def send_telegram_callback(
        self,
        chat: Dict[str, Any],
        message_content: str,
        supabase,
        customer_id: str = None
    ) -> Dict[str, Any]:
        """Send webhook callback to Telegram."""
        try:
            logger.info(f"✈️ Processing Telegram callback for chat: {chat['id']}")

            # 1. Get Customer Details
            customer_response = supabase.table("customers").select("metadata, phone").eq("id", chat["customer_id"]).execute()
            
            if not customer_response.data:
                raise Exception(f"Customer {chat['customer_id']} not found")

            customer_data = customer_response.data[0]
            customer_metadata = customer_data.get("metadata", {}) or {}
            
            telegram_id = customer_metadata.get("telegram_id")
            raw_phone = customer_data.get("phone")

            logger.info(f"👤 Resolved Identity - TelegramID: {telegram_id}, Phone: {raw_phone}")

            # 2. Get Agent Config
            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            integration_response = supabase.table("agent_integrations").select("config").eq("agent_id", sender_agent_id).eq("channel", "telegram").eq("enabled", True).execute()

            if not integration_response.data:
                raise Exception(f"No Telegram integration found for agent {sender_agent_id}")

            integration_config = integration_response.data[0].get("config", {})
            bot_token = integration_config.get("botToken")

            # 3. Decision Logic
            if not bot_token:
                # Userbot Mode
                target_id = telegram_id
                
                if not target_id:
                    if raw_phone:
                        clean_phone = self._normalize_phone_number(raw_phone)
                        target_id = f"+{clean_phone}"
                        logger.info(f"🔄 Fallback: Using formatted phone number as target: {target_id}")
                
                if not target_id:
                    raise Exception(f"Customer {chat['customer_id']} missing telegram_id and phone.")

                # [FIX] Pass chat_id to enable merging logic
                return await self.send_telegram_userbot_callback(
                    agent_id=sender_agent_id,
                    telegram_id=target_id, 
                    message_content=message_content,
                    supabase=supabase,
                    customer_id=chat['customer_id'],
                    current_metadata=customer_metadata,
                    chat_id=chat['id'] 
                )

            # Standard Bot Mode
            if not telegram_id:
                raise Exception(f"Customer {chat['customer_id']} has no telegram_id.")

            payload = {
                "bot_token": bot_token,
                "telegram_id": telegram_id,
                "message": message_content,
                "metadata": {"chat_id": chat["id"], "timestamp": datetime.utcnow().isoformat()}
            }

            webhook_url = settings.TELEGRAM_WEBHOOK_URL
            if not webhook_url:
                if settings.TELEGRAM_API_URL:
                     # Fallback to Userbot
                     target_id = telegram_id or (f"+{self._normalize_phone_number(raw_phone)}" if raw_phone else None)
                     if target_id:
                         return await self.send_telegram_userbot_callback(
                            agent_id=sender_agent_id,
                            telegram_id=target_id,
                            message_content=message_content,
                            supabase=supabase,
                            customer_id=chat['customer_id'],
                            current_metadata=customer_metadata,
                            chat_id=chat['id']
                        )
                return {"success": False, "reason": "webhook_url_not_configured"}

            logger.info(f"📤 Sending to Standard Bot Webhook: {webhook_url}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(webhook_url, json=payload, headers={"Content-Type": "application/json"})
                response.raise_for_status()
                return {"success": True, "channel": "telegram", "response": response.json()}

        except Exception as e:
            logger.error(f"❌ Telegram callback failed: {e}")
            return {"success": False, "reason": "error", "error": str(e)}
    
    async def send_telegram_userbot_callback(
        self, 
        agent_id: str, 
        telegram_id: str, 
        message_content: str,
        supabase=None,
        customer_id: str=None,
        current_metadata: dict=None,
        chat_id: str=None
    ) -> Dict[str, Any]:
        """Send message via the Python Userbot Worker."""
        try:
            base_url = settings.TELEGRAM_API_URL
            if not base_url: raise Exception("TELEGRAM_API_URL missing")
            
            base_url = base_url.rstrip("/")
            if base_url.endswith("/api"): endpoint_url = f"{base_url}/webhook/send"
            else: endpoint_url = f"{base_url}/api/webhook/send"
            
            payload = {"agent_id": agent_id, "chat_id": telegram_id, "text": message_content}
            secret = settings.TELEGRAM_SECRET_KEY_SERVICE
            headers = {"Content-Type": "application/json", "X-Service-Key": secret}

            logger.info(f"🚀 Dispatching to Userbot Worker: {endpoint_url}")

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                
                if response.status_code != 200:
                    logger.error(f"⚠️ Worker Error: {response.text}")
                    return {"success": False, "reason": "worker_error", "error": response.text}

                resp_json = response.json()

                # [FIX: DUPLICATE PREVENTION & MERGE STRATEGY]
                if resp_json.get("status") == "success" and supabase and customer_id:
                    resolved_id = str(resp_json.get("resolved_chat_id"))
                    
                    if resolved_id:
                        # 1. Fetch current customer details to get Org ID
                        curr_cust = supabase.table("customers").select("organization_id, phone").eq("id", customer_id).single().execute()
                        
                        if curr_cust.data:
                            org_id = curr_cust.data["organization_id"]
                            curr_phone = curr_cust.data["phone"]

                            # 2. Check for DUPLICATE (Another customer with same Telegram ID)
                            # Exclude current ID
                            ghost_match = supabase.table("customers") \
                                .select("id") \
                                .eq("organization_id", org_id) \
                                .neq("id", customer_id) \
                                .contains("metadata", {"telegram_id": resolved_id}) \
                                .execute()

                            if ghost_match.data:
                                # DUPLICATE FOUND! MERGE TIME.
                                ghost_id = ghost_match.data[0]["id"]
                                logger.info(f"⚡ Merge Triggered: Merging Temp Customer {customer_id} into Existing {ghost_id}")

                                # A. Update Existing (Ghost) with Phone if missing
                                supabase.table("customers").update({
                                    "phone": curr_phone, 
                                    "updated_at": datetime.utcnow().isoformat()
                                }).eq("id", ghost_id).execute()

                                # B. Move Chat to Existing Customer
                                if chat_id:
                                    supabase.table("chats").update({"customer_id": ghost_id}).eq("id", chat_id).execute()

                                # C. Delete Temp Customer
                                supabase.table("customers").delete().eq("id", customer_id).execute()

                                # Return the NEW ID so calling functions can update their references
                                return {
                                    "success": True, 
                                    "channel": "telegram_userbot", 
                                    "response": resp_json, 
                                    "merged_customer_id": ghost_id
                                }

                            # 3. NO DUPLICATE - Just Update Metadata
                            elif current_metadata.get("telegram_id") != resolved_id:
                                logger.info(f"🔗 Linking Customer {customer_id} to Telegram ID {resolved_id}")
                                new_metadata = current_metadata.copy()
                                new_metadata["telegram_id"] = resolved_id
                                supabase.table("customers").update({"metadata": new_metadata}).eq("id", customer_id).execute()

                return {"success": True, "channel": "telegram_userbot", "response": resp_json}

        except Exception as e:
            logger.error(f"❌ Userbot dispatch failed: {e}")
            raise e            
    
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
