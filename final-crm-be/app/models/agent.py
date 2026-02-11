"""
Agent Models for CRM Module

Pydantic models for Agent Management System.
Includes agents, settings, integrations, customers, chats, messages, and tickets.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, EmailStr, validator
from datetime import datetime as dt, time as t, date as d
from enum import Enum


# ============================================
# ENUMS
# ============================================

class AgentStatus(str, Enum):
    """Agent status enumeration"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    BUSY = "busy"
    ARCHIVED = "archived"

class ChatStatus(str, Enum):
    """Chat status enumeration"""
    OPEN = "open"
    PENDING = "pending"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SenderType(str, Enum):
    """Message sender type enumeration"""
    CUSTOMER = "customer"
    AGENT = "agent"
    AI = "ai"


class CommunicationChannel(str, Enum):
    """Communication channel enumeration"""
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    EMAIL = "email"
    WEB = "web"
    MCP = "mcp"


class TicketPriority(str, Enum):
    """Ticket priority enumeration"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(str, Enum):
    """Ticket status enumeration"""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IntegrationStatus(str, Enum):
    """Integration status enumeration"""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ERROR = "error"


class LanguageCode(str, Enum):
    """Language code enumeration"""
    ID = "id"  # Indonesian
    EN = "en"  # English
    MULTI = "multi"  # Multi-language


class ToneType(str, Enum):
    """Tone type enumeration"""
    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    FORMAL = "formal"
    EMPATHETIC = "empathetic"


class TemperatureSetting(str, Enum):
    """AI temperature setting enumeration"""
    CONSISTENT = "consistent"
    BALANCED = "balanced"
    CREATIVE = "creative"


# ============================================
# AGENT MODELS
# ============================================

class AgentCreate(BaseModel):
    """Schema for creating a new agent"""
    name: str = Field(..., min_length=1, max_length=255, description="Agent full name")
    email: EmailStr = Field(..., description="Agent email address")
    phone: str = Field(..., min_length=8, max_length=50, description="Agent phone number")
    status: AgentStatus = Field(default=AgentStatus.ACTIVE, description="Initial agent status")
    avatar_url: Optional[str] = Field(None, description="URL to agent avatar")
    user_id: Optional[str] = Field(None, description="Link to auth.users if applicable")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "John Doe",
                "email": "john.doe@example.com",
                "phone": "+62 812-3456-7890",
                "status": "active",
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            }
        }


class AgentUpdate(BaseModel):
    """Schema for updating an agent"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, min_length=8, max_length=50)
    status: Optional[AgentStatus] = None
    avatar_url: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "John Doe Updated",
                "status": "busy"
            }
        }


class AgentStatusUpdate(BaseModel):
    """Schema for updating agent status only"""
    status: AgentStatus = Field(..., description="New agent status")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "active"
            }
        }


class Agent(BaseModel):
    """Schema for agent response"""
    id: str = Field(..., description="Agent UUID")
    organization_id: str = Field(..., description="Organization UUID")
    user_id: Optional[str] = Field(None, description="User UUID if linked")
    name: str = Field(..., description="Agent full name")
    email: str = Field(..., description="Agent email")
    phone: str = Field(..., description="Agent phone number")
    status: AgentStatus = Field(..., description="Agent status")
    avatar_url: Optional[str] = Field(None, description="Avatar URL")
    assigned_chats_count: int = Field(0, description="Number of assigned chats")
    resolved_today_count: int = Field(0, description="Number of chats resolved today")
    avg_response_time_seconds: int = Field(0, description="Average response time in seconds")
    last_active_at: dt = Field(..., description="Last activity timestamp")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "agent-uuid-123",
                "organization_id": "org-uuid-456",
                "user_id": "user-uuid-789",
                "name": "John Doe",
                "email": "john.doe@example.com",
                "phone": "+62 812-3456-7890",
                "status": "active",
                "avatar_url": "https://example.com/avatar.jpg",
                "assigned_chats_count": 5,
                "resolved_today_count": 12,
                "avg_response_time_seconds": 150,
                "last_active_at": "2025-10-16T10:30:00Z",
                "created_at": "2025-10-10T08:00:00Z",
                "updated_at": "2025-10-16T10:30:00Z"
            }
        }


# ============================================
# AGENT SETTINGS MODELS
# ============================================

class PersonaConfig(BaseModel):
    """Agent persona configuration"""
    name: str = Field(default="Customer Support Assistant", description="Persona name")
    language: LanguageCode = Field(default=LanguageCode.ID, description="Preferred language")
    tone: ToneType = Field(default=ToneType.FRIENDLY, description="Communication tone")
    customInstructions: str = Field(default="", description="Custom AI instructions")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Customer Support Assistant",
                "language": "id",
                "tone": "friendly",
                "customInstructions": "Always greet customers warmly and provide detailed answers."
            }
        }


class WorkingHours(BaseModel):
    """Working hours for a specific day"""
    day: str = Field(..., description="Day name or index (0=Sunday, 1=Monday, etc.)")
    enabled: bool = Field(True, description="Is this day active?")
    start: str = Field(..., description="Start time (HH:mm)")
    end: str = Field(..., description="End time (HH:mm)")

    @validator('day', pre=True)
    def convert_day_to_string(cls, v):
        """Convert integer day index to string, or keep string as is"""
        if isinstance(v, int):
            # Map integer (0-6) to day names
            day_map = {
                0: "sunday",
                1: "monday",
                2: "tuesday",
                3: "wednesday",
                4: "thursday",
                5: "friday",
                6: "saturday"
            }
            return day_map.get(v, str(v))
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "day": "monday",
                "enabled": True,
                "start": "09:00",
                "end": "17:00"
            }
        }


class \
        ScheduleConfig(BaseModel):
    """Agent schedule configuration"""
    enabled: bool = Field(default=False, description="Enable schedule")
    timezone: str = Field(default="Asia/Jakarta", description="Timezone")
    workingHours: List[WorkingHours] = Field(default_factory=list, description="Working hours per day")

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "timezone": "Asia/Jakarta",
                "workingHours": [
                    {"day": "Senin", "enabled": True, "start": "09:00", "end": "17:00"},
                    {"day": "Selasa", "enabled": True, "start": "09:00", "end": "17:00"}
                ]
            }
        }


class HandoffTriggers(BaseModel):
    """Handoff triggers configuration"""
    enabled: bool = Field(default=True, description="Enable handoff triggers")
    keywords: List[str] = Field(default_factory=list, description="Trigger keywords")
    sentimentThreshold: float = Field(default=-0.5, ge=-1.0, le=0.0, description="Sentiment threshold")
    unansweredQuestions: int = Field(default=3, ge=1, le=100, description="Max unanswered questions (1-100)")
    escalationMessage: str = Field(default="", description="Message sent on escalation")

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "keywords": ["speak to human", "talk to agent", "supervisor"],
                "sentimentThreshold": -0.5,
                "unansweredQuestions": 3,
                "escalationMessage": "I'm connecting you to a human agent..."
            }
        }


class AdvancedConfig(BaseModel):
    """Advanced agent configuration"""
    temperature: TemperatureSetting = Field(default=TemperatureSetting.BALANCED, description="AI temperature")
    historyLimit: int = Field(default=10, ge=5, le=50, description="Conversation history limit")
    handoffTriggers: HandoffTriggers = Field(default_factory=HandoffTriggers, description="Handoff triggers")

    class Config:
        json_schema_extra = {
            "example": {
                "temperature": "balanced",
                "historyLimit": 10,
                "handoffTriggers": {
                    "enabled": True,
                    "keywords": ["speak to human"],
                    "sentimentThreshold": -0.5,
                    "unansweredQuestions": 3,
                    "escalationMessage": "Let me connect you to a human agent..."
                }
            }
        }


class TicketingConfig(BaseModel):
    """Ticketing system configuration"""
    enabled: bool = Field(default=False, description="Enable ticketing")
    autoCreateTicket: bool = Field(default=True, description="Auto-create ticket on chat start")
    ticketPrefix: str = Field(default="TKT-", description="Ticket number prefix")
    requireCategory: bool = Field(default=True, description="Require category selection")
    requirePriority: bool = Field(default=False, description="Require priority selection")
    autoCloseAfterResolved: bool = Field(default=True, description="Auto-close after resolved")
    autoCloseDelay: int = Field(default=24, ge=1, le=168, description="Auto-close delay in hours")
    categories: List[str] = Field(default_factory=list, description="Ticket categories")

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "autoCreateTicket": True,
                "ticketPrefix": "TKT-",
                "requireCategory": True,
                "requirePriority": False,
                "autoCloseAfterResolved": True,
                "autoCloseDelay": 24,
                "categories": ["Technical", "Billing", "General Inquiry"]
            }
        }


class AgentSettings(BaseModel):
    """Complete agent settings"""
    id: str = Field(..., description="Settings UUID")
    agent_id: str = Field(..., description="Agent UUID")
    persona_config: PersonaConfig = Field(default_factory=PersonaConfig, description="Persona configuration")
    schedule_config: ScheduleConfig = Field(default_factory=ScheduleConfig, description="Schedule configuration")
    advanced_config: AdvancedConfig = Field(default_factory=AdvancedConfig, description="Advanced configuration")
    ticketing_config: TicketingConfig = Field(default_factory=TicketingConfig, description="Ticketing configuration")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True


class AgentSettingsUpdate(BaseModel):
    """Schema for updating agent settings"""
    persona_config: Optional[PersonaConfig] = None
    schedule_config: Optional[ScheduleConfig] = None
    advanced_config: Optional[AdvancedConfig] = None
    ticketing_config: Optional[TicketingConfig] = None

    class Config:
        json_schema_extra = {
            "example": {
                "persona_config": {
                    "name": "Customer Support Assistant",
                    "language": "id",
                    "tone": "friendly",
                    "customInstructions": ""
                }
            }
        }


# ============================================
# KNOWLEDGE DOCUMENT MODELS
# ============================================

class KnowledgeDocumentCreate(BaseModel):
    """Schema for creating a knowledge document"""
    name: str = Field(..., min_length=1, max_length=255, description="Document name")
    file_url: str = Field(..., description="File URL in storage")
    file_type: str = Field(..., description="File type (PDF, DOCX, etc.)")
    file_size_kb: int = Field(..., ge=0, description="File size in KB")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class KnowledgeDocument(BaseModel):
    """Schema for knowledge document response"""
    id: str = Field(..., description="Document UUID")
    agent_id: str = Field(..., description="Agent UUID")
    name: str = Field(..., description="Document name")
    file_url: str = Field(..., description="File URL")
    file_type: str = Field(..., description="File type")
    file_size_kb: int = Field(..., description="File size in KB")
    uploaded_at: dt = Field(..., description="Upload timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "doc-uuid-123",
                "agent_id": "agent-uuid-456",
                "name": "Product_Manual.pdf",
                "file_url": "https://storage.example.com/docs/manual.pdf",
                "file_type": "PDF",
                "file_size_kb": 2048,
                "uploaded_at": "2025-10-16T10:00:00Z",
                "metadata": {}
            }
        }


# ============================================
# AGENT INTEGRATION MODELS
# ============================================

class ChannelIntegrationConfig(BaseModel):
    """Base channel integration configuration"""
    pass


class WhatsAppConfig(ChannelIntegrationConfig):
    """WhatsApp integration configuration"""
    status: IntegrationStatus = Field(default=IntegrationStatus.DISCONNECTED, description="Connection status")
    connectedAt: Optional[dt] = Field(None, description="Connection timestamp")
    phoneNumber: Optional[str] = Field(None, description="Connected phone number")
    sessionId: Optional[str] = Field(None, description="Session identifier")


class TelegramConfig(ChannelIntegrationConfig):
    """Telegram integration configuration"""
    botToken: str = Field(..., description="Bot API token")
    botUsername: str = Field(..., description="Bot username")
    webhookUrl: Optional[str] = Field(None, description="Webhook URL")


class EmailConfig(ChannelIntegrationConfig):
    """Email integration configuration"""
    provider: str = Field(..., description="Email provider (Gmail, Outlook, Custom)")
    email: EmailStr = Field(..., description="Email address")
    imapHost: str = Field(..., description="IMAP server")
    imapPort: int = Field(..., description="IMAP port")
    smtpHost: str = Field(..., description="SMTP server")
    smtpPort: int = Field(..., description="SMTP port")


class MCPServer(BaseModel):
    """MCP server configuration"""
    id: str = Field(..., description="Server UUID")
    name: str = Field(..., description="Server name")
    type: str = Field(..., description="Server type")
    endpoint: str = Field(..., description="API endpoint")
    enabled: bool = Field(default=True, description="Is server enabled")


class MCPConfig(ChannelIntegrationConfig):
    """MCP integration configuration"""
    servers: List[MCPServer] = Field(default_factory=list, description="List of MCP servers")


class AgentIntegration(BaseModel):
    """Schema for agent integration"""
    id: str = Field(..., description="Integration UUID")
    agent_id: str = Field(..., description="Agent UUID")
    channel: CommunicationChannel = Field(..., description="Communication channel")
    enabled: bool = Field(default=False, description="Is integration enabled")
    config: Dict[str, Any] = Field(default_factory=dict, description="Channel-specific config")
    status: IntegrationStatus = Field(default=IntegrationStatus.DISCONNECTED, description="Integration status")
    last_connected_at: Optional[dt] = Field(None, description="Last connection timestamp")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True


class AgentIntegrationUpdate(BaseModel):
    """Schema for updating agent integration"""
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    status: Optional[IntegrationStatus] = None


# ============================================
# CUSTOMER MODELS
# ============================================

class CustomerCreate(BaseModel):
    """Schema for creating a customer"""
    name: str = Field(..., min_length=1, max_length=255, description="Customer name")
    email: Optional[EmailStr] = Field(None, description="Customer email")
    phone: Optional[str] = Field(None, description="Customer phone")
    avatar_url: Optional[str] = Field(None, description="Avatar URL")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class CustomerUpdate(BaseModel):
    """Schema for updating a customer"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class Customer(BaseModel):
    """Schema for customer response"""
    id: str = Field(..., description="Customer UUID")
    organization_id: str = Field(..., description="Organization UUID")
    name: str = Field(..., description="Customer name")
    email: Optional[str] = Field(None, description="Customer email")
    phone: Optional[str] = Field(None, description="Customer phone")
    avatar_url: Optional[str] = Field(None, description="Avatar URL")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True


# ============================================
# CHAT & MESSAGE MODELS
# ============================================

class ChatCreate(BaseModel):
    """Schema for creating a chat"""
    customer_id: Optional[str] = Field(None, description="Customer UUID (optional if customer_name and contact provided)")
    customer_name: Optional[str] = Field(None, description="Customer name for findOrCreate")
    contact: Optional[str] = Field(None, description="Customer contact (phone/email) for findOrCreate")
    channel: CommunicationChannel = Field(..., description="Communication channel")
    initial_message: Optional[str] = Field(None, description="Initial message content")
    assigned_agent_id: Optional[str] = Field(None, description="Assigned agent UUID")
    using_agent_integration_id: Optional[str] = Field(None, description="Force specific integration ID for sending")


class ChatUpdate(BaseModel):
    """Schema for updating a chat"""
    assigned_agent_id: Optional[str] = None
    status: Optional[ChatStatus] = None


class ChatAssign(BaseModel):
    """Schema for assigning a chat to an agent"""
    assigned_agent_id: Optional[str] = Field(None, description="Agent UUID to assign. Required if assigned_to_me is False")
    assigned_to_me: bool = Field(default=False, description="Set to True to assign chat to current authenticated user. System will auto-create agent profile if doesn't exist")
    reason: Optional[str] = Field(None, description="Optional reason for assignment (e.g., 'Customer requested transfer', 'Taking over conversation')")

    class Config:
        json_schema_extra = {
            "example": {
                "assigned_agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "assigned_to_me": False,
                "reason": "Customer requested transfer to specialist"
            }
        }


class ChatEscalation(BaseModel):
    """Schema for escalating a chat from AI to human agent"""
    human_agent_id: str = Field(..., description="Human agent UUID to escalate to")
    reason: Optional[str] = Field(None, description="Reason for escalation")

    class Config:
        json_schema_extra = {
            "example": {
                "human_agent_id": "agent-uuid-123",
                "reason": "Customer requested human agent"
            }
        }


class Chat(BaseModel):
    """Schema for chat response"""
    id: str = Field(..., description="Chat UUID")
    organization_id: str = Field(..., description="Organization UUID")
    customer_id: str = Field(..., description="Customer UUID")
    customer_name: Optional[str] = Field(None, description="Customer name (from customers table)")

    # Legacy field for backward compatibility
    assigned_agent_id: Optional[str] = Field(None, description="Assigned agent UUID (backward compatibility)")
    agent_name: Optional[str] = Field(None, description="Agent name (from agents table)")

    # Dual agent tracking fields
    ai_agent_id: Optional[str] = Field(None, description="AI Agent UUID that handles/handled the chat")
    ai_agent_name: Optional[str] = Field(None, description="AI Agent name (from agents table)")
    human_agent_id: Optional[str] = Field(None, description="Human Agent UUID (assigned after escalation)")
    human_id: Optional[str] = Field(None, description="Human ID (for systems using human identifiers)")
    human_agent_name: Optional[str] = Field(None, description="Human Agent name (from agents table)")
    handled_by: str = Field(default="unassigned", description="Current handler: ai, human, or unassigned")

    # Escalation tracking
    escalated_at: Optional[dt] = Field(None, description="Timestamp when escalated from AI to human")
    escalation_reason: Optional[str] = Field(None, description="Reason for escalation to human agent")

    status: ChatStatus = Field(..., description="Chat status")
    channel: CommunicationChannel = Field(..., description="Communication channel")
    unread_count: int = Field(0, description="Unread message count")
    last_message_at: dt = Field(..., description="Last message timestamp")
    last_message: Optional['Message'] = Field(None, description="Last message in the chat")
    created_at: dt = Field(..., description="Creation timestamp")
    resolved_at: Optional[dt] = Field(None, description="Resolution timestamp")
    resolved_by_agent_id: Optional[str] = Field(None, description="Agent who resolved")
    updated_at: dt = Field(..., description="Last update timestamp")

    metadata: Dict[str, Any] = Field(default_factory=dict, description="Chat metadata (is_group, etc.)")

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    """Schema for creating a message"""
    content: str = Field(..., min_length=1, description="Message content")
    sender_type: SenderType = Field(..., description="Sender type")
    sender_id: Optional[str] = Field(None, description="Sender UUID (agent or customer)")
    ticket_id: Optional[str] = Field(None, description="Related ticket UUID")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class Message(BaseModel):
    """Schema for message response"""
    id: str = Field(..., description="Message UUID")
    chat_id: str = Field(..., description="Chat UUID")
    sender_type: SenderType = Field(..., description="Sender type")
    sender_id: Optional[str] = Field(None, description="Sender UUID")
    sender_name: Optional[str] = Field(None, description="Sender name (from users/agents/customers table)")
    content: str = Field(..., description="Message content")
    ticket_id: Optional[str] = Field(None, description="Related ticket UUID")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True


# ============================================
# TICKET MODELS
# ============================================

class TicketCreate(BaseModel):
    """Schema for creating a ticket"""
    chat_id: str = Field(..., description="Chat UUID")
    customer_id: str = Field(..., description="Customer UUID")
    title: str = Field(..., min_length=1, max_length=255, description="Ticket title")
    description: Optional[str] = Field(None, description="Ticket description")
    category: Optional[str] = Field(None, description="Ticket category")
    priority: TicketPriority = Field(default=TicketPriority.MEDIUM, description="Ticket priority")
    assigned_agent_id: Optional[str] = Field(None, description="Assigned agent UUID")
    tags: List[str] = Field(default_factory=list, description="Ticket tags")


class TicketUpdate(BaseModel):
    """Schema for updating a ticket"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[TicketPriority] = None
    status: Optional[TicketStatus] = None
    assigned_agent_id: Optional[str] = None
    tags: Optional[List[str]] = None


class Ticket(BaseModel):
    """Schema for ticket response"""
    id: str = Field(..., description="Ticket UUID")
    organization_id: str = Field(..., description="Organization UUID")
    ticket_number: str = Field(..., description="Ticket number")
    chat_id: str = Field(..., description="Chat UUID")
    customer_id: str = Field(..., description="Customer UUID")
    assigned_agent_id: Optional[str] = Field(None, description="Assigned agent UUID")
    title: str = Field(..., description="Ticket title")
    description: Optional[str] = Field(None, description="Ticket description")
    category: Optional[str] = Field(None, description="Ticket category")
    priority: TicketPriority = Field(..., description="Ticket priority")
    status: TicketStatus = Field(..., description="Ticket status")
    tags: List[str] = Field(default_factory=list, description="Ticket tags")
    created_at: dt = Field(..., description="Creation timestamp")
    updated_at: dt = Field(..., description="Last update timestamp")
    resolved_at: Optional[dt] = Field(None, description="Resolution timestamp")
    closed_at: Optional[dt] = Field(None, description="Close timestamp")
    channel: Optional[str] = None
    # [FIX] Add these fields so the Frontend receives the data
    customer_name: Optional[str] = Field(None, description="Joined Customer Name")
    customer: Optional[Dict[str, Any]] = Field(None, description="Full Customer Object")

    class Config:
        from_attributes = True


# ============================================
# ANALYTICS MODELS
# ============================================

class AgentMetrics(BaseModel):
    """Schema for agent performance metrics"""
    id: str = Field(..., description="Metrics UUID")
    agent_id: str = Field(..., description="Agent UUID")
    date: d = Field(..., description="Metrics date")
    chats_assigned: int = Field(0, description="Chats assigned")
    chats_resolved: int = Field(0, description="Chats resolved")
    avg_response_time_seconds: int = Field(0, description="Average response time")
    avg_resolution_time_seconds: int = Field(0, description="Average resolution time")
    customer_satisfaction_score: float = Field(0.0, ge=0.0, le=5.0, description="Customer satisfaction score")

    class Config:
        from_attributes = True


class DashboardMetrics(BaseModel):
    """Schema for dashboard metrics"""
    total_chats: int = Field(..., description="Total chats")
    open_chats: int = Field(..., description="Open chats")
    resolved_today: int = Field(..., description="Chats resolved today")
    avg_response_time: str = Field(..., description="Average response time (formatted)")
    active_agents: int = Field(..., description="Number of active agents")
    tickets_by_status: Dict[str, int] = Field(..., description="Tickets grouped by status")


# ============================================
# LIST RESPONSE MODELS
# ============================================
class MessageAttachment(BaseModel):
    """Schema for standardized attachment object"""
    url: str = Field(..., description="Public URL of the file")
    type: str = Field(..., description="MIME type of the file (e.g. image/png)")
    name: Optional[str] = Field(None, description="Original filename")
    
class AgentListResponse(BaseModel):
    """Response for list of agents"""
    agents: List[Agent]
    total: int = Field(..., description="Total number of agents")


class ChatListResponse(BaseModel):
    """Response for list of chats"""
    chats: List[Chat]
    total: int = Field(..., description="Total number of chats")


class MessageListResponse(BaseModel):
    """Response for list of messages"""
    messages: List[Message]
    total: int = Field(..., description="Total number of messages")


class TicketListResponse(BaseModel):
    """Response for list of tickets"""
    tickets: List[Ticket]
    total: int = Field(..., description="Total number of tickets")


class CustomerListResponse(BaseModel):
    """Response for list of customers"""
    customers: List[Customer]
    total: int = Field(..., description="Total number of customers")


# Update forward references for Chat model to resolve Message
Chat.model_rebuild()
