from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field

# ==========================================
# ENUMS
# ==========================================

class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"

class ActorType(str, Enum):
    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"

# ==========================================
# TICKET GUARD (INTENT ANALYSIS) MODELS
# ==========================================

class TicketDecision(BaseModel):
    should_create_ticket: bool = Field(..., description="True if ticket should be created")
    reason: str = Field(..., description="Reason for the decision")
    suggested_priority: Optional[TicketPriority] = Field(None, description="Suggested priority")
    suggested_category: Optional[str] = Field(None, description="Suggested category")
    auto_reply_hint: Optional[str] = Field(None, description="Hint for auto-reply if rejected")

# ==========================================
# TICKET ACTIVITY (LOGGING) MODELS
# ==========================================

class TicketActivityCreate(BaseModel):
    ticket_id: str
    action: str
    description: str
    actor_type: ActorType
    human_actor_id: Optional[str] = None
    ai_actor_id: Optional[str] = None
    metadata: Dict[str, Any] = {}

class TicketActivityResponse(BaseModel):
    id: str
    ticket_id: str
    action: str
    description: str
    actor_type: ActorType
    actor_name: str = "Unknown"
    created_at: datetime
    metadata: Dict[str, Any] = {}

    class Config:
        from_attributes = True

# ==========================================
# TICKET CRUD MODELS
# ==========================================

class TicketCreate(BaseModel):
    chat_id: str
    customer_id: str
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    category: Optional[str] = None
    priority: TicketPriority = TicketPriority.MEDIUM
    assigned_agent_id: Optional[str] = None
    tags: List[str] = []

class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[TicketPriority] = None
    status: Optional[TicketStatus] = None
    assigned_agent_id: Optional[str] = None
    tags: Optional[List[str]] = None

class Ticket(BaseModel):
    id: str
    organization_id: str
    ticket_number: str
    chat_id: str
    customer_id: str
    assigned_agent_id: Optional[str]
    title: str
    description: Optional[str]
    category: Optional[str]
    priority: TicketPriority
    status: TicketStatus
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    customer_name: Optional[str] = None
    customer: Optional[Dict[str, Any]] = None
    channel: Optional[str] = None

    class Config:
        from_attributes = True

class TicketListResponse(BaseModel):
    tickets: List[Ticket]
    total: int