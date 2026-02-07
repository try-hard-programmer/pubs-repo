"""
WebSocket API Endpoint
Real-time communication endpoint for chat notifications
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, status
import logging
import jwt
import asyncio
from datetime import datetime
from typing import Optional

from app.services.websocket_service import get_connection_manager
from app.config import settings
from app.services.organization_service import get_organization_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


async def verify_websocket_token(token: str) -> dict:
    """
    Verify JWT token for WebSocket connection.

    Args:
        token: JWT token string

    Returns:
        Decoded token payload with user info

    Raises:
        HTTPException: If token is invalid
    """
    try:
        # Decode JWT token using same configuration as regular API endpoints
        # This ensures consistency with jwt_handler.py
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,  # Use Supabase JWT secret (not JWT_SECRET_KEY)
            algorithms=["HS256"],
            audience="authenticated",  # Supabase default audience
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True  # Verify audience to match "authenticated"
            }
        )

        # Extract user info
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("Token missing user ID")

        return {
            "user_id": user_id,
            "email": payload.get("email"),
            "role": payload.get("role")
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {str(e)}"
        )


@router.websocket("/ws/{organization_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    organization_id: str,
    token: Optional[str] = Query(None, description="JWT authentication token")
):
    """
    WebSocket endpoint for real-time chat notifications.

    **Connection URL:**
    ```
    ws://your-api.com/ws/{organization_id}?token={jwt_token}
    ```

    **Authentication:**
    - Requires JWT token in query parameter
    - Token must be valid and not expired
    - User must belong to the specified organization

    **Message Types Received:**

    1. **New Message Notification:**
    ```json
    {
        "type": "new_message",
        "timestamp": "2025-10-21T15:30:00Z",
        "data": {
            "chat_id": "chat-uuid",
            "message_id": "msg-uuid",
            "customer_id": "customer-uuid",
            "customer_name": "John Doe",
            "message_content": "Hello, I need help",
            "channel": "whatsapp",
            "handled_by": "ai",
            "is_new_chat": false,
            "was_reopened": true
        }
    }
    ```

    2. **Chat Update Notification:**
    ```json
    {
        "type": "chat_update",
        "timestamp": "2025-10-21T15:30:00Z",
        "update_type": "escalated",
        "data": {
            "chat_id": "chat-uuid",
            "from_agent": "ai-agent-uuid",
            "to_agent": "human-agent-uuid",
            "reason": "Customer requested human"
        }
    }
    ```

    **Frontend Integration Example (JavaScript/TypeScript):**
    ```javascript
    const ws = new WebSocket(
        `ws://api.example.com/ws/${organizationId}?token=${jwtToken}`
    );

    ws.onmessage = (event) => {
        const notification = JSON.parse(event.data);

        if (notification.type === 'new_message') {
            // Update chat list
            updateChatList(notification.data.chat_id);
            // Show notification badge
            showNotificationBadge(notification.data.customer_name);
        } else if (notification.type === 'chat_update') {
            // Refresh chat details
            refreshChatDetails(notification.data.chat_id);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };

    ws.onclose = () => {
        console.log('WebSocket connection closed');
        // Implement reconnection logic
    };
    ```

    Args:
        websocket: WebSocket connection instance
        organization_id: Organization UUID to subscribe to
        token: JWT authentication token (query parameter)

    **Connection Flow:**
    1. Client connects with JWT token
    2. Server validates token
    3. Server verifies user belongs to organization
    4. Connection accepted and added to organization's connection pool
    5. Client receives real-time notifications for that organization
    6. Connection maintained until client disconnects or error occurs
    """
    connection_manager = get_connection_manager()
    user_id = None

    try:
        # Validate JWT token
        if not token:
            logger.warning(f"WebSocket connection attempt without token for org {organization_id}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Verify token and get user info
        try:
            user_info = await verify_websocket_token(token)
            user_id = user_info["user_id"]
            logger.info(f"✅ WebSocket token verified for user {user_id}")
        except HTTPException as e:
            logger.warning(f"Invalid WebSocket token: {e.detail}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Verify user belongs to organization
        try:
            from app.models.user import User
            org_service = get_organization_service()
            user_org = await org_service.get_user_organization(user_id)

            if not user_org or user_org.id != organization_id:
                logger.warning(
                    f"User {user_id} attempted to connect to unauthorized org {organization_id}"
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        except Exception as e:
            logger.error(f"Error verifying user organization: {e}")
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return

        # Accept WebSocket connection
        await websocket.accept()
        logger.debug(f"✅ WebSocket accepted for user {user_id}, org {organization_id}")

        # Register connection in manager
        await connection_manager.connect(websocket, organization_id, user_id)

        # Send welcome message - CRITICAL for immediate acknowledgment
        # This prevents browser timeout and confirms connection established
        await connection_manager.send_personal_message(
            {
                "type": "connection_established",
                "message": f"Connected to organization {organization_id}",
                "connection_count": connection_manager.get_connection_count(organization_id)
            },
            websocket
        )

        # Keep connection alive and handle incoming messages with ping/pong mechanism
        logger.info(f"🔄 Starting WebSocket keepalive loop for user={user_id}, org={organization_id}")

        while True:
            try:
                # Wait for message from client with 30 second timeout
                # This prevents indefinite blocking and allows us to send periodic pings
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0
                )

                logger.debug(f"📩 Received from client: {data}")

                # Handle client messages
                try:
                    import json
                    message = json.loads(data)
                    message_type = message.get("type", "")

                    # Handle ping from client
                    if message_type == "ping":
                        await connection_manager.send_personal_message(
                            {
                                "type": "pong",
                                "timestamp": datetime.utcnow().isoformat()
                            },
                            websocket
                        )
                        logger.debug(f"🏓 Sent pong response to user={user_id}")

                    # Handle pong from client (response to our ping)
                    elif message_type == "pong":
                        logger.debug(f"🏓 Received pong from user={user_id}")

                    # Echo back any other message
                    else:
                        await connection_manager.send_personal_message(
                            {
                                "type": "echo",
                                "data": data
                            },
                            websocket
                        )

                except json.JSONDecodeError:
                    # If not JSON, just echo back as plain text
                    await connection_manager.send_personal_message(
                        {
                            "type": "echo",
                            "data": data
                        },
                        websocket
                    )

            except asyncio.TimeoutError:
                # No message received in 30 seconds, send ping to keep connection alive
                try:
                    await connection_manager.send_personal_message(
                        {
                            "type": "ping",
                            "timestamp": datetime.utcnow().isoformat(),
                            "message": "keepalive"
                        },
                        websocket
                    )
                    logger.debug(f"🏓 Sent keepalive ping to user={user_id}")
                except Exception as ping_error:
                    logger.error(f"Failed to send ping: {ping_error}")
                    # If we can't send ping, connection is likely dead
                    break

            except WebSocketDisconnect:
                logger.info(f"WebSocket client disconnected: user={user_id}, org={organization_id}")
                break

            except Exception as e:
                logger.error(f"Error in WebSocket loop: {e}")
                break

    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {e}")

    finally:
        # Cleanup connection
        connection_manager.disconnect(websocket)


@router.get(
    "/ws/stats",
    summary="Get WebSocket connection statistics",
    description="Get statistics about active WebSocket connections"
)
async def get_websocket_stats():
    """
    Get WebSocket connection statistics.

    Returns statistics about active connections across all organizations.

    **Response Example:**
    ```json
    {
        "total_connections": 15,
        "organizations_with_connections": 3,
        "connections_by_organization": {
            "org-uuid-1": 5,
            "org-uuid-2": 7,
            "org-uuid-3": 3
        }
    }
    ```

    Returns:
        Dictionary with connection statistics
    """
    connection_manager = get_connection_manager()

    organizations = connection_manager.get_organizations_with_connections()
    connections_by_org = {
        org_id: connection_manager.get_connection_count(org_id)
        for org_id in organizations
    }

    stats = {
        "total_connections": connection_manager.get_connection_count(),
        "organizations_with_connections": len(organizations),
        "connections_by_organization": connections_by_org
    }

    logger.info(f"📊 WebSocket stats requested: {stats}")

    return stats
