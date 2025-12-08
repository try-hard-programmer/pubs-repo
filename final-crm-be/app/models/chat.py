"""
Chat History Models

Pydantic models for chat topics and messages.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class MessageRole(str, Enum):
    """Role of the message sender"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# Request Models

class TopicCreate(BaseModel):
    """Schema for creating a new topic"""
    title: str = Field(..., min_length=1, max_length=255, description="Topic title")
    description: Optional[str] = Field(None, description="Optional topic description")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Discussion about Q1 Reports",
                "description": "Analyzing quarterly reports and metrics",
                "metadata": {"category": "business", "priority": "high"}
            }
        }


class TopicUpdate(BaseModel):
    """Schema for updating a topic"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_archived: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Updated Discussion about Q1 Reports",
                "is_archived": False
            }
        }


class MessageCreate(BaseModel):
    """Schema for creating a chat message"""
    topic_id: str = Field(..., description="UUID of the topic")
    role: MessageRole = Field(..., description="Role of the sender")
    content: str = Field(..., min_length=1, description="Message content")
    agent_name: Optional[str] = Field(None, description="Name of the agent (for assistant messages)")
    reference_documents: Optional[List[Dict[str, Any]]] = Field(
        default_factory=list,
        description="Referenced documents"
    )
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_schema_extra = {
            "example": {
                "topic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "role": "user",
                "content": "What are the key findings in the Q1 report?",
                "metadata": {}
            }
        }


# Response Models

class Topic(BaseModel):
    """Schema for topic response"""
    id: str = Field(..., description="Topic UUID")
    user_id: str = Field(..., description="Owner user UUID")
    title: str = Field(..., description="Topic title")
    description: Optional[str] = Field(None, description="Topic description")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    is_archived: bool = Field(False, description="Archive status")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "user_id": "user-uuid-123",
                "title": "Discussion about Q1 Reports",
                "description": "Analyzing quarterly reports",
                "created_at": "2025-10-10T10:00:00Z",
                "updated_at": "2025-10-10T10:00:00Z",
                "is_archived": False,
                "metadata": {}
            }
        }


class TopicWithStats(Topic):
    """Schema for topic with statistics"""
    message_count: int = Field(0, description="Total number of messages in topic")
    last_message_at: Optional[datetime] = Field(None, description="Timestamp of last message")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "user_id": "user-uuid-123",
                "title": "Discussion about Q1 Reports",
                "description": "Analyzing quarterly reports",
                "created_at": "2025-10-10T10:00:00Z",
                "updated_at": "2025-10-10T10:00:00Z",
                "is_archived": False,
                "metadata": {},
                "message_count": 15,
                "last_message_at": "2025-10-10T15:30:00Z"
            }
        }


class ChatMessage(BaseModel):
    """Schema for chat message response"""
    id: str = Field(..., description="Message UUID")
    topic_id: str = Field(..., description="Topic UUID")
    user_id: str = Field(..., description="User UUID")
    role: MessageRole = Field(..., description="Message role")
    content: str = Field(..., description="Message content")
    agent_name: Optional[str] = Field(None, description="Agent name (for assistant messages)")
    reference_documents: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Referenced documents"
    )
    created_at: datetime = Field(..., description="Creation timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "msg-uuid-123",
                "topic_id": "topic-uuid-456",
                "user_id": "user-uuid-789",
                "role": "user",
                "content": "What are the key findings?",
                "agent_name": None,
                "reference_documents": [],
                "created_at": "2025-10-10T10:00:00Z",
                "metadata": {}
            }
        }


class TopicWithMessages(Topic):
    """Schema for topic with all messages"""
    messages: List[ChatMessage] = Field(default_factory=list, description="List of messages in topic")

    class Config:
        from_attributes = True


# List Response Models

class TopicListResponse(BaseModel):
    """Schema for list of topics"""
    topics: List[TopicWithStats]
    total: int = Field(..., description="Total number of topics")
    page: int = Field(1, description="Current page number")
    page_size: int = Field(20, description="Number of items per page")

    class Config:
        json_schema_extra = {
            "example": {
                "topics": [],
                "total": 50,
                "page": 1,
                "page_size": 20
            }
        }


class MessageListResponse(BaseModel):
    """Schema for list of messages"""
    messages: List[ChatMessage]
    total: int = Field(..., description="Total number of messages")
    topic: Topic = Field(..., description="Topic information")

    class Config:
        json_schema_extra = {
            "example": {
                "messages": [],
                "total": 25,
                "topic": {}
            }
        }


# Agent Request with Topic

class AgentRequestWithTopic(BaseModel):
    """Agent request that includes topic_id for chat history"""
    topic_id: str = Field(..., description="Topic UUID for conversation context")
    query: str = Field(..., description="User's question or query")
    save_history: bool = Field(True, description="Whether to save this interaction to history")

    class Config:
        json_schema_extra = {
            "example": {
                "topic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "query": "What are the key findings in the Q1 report?",
                "save_history": True
            }
        }
