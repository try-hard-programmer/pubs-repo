"""
Chat History API Endpoints

Provides HTTP endpoints for managing conversation topics and chat history.
"""
from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
import logging

from app.auth.dependencies import get_current_user
from app.models.user import User
from app.models.chat import (
    Topic, TopicCreate, TopicUpdate, TopicWithStats,
    ChatMessage, TopicListResponse, MessageListResponse,
    TopicWithMessages
)
from app.services.chat_service import get_chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat-history"])


# ============ Topic Endpoints ============

@router.post("/topics", response_model=Topic, status_code=201)
async def create_topic(
    topic_data: TopicCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new conversation topic.

    Topics are used to organize chat conversations. Before starting a conversation,
    create a topic to group related messages together.

    Args:
        topic_data: Topic creation data (title, description, metadata)
        current_user: Authenticated user from JWT token

    Returns:
        Created topic with ID

    Example:
        ```json
        {
            "title": "Discussion about Q1 Reports",
            "description": "Analyzing quarterly reports and metrics",
            "metadata": {"category": "business"}
        }
        ```
    """
    try:
        chat_service = get_chat_service()
        topic = await chat_service.create_topic(
            user_id=current_user.user_id,
            topic_data=topic_data
        )
        logger.info(f"User {current_user.email} created topic: {topic.id}")
        return topic

    except RuntimeError as e:
        logger.error(f"Failed to create topic: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        logger.error(f"Unexpected error creating topic: {e}")
        raise HTTPException(status_code=500, detail="Failed to create topic")


@router.get("/topics", response_model=TopicListResponse)
async def list_topics(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    include_archived: bool = Query(False, description="Include archived topics"),
    current_user: User = Depends(get_current_user)
):
    """
    List all topics for the authenticated user.

    Returns topics with statistics (message count, last message time) ordered by
    most recently updated first.

    Args:
        page: Page number (1-indexed)
        page_size: Number of topics per page (max 100)
        include_archived: Whether to include archived topics
        current_user: Authenticated user from JWT token

    Returns:
        Paginated list of topics with statistics
    """
    try:
        chat_service = get_chat_service()
        topics, total = await chat_service.list_topics(
            user_id=current_user.user_id,
            page=page,
            page_size=page_size,
            include_archived=include_archived
        )

        return TopicListResponse(
            topics=topics,
            total=total,
            page=page,
            page_size=page_size
        )

    except Exception as e:
        logger.error(f"Error listing topics: {e}")
        raise HTTPException(status_code=500, detail="Failed to list topics")


@router.get("/topics/{topic_id}", response_model=Topic)
async def get_topic(
    topic_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific topic by ID.

    Args:
        topic_id: UUID of the topic
        current_user: Authenticated user from JWT token

    Returns:
        Topic details

    Raises:
        404: If topic not found or user doesn't have permission
    """
    try:
        chat_service = get_chat_service()
        topic = await chat_service.get_topic(topic_id, current_user.user_id)

        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")

        return topic

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch topic")


@router.patch("/topics/{topic_id}", response_model=Topic)
async def update_topic_patch(
    topic_id: str,
    topic_data: TopicUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Partially update a topic (PATCH).

    Can update title, description, archive status, or metadata.
    Only provided fields will be updated.

    Args:
        topic_id: UUID of the topic
        topic_data: Update data (all fields optional)
        current_user: Authenticated user from JWT token

    Returns:
        Updated topic

    Raises:
        404: If topic not found or user doesn't have permission
    """
    try:
        chat_service = get_chat_service()
        topic = await chat_service.update_topic(
            topic_id=topic_id,
            user_id=current_user.user_id,
            topic_data=topic_data
        )

        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")

        logger.info(f"Updated topic {topic_id} via PATCH")
        return topic

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update topic")


@router.put("/topics/{topic_id}", response_model=Topic)
async def update_topic_put(
    topic_id: str,
    topic_data: TopicUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update a topic (PUT).

    Can update title, description, archive status, or metadata.
    Only provided fields will be updated.

    Args:
        topic_id: UUID of the topic
        topic_data: Update data (all fields optional)
        current_user: Authenticated user from JWT token

    Returns:
        Updated topic

    Raises:
        404: If topic not found or user doesn't have permission

    Example:
        ```bash
        curl -X PUT https://api.syntra.id/chat/topics/{topic_id} \\
          -H "Authorization: Bearer YOUR_TOKEN" \\
          -H "Content-Type: application/json" \\
          -d '{"title": "New Title"}'
        ```
    """
    try:
        chat_service = get_chat_service()
        topic = await chat_service.update_topic(
            topic_id=topic_id,
            user_id=current_user.user_id,
            topic_data=topic_data
        )

        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")

        logger.info(f"Updated topic {topic_id} via PUT")
        return topic

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update topic")


@router.delete("/topics/{topic_id}", status_code=204)
async def delete_topic(
    topic_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a topic and all its messages.

    Warning: This operation is irreversible. All messages in this topic
    will also be deleted.

    Args:
        topic_id: UUID of the topic
        current_user: Authenticated user from JWT token

    Returns:
        204 No Content on success

    Raises:
        404: If topic not found or user doesn't have permission
    """
    try:
        chat_service = get_chat_service()
        success = await chat_service.delete_topic(topic_id, current_user.user_id)

        if not success:
            raise HTTPException(status_code=404, detail="Topic not found")

        logger.info(f"Deleted topic {topic_id}")
        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete topic")


# ============ Message Endpoints ============

@router.get("/topics/{topic_id}/messages", response_model=MessageListResponse)
async def get_topic_messages(
    topic_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get all messages for a specific topic.

    Returns all messages in chronological order (oldest first).

    Args:
        topic_id: UUID of the topic
        current_user: Authenticated user from JWT token

    Returns:
        List of messages with topic information

    Raises:
        404: If topic not found or user doesn't have permission
    """
    try:
        chat_service = get_chat_service()

        # Get topic info
        topic = await chat_service.get_topic(topic_id, current_user.user_id)
        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")

        # Get messages
        messages = await chat_service.get_messages(topic_id, current_user.user_id)

        return MessageListResponse(
            messages=messages,
            total=len(messages),
            topic=topic
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching messages for topic {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@router.get("/topics/{topic_id}/full", response_model=TopicWithMessages)
async def get_topic_with_messages(
    topic_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a topic with all its messages.

    Convenience endpoint that returns both topic details and all messages
    in a single response.

    Args:
        topic_id: UUID of the topic
        current_user: Authenticated user from JWT token

    Returns:
        Topic with embedded messages

    Raises:
        404: If topic not found or user doesn't have permission
    """
    try:
        chat_service = get_chat_service()

        # Get topic info
        topic = await chat_service.get_topic(topic_id, current_user.user_id)
        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")

        # Get messages
        messages = await chat_service.get_messages(topic_id, current_user.user_id)

        # Combine into TopicWithMessages
        topic_dict = topic.model_dump()
        topic_dict["messages"] = messages

        return TopicWithMessages(**topic_dict)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching topic with messages {topic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch topic")


# ============ Health/Status Endpoint ============

@router.get("/health")
async def chat_health():
    """
    Health check for chat history service.

    Returns:
        Service health status
    """
    try:
        chat_service = get_chat_service()
        is_configured = chat_service._client is not None

        return {
            "status": "healthy" if is_configured else "not_configured",
            "configured": is_configured,
            "service": "chat_history"
        }
    except Exception as e:
        logger.error(f"Chat health check failed: {e}")
        return {
            "status": "unhealthy",
            "configured": False,
            "service": "chat_history",
            "error": str(e)
        }
