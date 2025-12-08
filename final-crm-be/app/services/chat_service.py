"""
Chat History Service

Service layer for managing chat topics and messages in Supabase.
"""
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from supabase import create_client, Client

from app.config import settings
from app.models.chat import (
    Topic, TopicCreate, TopicUpdate, TopicWithStats,
    ChatMessage, MessageCreate, MessageRole
)

logger = logging.getLogger(__name__)


class ChatService:
    """Service for managing chat history in Supabase"""

    def __init__(self):
        """Initialize Supabase client"""
        if not settings.is_supabase_configured:
            logger.warning("Supabase not configured. Chat history features will not work.")
            self._client: Optional[Client] = None
        else:
            # Use service role key for backend operations (bypasses RLS)
            # Falls back to anon key if service key not available
            supabase_key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY

            if settings.SUPABASE_SERVICE_KEY:
                logger.info("Using Supabase service role key (RLS bypassed)")
            else:
                logger.warning("Using Supabase anon key - RLS must be disabled or properly configured")

            self._client: Client = create_client(
                settings.SUPABASE_URL,
                supabase_key
            )

    @property
    def client(self) -> Client:
        """Get Supabase client with error handling"""
        if self._client is None:
            raise RuntimeError("Supabase client not initialized. Check configuration.")
        return self._client

    # ============ Topic Operations ============

    async def create_topic(self, user_id: str, topic_data: TopicCreate) -> Topic:
        """
        Create a new conversation topic.

        Args:
            user_id: UUID of the user creating the topic
            topic_data: Topic creation data

        Returns:
            Created topic

        Raises:
            RuntimeError: If creation fails
        """
        try:
            data = {
                "user_id": user_id,
                "title": topic_data.title,
                "description": topic_data.description,
                "metadata": topic_data.metadata or {}
            }

            response = self.client.table("topics").insert(data).execute()

            if not response.data:
                raise RuntimeError("Failed to create topic")

            logger.info(f"Created topic '{topic_data.title}' for user {user_id}")
            return Topic(**response.data[0])

        except Exception as e:
            logger.error(f"Error creating topic: {e}")
            raise RuntimeError(f"Failed to create topic: {str(e)}")

    async def get_topic(self, topic_id: str, user_id: str) -> Optional[Topic]:
        """
        Get a specific topic by ID.

        Args:
            topic_id: UUID of the topic
            user_id: UUID of the user (for permission check)

        Returns:
            Topic if found and owned by user, None otherwise
        """
        try:
            response = self.client.table("topics").select("*").eq("id", topic_id).eq("user_id", user_id).execute()

            if not response.data:
                return None

            return Topic(**response.data[0])

        except Exception as e:
            logger.error(f"Error fetching topic {topic_id}: {e}")
            return None

    async def list_topics(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        include_archived: bool = False
    ) -> tuple[List[TopicWithStats], int]:
        """
        List all topics for a user with pagination.

        Args:
            user_id: UUID of the user
            page: Page number (1-indexed)
            page_size: Number of items per page
            include_archived: Whether to include archived topics

        Returns:
            Tuple of (list of topics, total count)
        """
        try:
            # Build query
            query = self.client.table("topics_with_stats").select("*", count="exact").eq("user_id", user_id)

            # Filter archived
            if not include_archived:
                query = query.eq("is_archived", False)

            # Order by updated_at descending (most recent first)
            query = query.order("updated_at", desc=True)

            # Pagination
            start = (page - 1) * page_size
            end = start + page_size - 1
            query = query.range(start, end)

            # Execute
            response = query.execute()

            topics = [TopicWithStats(**item) for item in response.data]
            total = response.count or 0

            logger.info(f"Listed {len(topics)} topics for user {user_id}")
            return topics, total

        except Exception as e:
            logger.error(f"Error listing topics: {e}")
            return [], 0

    async def update_topic(self, topic_id: str, user_id: str, topic_data: TopicUpdate) -> Optional[Topic]:
        """
        Update a topic.

        Args:
            topic_id: UUID of the topic
            user_id: UUID of the user (for permission check)
            topic_data: Update data

        Returns:
            Updated topic if successful, None otherwise
        """
        try:
            # Build update data (only include fields that are set)
            update_data = {}
            if topic_data.title is not None:
                update_data["title"] = topic_data.title
            if topic_data.description is not None:
                update_data["description"] = topic_data.description
            if topic_data.is_archived is not None:
                update_data["is_archived"] = topic_data.is_archived
            if topic_data.metadata is not None:
                update_data["metadata"] = topic_data.metadata

            if not update_data:
                # No updates to perform
                return await self.get_topic(topic_id, user_id)

            response = (
                self.client.table("topics")
                .update(update_data)
                .eq("id", topic_id)
                .eq("user_id", user_id)
                .execute()
            )

            if not response.data:
                return None

            logger.info(f"Updated topic {topic_id}")
            return Topic(**response.data[0])

        except Exception as e:
            logger.error(f"Error updating topic {topic_id}: {e}")
            return None

    async def delete_topic(self, topic_id: str, user_id: str) -> bool:
        """
        Delete a topic and all its messages (cascade).

        Args:
            topic_id: UUID of the topic
            user_id: UUID of the user (for permission check)

        Returns:
            True if deleted, False otherwise
        """
        try:
            response = (
                self.client.table("topics")
                .delete()
                .eq("id", topic_id)
                .eq("user_id", user_id)
                .execute()
            )

            success = len(response.data) > 0
            if success:
                logger.info(f"Deleted topic {topic_id}")
            return success

        except Exception as e:
            logger.error(f"Error deleting topic {topic_id}: {e}")
            return False

    # ============ Message Operations ============

    async def create_message(self, user_id: str, message_data: MessageCreate) -> ChatMessage:
        """
        Create a new chat message.

        Args:
            user_id: UUID of the user
            message_data: Message creation data

        Returns:
            Created message

        Raises:
            RuntimeError: If creation fails
        """
        try:
            data = {
                "topic_id": message_data.topic_id,
                "user_id": user_id,
                "role": message_data.role.value,
                "content": message_data.content,
                "agent_name": message_data.agent_name,
                "reference_documents": message_data.reference_documents or [],
                "metadata": message_data.metadata or {}
            }

            response = self.client.table("chat_messages").insert(data).execute()

            if not response.data:
                raise RuntimeError("Failed to create message")

            logger.info(f"Created {message_data.role} message in topic {message_data.topic_id}")
            return ChatMessage(**response.data[0])

        except Exception as e:
            logger.error(f"Error creating message: {e}")
            raise RuntimeError(f"Failed to create message: {str(e)}")

    async def get_messages(self, topic_id: str, user_id: str) -> List[ChatMessage]:
        """
        Get all messages for a topic.

        Args:
            topic_id: UUID of the topic
            user_id: UUID of the user (for permission check)

        Returns:
            List of messages ordered by creation time
        """
        try:
            # First verify user owns the topic
            topic = await self.get_topic(topic_id, user_id)
            if not topic:
                logger.warning(f"Topic {topic_id} not found or not owned by user {user_id}")
                return []

            # Get messages
            response = (
                self.client.table("chat_messages")
                .select("*")
                .eq("topic_id", topic_id)
                .order("created_at", desc=False)
                .execute()
            )

            messages = [ChatMessage(**item) for item in response.data]
            logger.info(f"Retrieved {len(messages)} messages for topic {topic_id}")
            return messages

        except Exception as e:
            logger.error(f"Error fetching messages for topic {topic_id}: {e}")
            return []

    async def save_conversation(
        self,
        topic_id: str,
        user_id: str,
        user_message: str,
        assistant_message: str,
        agent_name: str,
        reference_documents: Optional[List[Dict[str, Any]]] = None
    ) -> tuple[ChatMessage, ChatMessage]:
        """
        Save a complete conversation turn (user question + assistant answer).

        Args:
            topic_id: UUID of the topic
            user_id: UUID of the user
            user_message: User's question/query
            assistant_message: Assistant's response
            agent_name: Name of the agent that generated the response
            reference_documents: Optional list of referenced documents

        Returns:
            Tuple of (user_message, assistant_message)

        Raises:
            RuntimeError: If saving fails
        """
        try:
            # Create user message
            user_msg = await self.create_message(
                user_id=user_id,
                message_data=MessageCreate(
                    topic_id=topic_id,
                    role=MessageRole.USER,
                    content=user_message,
                    metadata={}
                )
            )

            # Create assistant message
            assistant_msg = await self.create_message(
                user_id=user_id,
                message_data=MessageCreate(
                    topic_id=topic_id,
                    role=MessageRole.ASSISTANT,
                    content=assistant_message,
                    agent_name=agent_name,
                    reference_documents=reference_documents or [],
                    metadata={}
                )
            )

            logger.info(f"Saved conversation turn in topic {topic_id}")
            return user_msg, assistant_msg

        except Exception as e:
            logger.error(f"Error saving conversation: {e}")
            raise RuntimeError(f"Failed to save conversation: {str(e)}")


# Global chat service instance
_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    """Get or create global chat service instance"""
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service
