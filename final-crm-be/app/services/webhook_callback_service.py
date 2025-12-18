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
        """
        # FIX: If the phone number already contains an '@' (like @lid, @g.us, or existing @c.us),
        # return it as is. Do not append another suffix.
        if "@" in phone:
            return phone

        # Common country codes pattern (1-3 digits)
        country_code_pattern = r'^(1|7|2[0-7]|3[0-9]|4[0-4]|4[6-9]|5[1-8]|6[0-6]|8[1-6]|9[0-8])'

        if re.match(country_code_pattern, phone):
            # Personal chat
            return f"{phone}@c.us"
        else:
            # Group chat (fallback if no suffix found and doesn't look like international number)
            return f"{phone}@g.us"
        
    async def _ensure_chat_data(self, chat: Dict[str, Any], supabase) -> Dict[str, Any]:
        """
        Helper to ensure chat object has customer_id and agent_id.
        If missing, fetches full chat from DB.
        """
        if not chat.get("customer_id") or (not chat.get("sender_agent_id") and not chat.get("ai_agent_id")):
            logger.warning(f"⚠️ Partial chat object detected. Fetching full chat {chat.get('id')}...")
            try:
                res = supabase.table("chats").select("*").eq("id", chat["id"]).single().execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.error(f"❌ Failed to refetch chat data: {e}")
        return chat
    
    async def send_callback(self, chat: Dict[str, Any], message_content: str, supabase) -> Dict[str, Any]:
        """Send webhook callback based on chat channel."""
        
        # [FIX] Now this function exists
        chat = await self._ensure_chat_data(chat, supabase)
        
        channel = chat.get("channel")

        try:
            if channel == "whatsapp":
                return await self.send_whatsapp_callback(chat, message_content, supabase)
            elif channel == "telegram":
                return await self.send_telegram_callback(chat, message_content, supabase)
            elif channel == "email":
                # Placeholder for email
                return {"success": False, "reason": "email_not_implemented", "channel": channel}
            else:
                logger.warning(f"Unsupported channel for webhook: {channel}")
                return {"success": False, "reason": "unsupported_channel", "channel": channel}

        except Exception as e:
            logger.error(f"Error in send_callback for channel {channel}: {e}")
            return {"success": False, "reason": "callback_error", "error": str(e)}
        
    async def send_whatsapp_callback(self, chat: Dict[str, Any], message_content: str, supabase) -> Dict[str, Any]:
        """Send webhook callback to WhatsApp service."""
        try:
            logger.info(f"📱 Sending WhatsApp message for chat: {chat['id']}")
            
            customer_id = chat.get("customer_id")
            if not customer_id: raise Exception("Missing customer_id")

            customer_response = supabase.table("customers").select("phone").eq("id", customer_id).execute()
            if not customer_response.data: raise Exception(f"Customer {customer_id} not found")

            raw_phone = customer_response.data[0].get("phone", "")
            if not raw_phone: raise Exception(f"Customer {customer_id} has no phone number")

            normalized_phone = self._normalize_phone_number(raw_phone)
            chat_id = self._format_whatsapp_chat_id(normalized_phone)

            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            
            payload = {"chatId": chat_id, "contentType": "string", "content": message_content}
            base_url = settings.WHATSAPP_API_URL
            
            if not base_url: return {"success": False, "reason": "api_url_not_configured"}

            endpoint_url = f"{base_url.rstrip('/')}/client/sendMessage/{sender_agent_id}"
            
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "AIgent-CRM/1.0",
                "x-api-key": os.getenv("WHATSAPP_API_KEY")
            }
            if settings.WHATSAPP_API_KEY: headers["Authorization"] = f"Bearer {settings.WHATSAPP_API_KEY}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                response.raise_for_status()
                return {"success": True, "channel": "whatsapp", "response": response.json()}

        except Exception as e:
            logger.error(f"❌ WhatsApp message send failed: {e}")
            return {"success": False, "reason": "error", "error": str(e)}
    
    async def send_telegram_callback(self, chat: Dict[str, Any], message_content: str, supabase) -> Dict[str, Any]:
        """Send webhook callback to Telegram."""
        try:
            logger.info(f"✈️ Processing Telegram callback for chat: {chat['id']}")

            customer_id = chat.get("customer_id")
            if not customer_id: raise Exception("Missing customer_id in chat object")

            # 1. Get Customer Details
            cust_res = supabase.table("customers").select("metadata, phone").eq("id", customer_id).execute()
            if not cust_res.data: raise Exception(f"Customer {customer_id} not found")

            customer_data = cust_res.data[0]
            customer_metadata = customer_data.get("metadata", {}) or {}
            telegram_id = customer_metadata.get("telegram_id")
            raw_phone = customer_data.get("phone")

            logger.info(f"👤 Resolved Identity - TelegramID: {telegram_id}, Phone: {raw_phone}")

            # 2. Get Agent Config
            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            int_res = supabase.table("agent_integrations").select("config").eq("agent_id", sender_agent_id).eq("channel", "telegram").eq("enabled", True).execute()

            bot_token = None
            if int_res.data:
                bot_token = int_res.data[0].get("config", {}).get("botToken")

            # 3. Decision Logic: Userbot vs Bot
            use_userbot = not bot_token
            
            # Determine Target ID
            target_id = telegram_id
            if not target_id and raw_phone:
                target_id = f"+{self._normalize_phone_number(raw_phone)}"
            
            if not target_id:
                raise Exception("Customer missing both Telegram ID and Phone number")

            # A. Userbot Flow
            if use_userbot:
                return await self.send_telegram_userbot_callback(
                    agent_id=sender_agent_id, telegram_id=target_id, message_content=message_content,
                    supabase=supabase, customer_id=customer_id, current_metadata=customer_metadata, chat_id=chat['id']
                )

            # B. Standard Bot Flow
            webhook_url = settings.TELEGRAM_WEBHOOK_URL
            if not webhook_url:
                if settings.TELEGRAM_API_URL:
                     return await self.send_telegram_userbot_callback(
                        agent_id=sender_agent_id, telegram_id=target_id, message_content=message_content,
                        supabase=supabase, customer_id=customer_id, current_metadata=customer_metadata, chat_id=chat['id']
                    )
                return {"success": False, "reason": "webhook_url_not_configured"}

            payload = {
                "bot_token": bot_token,
                "telegram_id": telegram_id,
                "message": message_content,
                "metadata": {"chat_id": chat["id"], "timestamp": datetime.utcnow().isoformat()}
            }

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(webhook_url, json=payload, headers={"Content-Type": "application/json"})
                response.raise_for_status()
                return {"success": True, "channel": "telegram", "response": response.json()}

        except Exception as e:
            logger.error(f"❌ Telegram callback failed: {e}")
            return {"success": False, "reason": "error", "error": str(e)}
    
    async def send_telegram_userbot_callback(
        self, agent_id: str, telegram_id: str, message_content: str,
        supabase=None, customer_id: str=None, current_metadata: dict=None, chat_id: str=None
    ) -> Dict[str, Any]:
        """Send message via the Python Userbot Worker."""
        try:
            base_url = settings.TELEGRAM_API_URL
            if not base_url: raise Exception("TELEGRAM_API_URL missing")
            
            endpoint_url = f"{base_url.rstrip('/')}/api/webhook/send"
            if "/api/api" in endpoint_url: endpoint_url = endpoint_url.replace("/api/api", "/api")

            payload = {"agent_id": agent_id, "chat_id": telegram_id, "text": message_content}
            headers = {"Content-Type": "application/json", "X-Service-Key": settings.TELEGRAM_SECRET_KEY_SERVICE}

            logger.info(f"🚀 Dispatching to Userbot Worker: {endpoint_url}")

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                
                if response.status_code != 200:
                    logger.error(f"⚠️ Worker Error: {response.text}")
                    return {"success": False, "reason": "worker_error", "error": response.text}

                resp_json = response.json()

                # [MERGE LOGIC]
                if resp_json.get("status") == "success" and supabase and customer_id:
                    resolved_id = str(resp_json.get("resolved_chat_id"))
                    
                    if resolved_id and resolved_id != "None":
                        curr_cust = supabase.table("customers").select("organization_id, phone").eq("id", customer_id).single().execute()
                        if curr_cust.data:
                            org_id = curr_cust.data["organization_id"]
                            curr_phone = curr_cust.data["phone"]

                            # Check for DUPLICATE
                            ghost_match = supabase.table("customers").select("id") \
                                .eq("organization_id", org_id).neq("id", customer_id) \
                                .contains("metadata", {"telegram_id": resolved_id}).execute()

                            if ghost_match.data:
                                ghost_id = ghost_match.data[0]["id"]
                                logger.info(f"⚡ Merge Triggered: {customer_id} -> {ghost_id}")
                                supabase.table("customers").update({"phone": curr_phone, "updated_at": datetime.utcnow().isoformat()}).eq("id", ghost_id).execute()
                                if chat_id: supabase.table("chats").update({"customer_id": ghost_id}).eq("id", chat_id).execute()
                                supabase.table("customers").delete().eq("id", customer_id).execute()
                                return {"success": True, "channel": "telegram_userbot", "response": resp_json, "merged_customer_id": ghost_id}

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
