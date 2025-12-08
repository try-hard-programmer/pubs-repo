"""
WhatsApp Models
Pydantic models for WhatsApp integration requests and responses
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime


# Request Models

class SessionActivateRequest(BaseModel):
    """Request model for session activation"""
    agent_id: str = Field(..., description="Agent ID to use as session identifier")


class SendTextMessageRequest(BaseModel):
    """Request model for sending text message"""
    session_id: str = Field(..., description="WhatsApp session ID")
    phone_number: str = Field(..., description="Recipient phone number (e.g., 628123456789)")
    message: str = Field(..., description="Message text content")


class SendMediaMessageRequest(BaseModel):
    """Request model for sending media message"""
    session_id: str = Field(..., description="WhatsApp session ID")
    phone_number: str = Field(..., description="Recipient phone number")
    media_url: str = Field(..., description="Public URL of the media file")
    caption: Optional[str] = Field(None, description="Optional caption")
    media_type: str = Field(default="image", description="Media type: image, video, audio")


class SendFileMessageRequest(BaseModel):
    """Request model for sending file message"""
    session_id: str = Field(..., description="WhatsApp session ID")
    phone_number: str = Field(..., description="Recipient phone number")
    file_url: str = Field(..., description="Public URL of the file")
    filename: Optional[str] = Field(None, description="Optional filename")
    caption: Optional[str] = Field(None, description="Optional caption")


class WebhookConfigRequest(BaseModel):
    """Request model for webhook configuration"""
    session_id: str = Field(..., description="WhatsApp session ID")
    webhook_url: str = Field(..., description="Webhook callback URL")


class ChatInfoRequest(BaseModel):
    """Request model for getting chat info"""
    session_id: str = Field(..., description="WhatsApp session ID")
    chat_id: str = Field(..., description="WhatsApp chat ID (e.g., 628123456789@c.us)")


# Response Models

class SessionActivateResponse(BaseModel):
    """Response model for session activation"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    status: str = Field(..., description="Session status")
    qr_code: Optional[str] = Field(None, description="QR code data (if available)")
    qr_image_url: Optional[str] = Field(None, description="QR code image URL")
    message: str = Field(..., description="Status message")
    data: Optional[Dict[str, Any]] = Field(None, description="Additional data")


class QRCodeResponse(BaseModel):
    """Response model for QR code"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    format: str = Field(..., description="QR code format (text or image/png)")
    qr_code: Optional[str] = Field(None, description="QR code string (if format is text)")
    data: Optional[Any] = Field(None, description="QR code data or image bytes")


class SessionStatusResponse(BaseModel):
    """Response model for session status"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    status: str = Field(..., description="Session status (authenticated, pending, not_found, error)")
    connected: bool = Field(..., description="Connection status")
    message: str = Field(..., description="Status message")
    phone_number: Optional[str] = Field(None, description="WhatsApp phone number associated with the session")


class SessionTerminateResponse(BaseModel):
    """Response model for session termination"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    message: str = Field(..., description="Status message")
    data: Optional[Dict[str, Any]] = Field(None, description="Additional data")


class MessageSendResponse(BaseModel):
    """Response model for message sending"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    phone_number: str = Field(..., description="Recipient phone number")
    message_type: str = Field(..., description="Message type (text, image, video, audio, file)")
    data: Optional[Dict[str, Any]] = Field(None, description="Message data including message ID")


class ChatInfoResponse(BaseModel):
    """Response model for chat information"""
    success: bool = Field(..., description="Operation success status")
    session_id: str = Field(..., description="Session identifier")
    chat_id: str = Field(..., description="WhatsApp chat ID")
    chat_class: Optional[str] = Field(None, description="Chat class (c.us for individual, g.us for group)")
    name: Optional[str] = Field(None, description="Chat/contact name")
    is_group: Optional[bool] = Field(None, description="Is group chat")
    message: Optional[str] = Field(None, description="Status message")
    data: Optional[Dict[str, Any]] = Field(None, description="Full chat data")


class WebhookConfigResponse(BaseModel):
    """Response model for webhook configuration"""
    session_id: str = Field(..., description="Session identifier")
    webhook_url: str = Field(..., description="Webhook callback URL")
    instructions: str = Field(..., description="Configuration instructions")
    note: str = Field(..., description="Important notes about webhook setup")


# Webhook Event Models (for receiving callbacks)

class WebhookMessage(BaseModel):
    """Model for incoming webhook message"""
    id: str = Field(..., description="Message ID")
    from_number: str = Field(..., alias="from", description="Sender phone number")
    to_number: str = Field(..., alias="to", description="Recipient phone number")
    body: Optional[str] = Field(None, description="Message text content")
    timestamp: int = Field(..., description="Message timestamp")
    has_media: bool = Field(default=False, description="Whether message has media")
    media_url: Optional[str] = Field(None, description="Media URL if has_media is True")
    media_type: Optional[str] = Field(None, description="Media MIME type")


class WebhookEvent(BaseModel):
    """Model for incoming webhook event"""
    session_id: str = Field(..., description="Session identifier")
    event_type: str = Field(..., description="Event type (message, qr, status, etc.)")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Event timestamp")
    data: Dict[str, Any] = Field(..., description="Event data")
