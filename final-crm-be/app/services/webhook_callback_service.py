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
        """Send webhook callback to WhatsApp service with Perfect Group Mention Support."""
        try:
            logger.info(f"📱 Sending WhatsApp message for chat: {chat['id']}")
            
            customer_id = chat.get("customer_id")
            if not customer_id: raise Exception("Missing customer_id")

            # 1. Standard Customer Phone Lookup
            customer_response = supabase.table("customers").select("phone, metadata").eq("id", customer_id).execute()
            if not customer_response.data: raise Exception(f"Customer {customer_id} not found")

            customer_data = customer_response.data[0]
            raw_phone = customer_data.get("phone", "")
            if not raw_phone: raise Exception(f"Customer {customer_id} has no phone number")

            normalized_phone = self._normalize_phone_number(raw_phone)
            chat_id = self._format_whatsapp_chat_id(normalized_phone)

            # [ROUTING] RETRIEVE TARGET GROUP ID FROM LATEST MESSAGE
            target_group_id = None
            try:
                last_inbound = supabase.table("messages") \
                    .select("metadata") \
                    .eq("chat_id", chat["id"]) \
                    .eq("sender_type", "customer") \
                    .order("created_at", desc=True) \
                    .limit(1) \
                    .execute()
                
                if last_inbound.data:
                    msg_meta = last_inbound.data[0].get("metadata", {}) or {}
                    target_group_id = msg_meta.get("target_group_id")
            except Exception as meta_err:
                logger.warning(f"⚠️ Failed to check message metadata for routing: {meta_err}")

            if target_group_id:
                logger.info(f"🎯 Rerouting Private Chat to Group: {target_group_id}")
                chat_id = target_group_id 
            else:
                # Fallback Group Detection
                chat_meta = chat.get("metadata", {}) or {}
                is_known_group = chat_meta.get("is_group") or "group" in str(chat.get("name", "")).lower()
                if is_known_group:
                    if "@c.us" in chat_id: chat_id = chat_id.replace("@c.us", "@g.us")
                    elif "@" not in chat_id: chat_id = f"{normalized_phone}@g.us"

            sender_agent_id = chat.get("sender_agent_id") or chat.get("ai_agent_id")
            
            # [CLEANING] Remove accidental mentions from AI response
            lid_matches = re.findall(r"@\s?(2\d{10,17})", message_content)
            clean_content = message_content
            was_cleaned = False
            
            for detected_lid in lid_matches:
                clean_content = re.sub(r"@\s?" + detected_lid, "", clean_content).strip()
                was_cleaned = True
            
            if was_cleaned:
                try:
                    # Update DB with clean text
                    latest_msg = supabase.table("messages")\
                        .select("id")\
                        .eq("chat_id", chat["id"])\
                        .order("created_at", desc=True)\
                        .limit(1)\
                        .execute()
                    if latest_msg.data:
                        msg_id = latest_msg.data[0]["id"]
                        supabase.table("messages").update({"content": clean_content}).eq("id", msg_id).execute()
                except Exception: pass
                message_content = clean_content

            payload = {
                "chatId": chat_id, 
                "contentType": "string", 
                "content": message_content
            }

            # [FIX 3] MENTION LOGIC RESTORED
            if "@g.us" in chat_id:
                try:
                    # Fetch Metadata from Last Message
                    last_msg = supabase.table("messages") \
                        .select("metadata") \
                        .eq("chat_id", chat["id"]) \
                        .eq("sender_type", "customer") \
                        .order("created_at", desc=True) \
                        .limit(1) \
                        .execute()

                    if last_msg.data:
                        meta = last_msg.data[0].get("metadata", {}) or {}
                        
                        real_number = meta.get("real_contact_number") or \
                                      meta.get("real_number") or \
                                      meta.get("original_sender_id") or \
                                      meta.get("group_participant")
                        
                        # 1. Clean the ID strictly
                        clean_check = str(real_number).replace("@lid", "").replace("@c.us", "").replace("+", "").replace(" ", "")
                        
                        # 2. Determine if it's a LID
                        is_likely_lid = (len(clean_check) >= 14) or "lid" in str(real_number)
                        if customer_data.get("metadata", {}).get("is_lid_user"):
                            is_likely_lid = True

                        # 3. DB Lookup (Try to upgrade LID -> Real Phone locally first)
                        if is_likely_lid:
                            try:
                                db_lookup = supabase.table("customers") \
                                    .select("phone") \
                                    .or_(f"phone.eq.{clean_check},metadata->>whatsapp_lid.eq.{clean_check}") \
                                    .limit(1) \
                                    .execute()
                                
                                if db_lookup.data:
                                    found_phone = db_lookup.data[0].get("phone")
                                    if found_phone and "g.us" not in found_phone and len(found_phone) < 14: 
                                        logger.info(f"✅ [TRACE] DB Lookup Success: {real_number} -> {found_phone}")
                                        real_number = found_phone
                                        is_likely_lid = False 
                            except Exception: pass

                        if real_number:
                            clean_number = str(real_number).split("@")[0].replace("+", "")
                            
                            # Construct Mention ID
                            # If it looks like a LID, keep @lid. Else @c.us
                            is_lid_format = (len(clean_number) >= 14) or "lid" in str(real_number)
                            
                            if is_lid_format:
                                mention_id = real_number if "@" in str(real_number) else f"{real_number}@lid"
                            else:
                                mention_id = f"{clean_number}@c.us"

                            # [FINAL STRATEGY] ALWAYS SEND THE ID
                            # We stop trying to be smart with Names. We send the ID.
                            # 1. If it's a Real Number, WA renders Blue Tag.
                            # 2. If it's a LID, WA renders Blue Tag (if saved) or Raw Number.
                            # Node.js service can intercept this @ID if it wants to swap it, but Python shouldn't hide it.
                            
                            payload["content"] = f"@{clean_number} {message_content}"

                            payload["options"] = {"mentions": [mention_id]}
                            logger.info(f"🏷️ Auto-Mention: {mention_id} Body: {payload['content'][:20]}...")
                            
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
                                                 
    # [CRITICAL FIX] Added media_url argument
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

            # [FIX] GROUP MENTION LOGIC (Telegram)
            # Telegram Group IDs start with "-" (e.g. -100123456789)
            if str(target_id).startswith("-"):
                try:
                    # 1. Fetch the LAST MESSAGE to know exactly who we are replying to
                    last_msg = supabase.table("messages") \
                        .select("metadata") \
                        .eq("chat_id", chat["id"]) \
                        .eq("sender_type", "customer") \
                        .order("created_at", desc=True) \
                        .limit(1) \
                        .execute()

                    if last_msg.data:
                        meta = last_msg.data[0].get("metadata", {})
                        
                        # Extract Telegram User Details (Saved by webhook.py)
                        sender_uid = meta.get("telegram_sender_id") or meta.get("participant")
                        sender_name = meta.get("sender_display_name") or "User"
                        
                        if sender_uid:
                            # 2. Construct Mention (Markdown Format)
                            # Telegram requires: [Name](tg://user?id=12345) to make it clickable/blue
                            mention_link = f"[{sender_name}](tg://user?id={sender_uid})"
                            
                            # Prepend to message body
                            message_content = f"{mention_link} {message_content}"
                            
                            logger.info(f"🏷️ Telegram Auto-Mention: {mention_link}")
                except Exception as ex:
                    logger.warning(f"⚠️ Failed to resolve mention for Telegram group: {ex}")

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
