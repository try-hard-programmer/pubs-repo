"""
WebSocket Service
Manages WebSocket connections for real-time notifications to frontend clients
"""
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, List, Set
import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections per organization.

    Connections are organized by organization_id to ensure:
    - Messages only broadcast to users in same organization
    - Organization-level isolation maintained
    - Efficient targeted broadcasting
    """

    def __init__(self):
        """Initialize connection manager"""
        # Structure: {organization_id: Set[WebSocket]}
        self.active_connections: Dict[str, Set[WebSocket]] = {}

        # Track connection metadata
        # Structure: {WebSocket: {"organization_id": str, "user_id": str, "connected_at": datetime}}
        self.connection_metadata: Dict[WebSocket, Dict] = {}

    async def connect(self, websocket: WebSocket, organization_id: str, user_id: str = None):
        """
        Register a new WebSocket connection.

        Note: WebSocket must already be accepted before calling this method.

        Args:
            websocket: WebSocket connection instance (already accepted)
            organization_id: Organization UUID
            user_id: Optional user UUID for tracking
        """
        # Add to organization connections
        if organization_id not in self.active_connections:
            self.active_connections[organization_id] = set()

        self.active_connections[organization_id].add(websocket)

        # Store metadata
        self.connection_metadata[websocket] = {
            "organization_id": organization_id,
            "user_id": user_id,
            "connected_at": datetime.utcnow()
        }

        connection_count = len(self.active_connections[organization_id])
        logger.info(
            f"✅ WebSocket connected: org={organization_id}, user={user_id}, "
            f"total_connections={connection_count}"
        )

    def disconnect(self, websocket: WebSocket):
        """
        Remove WebSocket connection from active connections.

        Args:
            websocket: WebSocket connection instance
        """
        # Get metadata before removing
        metadata = self.connection_metadata.get(websocket, {})
        organization_id = metadata.get("organization_id")
        user_id = metadata.get("user_id")

        # Remove from organization connections
        if organization_id and organization_id in self.active_connections:
            self.active_connections[organization_id].discard(websocket)

            # Clean up empty organization sets
            if not self.active_connections[organization_id]:
                del self.active_connections[organization_id]

        # Remove metadata
        self.connection_metadata.pop(websocket, None)

        remaining = len(self.active_connections.get(organization_id, []))
        logger.info(
            f"🔌 WebSocket disconnected: org={organization_id}, user={user_id}, "
            f"remaining_connections={remaining}"
        )

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        """
        Send message to specific WebSocket connection.

        Args:
            message: Message dictionary to send
            websocket: Target WebSocket connection
        """
        try:
            # Check if websocket is still in active connections
            if websocket not in self.connection_metadata:
                logger.warning("Attempted to send message to unregistered WebSocket")
                return

            await websocket.send_json(message)
            logger.debug(f"📤 Sent personal message: type={message.get('type')}")
        except RuntimeError as e:
            # Handle "WebSocket is not connected" errors
            logger.error(f"❌ WebSocket not connected: {e}")
            self.disconnect(websocket)
        except Exception as e:
            logger.error(f"❌ Failed to send personal message: {e}")
            # Optionally disconnect on other errors
            if "disconnect" in str(e).lower() or "closed" in str(e).lower():
                self.disconnect(websocket)

    async def broadcast_to_organization(self, message: dict, organization_id: str):
        """
        Broadcast message to all connections in an organization.

        Args:
            message: Message dictionary to broadcast
            organization_id: Organization UUID
        """
        if organization_id not in self.active_connections:
            logger.debug(f"No active connections for organization {organization_id}")
            return

        connections = list(self.active_connections[organization_id])
        success_count = 0
        failed_connections = []

        for connection in connections:
            try:
                await connection.send_json(message)
                success_count += 1
            except Exception as e:
                logger.error(f"❌ Failed to broadcast to connection: {e}")
                failed_connections.append(connection)

        # Clean up failed connections
        for failed in failed_connections:
            self.disconnect(failed)

        logger.info(
            f"📢 Broadcast to organization {organization_id}: "
            f"sent={success_count}, failed={len(failed_connections)}"
        )

    async def broadcast_new_message(
        self,
        organization_id: str,
        chat_id: str,
        message_id: str,
        customer_id: str,
        customer_name: str,
        message_content: str,
        channel: str,
        handled_by: str,
        sender_type: str, # <--- NEW
        sender_id: str,   # <--- NEW
        is_new_chat: bool = False,
        was_reopened: bool = False
    ):
        """
        Broadcast new incoming message notification to all organization members.

        Args:
            organization_id: Organization UUID
            chat_id: Chat UUID
            message_id: Message UUID
            customer_id: Customer UUID
            customer_name: Customer name
            message_content: Message text content
            channel: Communication channel
            handled_by: Who is handling the chat
            is_new_chat: Whether this is a new chat
            was_reopened: Whether chat was reopened
        """
        notification = {
            "type": "new_message",
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "chat_id": chat_id,
                "message_id": message_id,
                "customer_id": customer_id,
                "customer_name": customer_name,
                "message_content": message_content,
                "channel": channel,
                "handled_by": handled_by,
                "sender_type": sender_type, # <--- NEW PAYLOAD FIELD
                "sender_id": sender_id,     # <--- NEW PAYLOAD FIELD
                "is_new_chat": is_new_chat,
                "was_reopened": was_reopened
            }
        }

        await self.broadcast_to_organization(notification, organization_id)

    async def broadcast_chat_update(
        self,
        organization_id: str,
        chat_id: str,
        update_type: str,
        data: dict
    ):
        """
        Broadcast chat status update to organization members.

        Args:
            organization_id: Organization UUID
            chat_id: Chat UUID
            update_type: Type of update (status_changed, assigned, escalated, resolved, etc.)
            data: Update data dictionary
        """
        notification = {
            "type": "chat_update",
            "timestamp": datetime.utcnow().isoformat(),
            "update_type": update_type,
            "data": {
                "chat_id": chat_id,
                **data
            }
        }

        await self.broadcast_to_organization(notification, organization_id)

    def get_connection_count(self, organization_id: str = None) -> int:
        """
        Get number of active connections.

        Args:
            organization_id: Optional organization UUID to filter by

        Returns:
            Count of active connections
        """
        if organization_id:
            return len(self.active_connections.get(organization_id, set()))
        else:
            # Total across all organizations
            return sum(len(connections) for connections in self.active_connections.values())

    def get_organizations_with_connections(self) -> List[str]:
        """
        Get list of organization IDs that have active connections.

        Returns:
            List of organization UUIDs
        """
        return list(self.active_connections.keys())


# Singleton instance
connection_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """
    Get the global ConnectionManager singleton instance.

    Returns:
        ConnectionManager instance
    """
    return connection_manager
