"""
Webhook Models
Pydantic models for incoming webhook messages from external services
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Union
from datetime import datetime


# ============================================
# INCOMING MESSAGE MODELS
# ============================================

class IncomingWebhookMessage(BaseModel):
    """Base model for incoming webhook message"""
    message: str = Field(..., description="Message content")
    timestamp: Optional[str] = Field(None, description="Message timestamp (ISO 8601)")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_schema_extra = {
            "example": {
                "message": "Hello, I need help",
                "timestamp": "2025-10-21T15:30:00Z",
                "metadata": {}
            }
        }


class WhatsAppWebhookMessage(IncomingWebhookMessage):
    """WhatsApp-specific webhook message"""
    phone_number: str = Field(..., description="Sender phone number (from) - e.g., +6281234567890")
    to_number: str = Field(..., description="Recipient/business phone number (to) - e.g., +6281111111")
    sender_name: Optional[str] = Field(None, description="Sender name from WhatsApp profile")
    message_id: Optional[str] = Field(None, description="WhatsApp message ID")
    message_type: Optional[str] = Field(default="text", description="Message type (text, image, video, audio, document)")
    media_url: Optional[str] = Field(None, description="Media URL if message contains media")
    caption: Optional[str] = Field(None, description="Media caption if applicable")

    class Config:
        json_schema_extra = {
            "example": {
                "phone_number": "+6281234567890",
                "to_number": "+6281111111",
                "sender_name": "John Doe",
                "message": "Hello, I need help with my order",
                "message_id": "wamid.xxx123",
                "message_type": "text",
                "timestamp": "2025-10-21T15:30:00Z",
                "metadata": {
                    "from_whatsapp_business": True
                }
            }
        }


class WhatsAppUnofficialWebhookMessage(BaseModel):
    """WhatsApp Unofficial webhook message (from whatsapp-web.js)"""
    dataType: str = Field(..., description="Type of data (message, media)")
    # Change Dict[str, Any] to Optional[Any] to prevent 422 errors
    data: Optional[Any] = Field(default=None, description="Message or media data")
    sessionId: str = Field(..., description="WhatsApp session ID")

    class Config:
        extra = "allow"  # Allow extra fields from WhatsApp service
        populate_by_name = True  # Support both camelCase and snake_case
        json_schema_extra = {
            "example": {
                "dataType": "message",
                "data": {
                    "message": {
                        "_data": {
                            "id": {"fromMe": False, "remote": "6289505130799@c.us"},
                            "body": "Hello",
                            "type": "chat",
                            "from": "6289505130799@c.us",
                            "to": "62881024580401@c.us"
                        }
                    }
                },
                "sessionId": "ea799531-14ff-400a-9380-cd2a9c16af5c"
            }
        }


class TelegramWebhookMessage(IncomingWebhookMessage):
    """Telegram-specific webhook message"""
    telegram_id: str = Field(..., description="Telegram user ID (sender)")
    bot_token: Optional[str] = Field(None, description="Telegram bot token (for identifying which bot received the message)")
    bot_username: Optional[str] = Field(None, description="Telegram bot username (alternative to bot_token)")
    username: Optional[str] = Field(None, description="Sender's username (without @)")
    first_name: Optional[str] = Field(None, description="Sender's first name")
    last_name: Optional[str] = Field(None, description="Sender's last name")
    chat_id: Optional[str] = Field(None, description="Telegram chat ID")
    message_id: Optional[int] = Field(None, description="Telegram message ID")
    message_type: Optional[str] = Field(default="text", description="Message type")
    photo_url: Optional[str] = Field(None, description="Photo URL if message contains photo")
    document_url: Optional[str] = Field(None, description="Document URL if message contains document")

    class Config:
        json_schema_extra = {
            "example": {
                "telegram_id": "123456789",
                "bot_username": "my_support_bot",
                "username": "johndoe",
                "first_name": "John",
                "last_name": "Doe",
                "message": "Hello from Telegram",
                "message_id": 999,
                "message_type": "text",
                "timestamp": "2025-10-21T15:30:00Z"
            }
        }


class EmailWebhookMessage(IncomingWebhookMessage):
    """Email-specific webhook message"""
    email: str = Field(..., description="Sender email address (from)")
    to_email: str = Field(..., description="Recipient email address (to) - used to identify which agent/mailbox")
    sender_name: Optional[str] = Field(None, description="Sender name")
    subject: Optional[str] = Field(None, description="Email subject")
    message_id: Optional[str] = Field(None, description="Email message ID")
    attachments: Optional[list] = Field(default_factory=list, description="Email attachments")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "customer@example.com",
                "to_email": "support@example.com",
                "sender_name": "Jane Smith",
                "subject": "Question about product",
                "message": "Hello, I have a question about your product...",
                "message_id": "email-msg-123",
                "timestamp": "2025-10-21T15:30:00Z",
                "attachments": []
            }
        }


class WhatsAppEventPayload(BaseModel):
    # Flexible payload to handle chrishubert/whatsapp-api webhooks
    message: Optional[Union[Dict[str, Any], str]] = None
    qr: Optional[str] = None
    percent: Optional[int] = None 
    
    class Config:
        extra = "allow"

# ============================================
# RESPONSE MODELS
# ============================================

class WebhookRouteResponse(BaseModel):
    """Response model for webhook message routing"""
    success: bool = Field(..., description="Whether routing was successful")
    chat_id: str = Field(..., description="Chat UUID where message was routed")
    message_id: str = Field(..., description="Created message UUID")
    customer_id: str = Field(..., description="Customer UUID")
    is_new_chat: bool = Field(..., description="Whether a new chat was created")
    was_reopened: bool = Field(..., description="Whether an existing chat was reopened")
    handled_by: str = Field(..., description="Who is handling the chat (ai/human/unassigned)")
    status: str = Field(..., description="Chat status (open/assigned/resolved/closed)")
    channel: str = Field(..., description="Communication channel")
    message: str = Field(..., description="Human-readable status message")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "chat_id": "chat-uuid-123",
                "message_id": "msg-uuid-456",
                "customer_id": "customer-uuid-789",
                "is_new_chat": False,
                "was_reopened": True,
                "handled_by": "ai",
                "status": "open",
                "channel": "whatsapp",
                "message": "Message routed to existing chat (chat was reopened)"
            }
        }


class WebhookErrorResponse(BaseModel):
    """Error response model for webhook failures"""
    success: bool = Field(default=False, description="Always False for errors")
    error: str = Field(..., description="Error type")
    detail: str = Field(..., description="Detailed error message")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat(), description="Error timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "success": False,
                "error": "authentication_failed",
                "detail": "Invalid webhook secret key",
                "timestamp": "2025-10-21T15:30:00Z"
            }
        }
