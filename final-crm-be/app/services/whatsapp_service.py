"""
WhatsApp Service
Handles integration with external WhatsApp API service
Based on: https://github.com/chrishubert/whatsapp-api
"""
import logging
import httpx
from typing import Optional, Dict, Any, List

from fastapi import HTTPException, status

from app.config.settings import settings
import os
import asyncio
from app.config import settings as app_settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Service for managing WhatsApp sessions and messaging"""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        """
        Initialize WhatsApp Service

        Args:
            base_url: WhatsApp API base URL (default: from env WHATSAPP_API_URL)
            api_key: Optional API key for authentication (default: from env WHATSAPP_API_KEY)
        """
        self.base_url = base_url or os.getenv("WHATSAPP_API_URL", "http://localhost:3000")
        self.api_key = api_key or os.getenv("WHATSAPP_API_KEY")
        self.timeout = 30.0  # 30 seconds timeout

        # Remove trailing slash from base_url
        self.base_url = self.base_url.rstrip("/")

        logger.info(f"WhatsApp Service initialized with base URL: {self.base_url}")

    def _get_headers(self) -> Dict[str, str]:
        """
        Get HTTP headers for API requests

        Returns:
            Headers dictionary
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Add API key if configured
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        return headers

    async def register_session(self, session_id: str) -> Dict[str, Any]:
        """
        Register a new WhatsApp session

        This starts a new session and prepares it for QR code scanning.

        Args:
            session_id: Unique session identifier (e.g., agent_id or phone_number)

        Returns:
            Session registration result with status and details

        Raises:
            Exception: If session registration fails
        """
        try:
            url = f"{self.base_url}/session/start/{session_id}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._get_headers())
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ WhatsApp session registered: {session_id}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "status": "pending",
                    "message": "Session started successfully",
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to register WhatsApp session {session_id}: {error_msg}")
            raise Exception(f"Session registration failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to register WhatsApp session {session_id}: {e}")
            raise Exception(f"Session registration failed: {str(e)}")

    async def get_qr_code(self, session_id: str, as_image: bool = False) -> Dict[str, Any]:
        """
        Get QR code for session authentication

        This will retry up to 3 times with a 1 second delay if the API returns {"success": False}.

        Args:
            session_id: Session identifier
            as_image: If True, returns PNG image data; if False, returns QR data string

        Returns:
            QR code data (string or image bytes) with metadata

        Raises:
            Exception: If QR code retrieval fails
        """
        max_attempts = 10

        try:
            for attempt in range(max_attempts):
                # Choose endpoint based on format
                if as_image:
                    url = f"{self.base_url}/session/qr/{session_id}/image"
                else:
                    url = f"{self.base_url}/session/qr/{session_id}"

                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, headers=self._get_headers())
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "").lower()

                    # If the response is an image, return bytes immediately
                    if "image" in content_type:
                        return {
                            "success": True,
                            "session_id": session_id,
                            "format": "image/png",
                            "data": response.content
                        }

                    # Otherwise parse JSON result
                    result = response.json()

                    # If API explicitly returned success: false, retry
                    if result.get("success") is False:
                        logger.warning(
                            f"Attempt {attempt+1}/{max_attempts}: API returned success=false for session {session_id}"
                        )
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(1)
                            continue
                        else:
                            error_msg = f"API returned success=false after {max_attempts} attempts: {result}"
                            logger.error(f"Failed to get QR code for session {session_id}: {error_msg}")
                            raise Exception(f"QR code retrieval failed: {error_msg}")

                    # Successful JSON result
                    return {
                        "success": True,
                        "session_id": session_id,
                        "format": "text",
                        "qr_code": result.get("qr"),
                        "data": result
                    }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to get QR code for session {session_id}: {error_msg}")
            raise Exception(f"QR code retrieval failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to get QR code for session {session_id}: {e}")
            raise Exception(f"QR code retrieval failed: {str(e)}")


    async def check_session_status(self, session_id: str) -> Dict[str, Any]:
        """
        Check WhatsApp session status

        Args:
            session_id: Session identifier

        Returns:
            Session status information including connection state

        Raises:
            Exception: If status check fails
        """
        try:
            # Get session status by checking QR endpoint
            # If session is authenticated, QR endpoint will return error
            # We can also check by trying to get contacts
            url = f"{self.base_url}/session/status/{session_id}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._get_headers())
                if response.status_code == 200 and response.json().get("success"):
                    # Session is active and authenticated
                    return {
                        "success": True,
                        "session_id": session_id,
                        "status": "authenticated",
                        "connected": True,
                        "message": "Session is active and authenticated"
                    }
                elif response.status_code == 404:
                    # Session not found or not started
                    return {
                        "success": True,
                        "session_id": session_id,
                        "status": "not_found",
                        "connected": False,
                        "message": "Session not found"
                    }
                else:
                    # Session exists but not authenticated
                    return {
                        "success": True,
                        "session_id": session_id,
                        "status": "pending",
                        "connected": False,
                        "message": "Session pending authentication"
                    }

        except Exception as e:
            logger.error(f"Failed to check session status for {session_id}: {e}")
            return {
                "success": False,
                "session_id": session_id,
                "status": "error",
                "connected": False,
                "message": str(e)
            }

    async def terminate_session(self, session_id: str) -> Dict[str, Any]:
        """
        Terminate WhatsApp session

        Args:
            session_id: Session identifier

        Returns:
            Termination result

        Raises:
            Exception: If session termination fails
        """
        try:
            url = f"{self.base_url}/session/terminate/{session_id}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._get_headers())
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ WhatsApp session terminated: {session_id}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "message": "Session terminated successfully",
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to terminate session {session_id}: {error_msg}")
            raise Exception(f"Session termination failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to terminate session {session_id}: {e}")
            raise Exception(f"Session termination failed: {str(e)}")

    async def send_text_message(
        self,
        session_id: str,
        phone_number: str,
        message: str
    ) -> Dict[str, Any]:
        """
        Send text message via WhatsApp

        Args:
            session_id: Session identifier
            phone_number: Recipient phone number (format: 628123456789)
            message: Message text content

        Returns:
            Message send result with message ID

        Raises:
            Exception: If message sending fails
        """
        try:
            url = f"{self.base_url}/client/sendMessage/{session_id}"

            payload = {
                "chatId": f"{phone_number}@c.us",
                "contentType": "string",
                "content": message
            }

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ Text message sent via session {session_id} to {phone_number}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "phone_number": phone_number,
                    "message_type": "text",
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to send text message: {error_msg}")
            raise Exception(f"Message sending failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to send text message: {e}")
            raise Exception(f"Message sending failed: {str(e)}")

    async def send_media_message(
        self,
        session_id: str,
        phone_number: str,
        media_url: str,
        caption: Optional[str] = None,
        media_type: str = "image"
    ) -> Dict[str, Any]:
        """
        Send media message (image, video, audio) via WhatsApp

        Args:
            session_id: Session identifier
            phone_number: Recipient phone number
            media_url: Public URL of the media file
            caption: Optional caption for the media
            media_type: Type of media (image, video, audio)

        Returns:
            Message send result

        Raises:
            Exception: If message sending fails
        """
        try:
            url = f"{self.base_url}/client/sendMessage/{session_id}"

            # Prepare media payload based on type
            payload = {
                "chatId": f"{phone_number}@c.us",
                "contentType": media_type,
                "content": media_url
            }

            if caption:
                payload["caption"] = caption

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ Media message ({media_type}) sent via session {session_id} to {phone_number}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "phone_number": phone_number,
                    "message_type": media_type,
                    "media_url": media_url,
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to send media message: {error_msg}")
            raise Exception(f"Media message sending failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to send media message: {e}")
            raise Exception(f"Media message sending failed: {str(e)}")

    async def send_file_message(
        self,
        session_id: str,
        phone_number: str,
        file_url: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send document/file message via WhatsApp

        Args:
            session_id: Session identifier
            phone_number: Recipient phone number
            file_url: Public URL of the file
            filename: Optional filename to display
            caption: Optional caption for the file

        Returns:
            Message send result

        Raises:
            Exception: If message sending fails
        """
        try:
            url = f"{self.base_url}/client/sendMessage/{session_id}"

            payload = {
                "chatId": f"{phone_number}@c.us",
                "contentType": "MessageMediaDocument",
                "content": file_url
            }

            if filename:
                payload["filename"] = filename

            if caption:
                payload["caption"] = caption

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ File message sent via session {session_id} to {phone_number}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "phone_number": phone_number,
                    "message_type": "file",
                    "file_url": file_url,
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to send file message: {error_msg}")
            raise Exception(f"File message sending failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to send file message: {e}")
            raise Exception(f"File message sending failed: {str(e)}")

    async def get_client_class_info(self, session_id: str) -> Dict[str, Any]:
        """
        Get client class information including phone number for authenticated session

        Args:
            session_id: Session identifier

        Returns:
            Client class information with phone number

        Raises:
            Exception: If retrieval fails
        """
        try:
            url = f"{self.base_url}/client/getClassInfo/{session_id}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._get_headers())
                response.raise_for_status()

                result = response.json()
                logger.info(f"✅ Retrieved client class info for session {session_id}")

                return {
                    "success": True,
                    "session_id": session_id,
                    "data": result
                }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to get client class info: {error_msg}")
            return {
                "success": False,
                "session_id": session_id,
                "message": f"Client class info retrieval failed: {error_msg}"
            }
        except Exception as e:
            logger.error(f"Failed to get client class info: {e}")
            return {
                "success": False,
                "session_id": session_id,
                "message": f"Client class info retrieval failed: {str(e)}"
            }

    async def get_chat_class_info(
        self,
        session_id: str,
        chat_id: str
    ) -> Dict[str, Any]:
        """
        Get chat class information (individual, group, broadcast)

        Args:
            session_id: Session identifier
            chat_id: WhatsApp chat ID (e.g., "628123456789@c.us")

        Returns:
            Chat class information

        Raises:
            Exception: If retrieval fails
        """
        try:
            # Get chat info using getContacts endpoint
            url = f"{self.base_url}/client/getContacts/{session_id}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._get_headers())
                response.raise_for_status()

                contacts = response.json()

                # Find specific chat
                chat_info = None
                for contact in contacts:
                    if contact.get("id", {}).get("_serialized") == chat_id:
                        chat_info = contact
                        break

                if chat_info:
                    logger.info(f"✅ Retrieved chat info for {chat_id}")
                    return {
                        "success": True,
                        "session_id": session_id,
                        "chat_id": chat_id,
                        "chat_class": chat_info.get("id", {}).get("server", "unknown"),
                        "name": chat_info.get("name", chat_info.get("pushname", "Unknown")),
                        "is_group": chat_info.get("isGroup", False),
                        "data": chat_info
                    }
                else:
                    return {
                        "success": False,
                        "session_id": session_id,
                        "chat_id": chat_id,
                        "message": "Chat not found"
                    }

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"Failed to get chat info: {error_msg}")
            raise Exception(f"Chat info retrieval failed: {error_msg}")
        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")
            raise Exception(f"Chat info retrieval failed: {str(e)}")

    def get_supabase_client(self):
        """Get Supabase client from settings"""
        from supabase import create_client

        if not app_settings.is_supabase_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Supabase is not configured"
            )

        return create_client(app_settings.SUPABASE_URL, app_settings.SUPABASE_SERVICE_KEY)



    def configure_webhook(
        self,
        session_id: str,
        webhook_url: str
    ) -> Dict[str, str]:
        """
        Configure webhook URL for session callbacks

        Note: This returns environment variable configuration instructions.
        The actual webhook configuration must be set in the WhatsApp API service
        environment variables.

        Args:
            session_id: Session identifier
            webhook_url: Webhook callback URL

        Returns:
            Configuration instructions
        """
        return {
            "session_id": session_id,
            "webhook_url": webhook_url,
            "instructions": (
                f"Set environment variable in WhatsApp API service:\n"
                f"{session_id}_WEBHOOK_URL={webhook_url}\n\n"
                f"Or set global webhook:\n"
                f"BASE_WEBHOOK_URL={webhook_url}"
            ),
            "note": "Webhooks are configured via environment variables in the WhatsApp API service"
        }


# Singleton instance
_whatsapp_service: Optional[WhatsAppService] = None


def get_whatsapp_service(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None
) -> WhatsAppService:
    """
    Get or create WhatsAppService singleton

    Args:
        base_url: Optional WhatsApp API base URL
        api_key: Optional API key

    Returns:
        WhatsAppService instance
    """
    global _whatsapp_service
    if _whatsapp_service is None:
        _whatsapp_service = WhatsAppService(base_url, api_key)
    return _whatsapp_service
