"""
Webhook Callback Service
Sends webhook callbacks to external WhatsApp/Telegram/Email services
"""
import logging
import os

import httpx
import re
import json
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
        [FIX] Improved detection: Long numbers (>15 digits) are Groups.
        """
        # 1. Trust existing suffixes
        if "@" in phone:
            return phone

        # 2. [FIX] Heuristic: Group IDs are usually long (18+ digits)
        if len(phone) > 15:
            return f"{phone}@g.us"

        # 3. Standard Regex for Country Codes
        country_code_pattern = r'^(1|7|2[0-7]|3[0-9]|4[0-4]|4[6-9]|5[1-8]|6[0-6]|8[1-6]|9[0-8])'

        if re.match(country_code_pattern, phone):
            return f"{phone}@c.us"
        else:
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
    
    # [CRITICAL FIX] Added media_url argument
    async def send_callback(self, chat: Dict[str, Any], message_content: str, supabase, media_url: Optional[str] = None) -> Dict[str, Any]:
        chat = await self._ensure_chat_data(chat, supabase)
        channel = chat.get("channel")

        try:
            if channel == "whatsapp":
                return await self.send_whatsapp_callback(chat, message_content, supabase)
            elif channel == "telegram":
                # [CRITICAL FIX] Passing media_url down
                return await self.send_telegram_callback(chat, message_content, supabase, media_url)
            elif channel == "email":
                return {"success": False, "reason": "email_not_implemented"}
            else:
                return {"success": False, "reason": "unsupported_channel"}
        except Exception as e:
            logger.error(f"Error in send_callback: {e}")
            return {"success": False, "reason": "error", "error": str(e)}          

    async def send_whatsapp_callback(self, chat: Dict[str, Any], message_content: str, supabase) -> Dict[str, Any]:
        """Send webhook callback to WhatsApp service with Group Mention Support."""
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

            # [FIX] Force @g.us if metadata or name indicates it's a group
            # This prevents the "Ghost DM" bug if the regex failed.
            chat_meta = chat.get("metadata", {}) or {}
            is_known_group = chat_meta.get("is_group") or "group" in str(chat.get("name", "")).lower()
            
            if is_known_group:
                if "@c.us" in chat_id:
                    chat_id = chat_id.replace("@c.us", "@g.us")
                elif "@" not in chat_id:
                    chat_id = f"{normalized_phone}@g.us"

            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            
            # Base Payload
            payload = {
                "chatId": chat_id, 
                "contentType": "string", 
                "content": message_content
            }

            # [FIX] GROUP MENTION LOGIC
            if "@g.us" in chat_id:
                try:
                    # Fetch the very last incoming message from the CUSTOMER
                    last_msg = supabase.table("messages") \
                        .select("metadata") \
                        .eq("chat_id", chat["id"]) \
                        .eq("sender_type", "customer") \
                        .order("created_at", desc=True) \
                        .limit(1) \
                        .execute()

                    if last_msg.data:
                        meta = last_msg.data[0].get("metadata", {})
                        
                        # [FIX] Prioritize 'original_sender_id' (Full ID) over 'group_participant'
                        participant = meta.get("original_sender_id") or meta.get("group_participant")
                        
                        if participant:
                            # [CRITICAL] Ensure suffix exists. API requires '123@c.us', not just '123'
                            if "@" not in participant:
                                participant = f"{participant}@c.us"

                            # Prepend "@User" to the text for highlighting
                            user_tag = participant.split('@')[0]
                            payload["content"] = f"@{user_tag} {message_content}"
                            
                            # Add to payload options
                            payload["options"] = { "mentions": [participant] }
                            logger.info(f"🏷️  Auto-Mentioning Participant: {participant}")
                except Exception as ex:
                    logger.warning(f"⚠️ Failed to resolve mention for group: {ex}")

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
        
    # [CRITICAL FIX] Added media_url argument AND Mention Logic
    async def send_telegram_callback(self, chat: Dict[str, Any], message_content: str, supabase, media_url: Optional[str] = None) -> Dict[str, Any]:
        try:
            logger.info(f"✈️ Processing Telegram callback for chat: {chat['id']}")
            
            customer_id = chat.get("customer_id")
            cust_res = supabase.table("customers").select("metadata, phone").eq("id", customer_id).execute()
            if not cust_res.data: raise Exception("Customer not found")

            customer_data = cust_res.data[0]
            telegram_id = customer_data.get("metadata", {}).get("telegram_id")
            raw_phone = customer_data.get("phone")
            
            target_id = telegram_id
            if not target_id and raw_phone:
                target_id = f"+{self._normalize_phone_number(raw_phone)}"
            
            if not target_id: raise Exception("No Target ID found")

            # ==============================================================================
            # [START] GROUP MENTION LOGIC (The Missing Piece)
            # ==============================================================================
            if str(target_id).startswith("-"):
                logger.info(f"🔍 Group Detected ({target_id}). Finding user to mention...")
                try:
                    # 1. Find the last message sent by a CUSTOMER in this chat
                    last_msg_res = supabase.table("messages") \
                        .select("metadata") \
                        .eq("chat_id", chat["id"]) \
                        .eq("sender_type", "customer") \
                        .order("created_at", desc=True) \
                        .limit(1) \
                        .execute()

                    if last_msg_res.data:
                        meta = last_msg_res.data[0].get("metadata", {})
                        
                        # 2. Extract the specific User ID to tag
                        # We check 'telegram_sender_id' (set by webhook.py) or fallbacks
                        user_id_to_tag = meta.get("telegram_sender_id") or \
                                         meta.get("participant") or \
                                         meta.get("sender_id")

                        if user_id_to_tag:
                            clean_uid = str(user_id_to_tag).split('@')[0]
                            display_name = meta.get("sender_display_name") or "User"
                            
                            # 3. Format: [Name](tg://user?id=123456)
                            mention_string = f"[{display_name}](tg://user?id={clean_uid})"
                            
                            # 4. Prepend to the message
                            message_content = f"{mention_string} {message_content}"
                            logger.info(f"✅ Auto-Mention Applied: {mention_string}")
                        else:
                            logger.warning(f"⚠️ Found message but no ID to tag. Meta keys: {meta.keys()}")
                    else:
                        logger.warning("⚠️ No customer message found to reply to.")

                except Exception as ex:
                    logger.error(f"❌ Mention Logic Failed: {ex}", exc_info=True)
            # ==============================================================================
            # [END] GROUP MENTION LOGIC
            # ==============================================================================

            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            
            # Userbot Logic
            return await self.send_telegram_userbot_callback(
                agent_id=sender_agent_id, 
                telegram_id=target_id, 
                message_content=message_content,
                media_url=media_url
            )

        except Exception as e:
            logger.error(f"❌ Telegram callback failed: {e}")
            return {"success": False, "error": str(e)}
        
    # [CRITICAL FIX] Added media_url argument and payload inclusion
    async def send_telegram_userbot_callback(self, agent_id: str, telegram_id: str, message_content: str, media_url: Optional[str] = None) -> Dict[str, Any]:
        try:
            base_url = settings.TELEGRAM_API_URL
            if not base_url: raise Exception("TELEGRAM_API_URL missing")
            
            endpoint_url = f"{base_url.rstrip('/')}/api/webhook/send".replace("/api/api/", "/api/")

            # Payload includes media_url now
            payload = {
                "agent_id": agent_id, 
                "chat_id": telegram_id, 
                "text": message_content,
                "media_url": media_url 
            }
            
            headers = {"Content-Type": "application/json", "X-Service-Key": settings.TELEGRAM_SECRET_KEY_SERVICE}
            
            logger.info(f"🚀 Dispatching to Userbot Worker: {endpoint_url} (Media: {bool(media_url)})")

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)
                if response.status_code != 200:
                    logger.error(f"⚠️ Worker Error: {response.text}")
                    return {"success": False, "error": response.text}
                return {"success": True, "response": response.json()}

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
