"""
Message Router Service
Handles message routing from external services (WhatsApp, Telegram, Email) to correct chats.
Based on MESSAGE_ROUTING_CHAT_MATCHING.md documentation.
"""
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from app.config import settings

logger = logging.getLogger(__name__)


class MessageRouterService:
    """Service for routing incoming messages to correct chats"""

    def __init__(self, supabase):
        """
        Initialize Message Router Service

        Args:
            supabase: Supabase client instance
        """
        self.supabase = supabase
        self.resolved_chat_reopen_enabled = True  # Enable reopening resolved chats

    async def find_or_create_customer(
        self,
        organization_id: str,
        channel: str,
        contact: str,
        customer_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Find existing customer or create new one based on contact info.

        Strategy:
        - WhatsApp: Find by phone number
        - Telegram: Find by telegram_id in metadata
        - Email: Find by email address

        Args:
            organization_id: Organization UUID
            channel: Communication channel (whatsapp, telegram, email)
            contact: Contact identifier (phone/email/telegram_id)
            customer_name: Optional customer name for creation
            metadata: Optional metadata for customer

        Returns:
            Customer data dict with id, name, and other fields

        Raises:
            Exception: If customer lookup/creation fails
        """
        try:
            logger.info(f"🔍 Finding customer: channel={channel}, contact={contact}")

            # Build query based on channel type
            if channel == "whatsapp":
                # Find by phone number
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("phone", contact)

            elif channel == "telegram":
                # Find by telegram_id in metadata
                # Using jsonb operator ->>
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("metadata->>telegram_id", contact)

            elif channel == "email":
                # Find by email
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("email", contact)

            elif channel == "web":
                # For web, we might use session_id or user_id
                # This is a placeholder - adjust based on your needs
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("metadata->>session_id", contact)

            else:
                raise ValueError(f"Unsupported channel: {channel}")

            # Execute query
            response = query.execute()

            # If customer found, check if we need to update the name
            if response.data:
                customer = response.data[0]
                current_name = customer.get('name', 'Unknown')

                # Helper function to check if a string is a phone number
                def is_phone_number(value: str) -> bool:
                    """Check if value looks like a phone number (starts with country code digits)"""
                    if not value:
                        return False
                    # Indonesian phone numbers typically start with 62, 08, or +62
                    # International phone numbers start with + or country code
                    return value.replace("+", "").replace("-", "").replace(" ", "").isdigit()

                # Update customer name if:
                # 1. Current name is "Unknown" OR looks like phone number AND
                # 2. New customer_name is provided AND is a real name (not Unknown, not phone number)
                should_update = (
                    (current_name == "Unknown" or is_phone_number(current_name)) and
                    customer_name and
                    customer_name != "Unknown" and
                    not is_phone_number(customer_name)
                )

                if should_update:
                    logger.info(
                        f"🔄 Updating customer name from '{current_name}' to '{customer_name}' "
                        f"for customer {customer['id']}"
                    )

                    # Update customer name
                    update_response = self.supabase.table("customers") \
                        .update({"name": customer_name}) \
                        .eq("id", customer["id"]) \
                        .execute()

                    if update_response.data:
                        customer = update_response.data[0]
                        logger.info(f"✅ Customer name updated successfully: {customer['id']} -> '{customer_name}'")
                    else:
                        logger.warning(f"⚠️ Failed to update customer name for {customer['id']}")

                logger.info(f"✅ Customer found: {customer['id']} ({customer.get('name', 'Unknown')})")
                return customer

            # If not found, create new customer
            logger.info(f"📝 Creating new customer for {channel}: {contact}")

            # [FIXED LOGIC]
            # 1. Phone Logic
            phone_val = None
            if channel == "whatsapp":
                phone_val = contact  # WhatsApp contact IS the phone
            elif channel == "telegram":
                phone_val = (metadata or {}).get("phone") # Telegram phone is in metadata

            # 2. Email Logic
            email_val = None
            if channel == "email":
                email_val = contact  # Email contact IS the email address
            elif channel == "telegram":
                # NEVER use 'contact' (ID) as email for Telegram
                email_val = (metadata or {}).get("email")

            customer_data = {
                "organization_id": organization_id,
                "name": customer_name or self._extract_name_from_contact(contact, channel),
                "phone": phone_val,
                "email": email_val,
                "metadata": metadata or {}
            }

            # Add channel-specific metadata
            if channel == "telegram":
                customer_data["metadata"]["telegram_id"] = contact
            elif channel == "web":
                customer_data["metadata"]["session_id"] = contact

            # Add tracking metadata
            customer_data["metadata"]["first_contact_at"] = datetime.utcnow().isoformat()
            customer_data["metadata"]["first_contact_channel"] = channel
            customer_data["metadata"]["message_count"] = 0
            customer_data["metadata"]["channels_used"] = [channel]

            # Create customer
            create_response = self.supabase.table("customers").insert(customer_data).execute()

            if not create_response.data:
                raise Exception("Failed to create customer")

            customer = create_response.data[0]
            logger.info(f"✅ Customer created: {customer['id']} ({customer['name']})")

            return customer

        except Exception as e:
            logger.error(f"❌ Error in find_or_create_customer: {e}")
            raise

    async def find_active_chat(
        self,
        customer_id: str,
        channel: str,
        organization_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find active chat for customer on specific channel.

        Strategy:
        1. Look for active chats (status = 'open' or 'assigned')
        2. If reopening enabled, also look for recently resolved chats
        3. Return most recent chat by last_message_at

        Args:
            customer_id: Customer UUID
            channel: Communication channel
            organization_id: Organization UUID

        Returns:
            Chat data dict if found, None otherwise
        """
        try:
            logger.info(f"🔍 Finding active chat: customer={customer_id}, channel={channel}")

            # Build base query
            query = self.supabase.table("chats") \
                .select("*") \
                .eq("customer_id", customer_id) \
                .eq("channel", channel) \
                .eq("organization_id", organization_id)

            # Include active chats and recently resolved chats (for reopening)
            if self.resolved_chat_reopen_enabled:
                # Include: open, assigned, or recently resolved
                query = query.in_("status", ["open", "assigned", "resolved"])
            else:
                # Only active chats
                query = query.in_("status", ["open", "assigned"])

            # Order by most recent and get first
            query = query.order("last_message_at", desc=True).limit(1)

            # Execute query
            response = query.execute()

            if response.data:
                chat = response.data[0]
                logger.info(
                    f"✅ Chat found: {chat['id']} "
                    f"(status={chat['status']}, handled_by={chat.get('handled_by', 'unknown')})"
                )
                return chat

            logger.info(f"ℹ️  No active chat found for customer {customer_id} on {channel}")
            return None

        except Exception as e:
            logger.error(f"❌ Error in find_active_chat: {e}")
            raise

    async def update_customer_metadata(
        self,
        customer_id: str,
        channel: str,
        organization_id: str
    ) -> None:
        """
        Update customer metadata with contact tracking info.

        Updates:
        - last_contact_at: Current timestamp
        - message_count: Increment by 1
        - preferred_channel: Most used channel
        - channels_used: List of channels customer has used

        Args:
            customer_id: Customer UUID
            channel: Communication channel
            organization_id: Organization UUID
        """
        try:
            logger.info(f"📊 Updating customer metadata: {customer_id}")

            # Get current customer data
            customer_response = self.supabase.table("customers") \
                .select("metadata") \
                .eq("id", customer_id) \
                .eq("organization_id", organization_id) \
                .execute()

            if not customer_response.data:
                logger.warning(f"⚠️  Customer {customer_id} not found for metadata update")
                return

            current_metadata = customer_response.data[0].get("metadata", {})

            # Update metadata
            updated_metadata = {
                **current_metadata,
                "last_contact_at": datetime.utcnow().isoformat(),
                "message_count": current_metadata.get("message_count", 0) + 1,
                "preferred_channel": channel,  # Simple strategy: last used channel
            }

            # Update channels_used list
            channels_used = current_metadata.get("channels_used", [])
            if channel not in channels_used:
                channels_used.append(channel)
            updated_metadata["channels_used"] = channels_used

            # Update customer
            self.supabase.table("customers") \
                .update({"metadata": updated_metadata}) \
                .eq("id", customer_id) \
                .execute()

            logger.info(
                f"✅ Customer metadata updated: message_count={updated_metadata['message_count']}, "
                f"preferred_channel={channel}"
            )

        except Exception as e:
            logger.error(f"❌ Error updating customer metadata: {e}")
            # Don't raise - metadata update failure shouldn't block message routing

    async def route_incoming_message(
        self,
        agent: Dict[str, Any],
        channel: str,
        contact: str,
        message_content: str,
        customer_name: Optional[str] = None,
        message_metadata: Optional[Dict[str, Any]] = None,
        customer_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Route incoming message to correct chat or create new chat.

        This is the main entry point for message routing logic.

        Flow:
        1. Extract organization_id and agent_id from agent object
        2. Find or create customer
        3. Find active/resolved chat
        4. If found:
           - Reopen if resolved
           - Add message to chat
        5. If not found:
           - Create new chat
           - Assign to agent (AI or Human based on agent.user_id)
           - Add initial message
        6. Update customer metadata
        7. Return routing result

        Args:
            agent: Agent object from agent_integrations lookup containing:
                - id: Agent UUID
                - organization_id: Organization UUID
                - user_id: User UUID (None for AI agent, UUID for human agent)
                - name: Agent name
                - integration_config: Integration config dict
            channel: Communication channel (whatsapp, telegram, email)
            contact: Contact identifier (phone/email/telegram_id)
            message_content: Message text content
            customer_name: Optional customer name
            message_metadata: Optional message metadata
            customer_metadata: Optional customer metadata for creation

        Returns:
            Routing result dict with:
                - chat_id: UUID of chat
                - message_id: UUID of message
                - customer_id: UUID of customer
                - is_new_chat: Boolean indicating if new chat was created
                - was_reopened: Boolean indicating if chat was reopened
                - handled_by: Who is handling the chat (ai/human/unassigned)
                - status: Chat status
                - agent_id: UUID of assigned agent
                - agent_name: Name of assigned agent
        """
        try:
            # Extract agent information
            organization_id = agent["organization_id"]
            agent_id = agent["id"]
            agent_name = agent["name"]
            is_ai_agent = agent["user_id"] is None

            logger.info(
                f"🚀 Routing message: org={organization_id}, agent={agent_name} "
                f"(is_ai={is_ai_agent}), channel={channel}, contact={contact}"
            )

            # Step 1: Find or create customer
            customer = await self.find_or_create_customer(
                organization_id=organization_id,
                channel=channel,
                contact=contact,
                customer_name=customer_name,
                metadata=customer_metadata
            )

            customer_id = customer["id"]

            # Step 2: Find active chat
            active_chat = await self.find_active_chat(
                customer_id=customer_id,
                channel=channel,
                organization_id=organization_id
            )

            # Initialize result variables
            chat_id = None
            message_id = None
            is_new_chat = False
            was_reopened = False
            handled_by = "unassigned"
            chat_status = "open"

            if active_chat:
                # Chat exists - add message to existing chat
                logger.info(f"📥 Adding message to existing chat: {active_chat['id']}")

                chat_id = active_chat["id"]
                chat_status = active_chat["status"]
                handled_by = active_chat.get("handled_by", "unassigned")

                # Check if chat was resolved and needs reopening
                if chat_status == "resolved":
                    logger.info(f"♻️  Reopening resolved chat: {chat_id}")

                    # Determine new status based on who was handling before
                    if handled_by == "ai":
                        new_status = "open"
                    elif handled_by == "human":
                        new_status = "assigned"
                    else:
                        new_status = "open"

                    # Reopen chat
                    self.supabase.table("chats") \
                        .update({
                            "status": new_status,
                            "last_message_at": datetime.utcnow().isoformat()
                        }) \
                        .eq("id", chat_id) \
                        .execute()

                    chat_status = new_status
                    was_reopened = True
                    logger.info(f"✅ Chat reopened: status={new_status}, handled_by={handled_by}")

                # Create message in existing chat
                message_data = {
                    "chat_id": chat_id,
                    "sender_type": "customer",
                    "sender_id": customer_id,
                    "content": message_content,
                    "metadata": message_metadata or {}
                }

                message_response = self.supabase.table("messages").insert(message_data).execute()

                if message_response.data:
                    message_id = message_response.data[0]["id"]
                    logger.info(f"✅ Message added to chat: {message_id}")

                    # Update chat's last_message_at
                    self.supabase.table("chats") \
                        .update({"last_message_at": datetime.utcnow().isoformat()}) \
                        .eq("id", chat_id) \
                        .execute()

            else:
                # No active chat - create new chat
                logger.info(f"📝 Creating new chat for customer: {customer_id}")

                chat_data = {
                    "organization_id": organization_id,
                    "customer_id": customer_id,
                    "channel": channel,
                    "sender_agent_id": agent_id,  # Track agent yang punya nomor WA/Telegram/Email
                    "unread_count": 1,
                    "last_message_at": datetime.utcnow().isoformat()
                }

                # Assign to agent (AI or Human based on agent type)
                if is_ai_agent:
                    # AI Agent
                    chat_data["ai_agent_id"] = agent_id
                    chat_data["assigned_agent_id"] = agent_id  # Legacy compatibility
                    chat_data["handled_by"] = "ai"
                    chat_data["status"] = "open"
                    handled_by = "ai"
                    logger.info(f"🤖 Assigning to AI agent: {agent_name}")
                else:
                    # Human Agent
                    chat_data["human_agent_id"] = agent_id
                    chat_data["assigned_agent_id"] = agent_id  # Legacy compatibility
                    chat_data["handled_by"] = "human"
                    chat_data["status"] = "assigned"
                    handled_by = "human"
                    logger.info(f"👤 Assigning to Human agent: {agent_name}")

                # Create chat
                chat_response = self.supabase.table("chats").insert(chat_data).execute()

                if chat_response.data:
                    chat_id = chat_response.data[0]["id"]
                    is_new_chat = True
                    logger.info(f"✅ New chat created: {chat_id}")

                    # Create initial message
                    message_data = {
                        "chat_id": chat_id,
                        "sender_type": "customer",
                        "sender_id": customer_id,
                        "content": message_content,
                        "metadata": message_metadata or {}
                    }

                    message_response = self.supabase.table("messages").insert(message_data).execute()

                    if message_response.data:
                        message_id = message_response.data[0]["id"]
                        logger.info(f"✅ Initial message created: {message_id}")

            # Step 3: Update customer metadata (contact tracking)
            await self.update_customer_metadata(
                customer_id=customer_id,
                channel=channel,
                organization_id=organization_id
            )

            # Prepare routing result
            result = {
                "success": True,
                "chat_id": chat_id,
                "message_id": message_id,
                "customer_id": customer_id,
                "is_new_chat": is_new_chat,
                "was_reopened": was_reopened,
                "handled_by": handled_by,
                "status": chat_status,
                "channel": channel,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "organization_id": organization_id
            }

            logger.info(
                f"✅ Message routed successfully: "
                f"chat={chat_id}, is_new={is_new_chat}, reopened={was_reopened}, "
                f"handled_by={handled_by}"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Error routing message: {e}")
            raise

    def _extract_name_from_contact(self, contact: str, channel: str) -> str:
        """
        Extract a display name from contact information.

        Args:
            contact: Contact identifier
            channel: Communication channel

        Returns:
            Generated display name
        """
        if channel == "email":
            # Extract name from email (before @)
            return contact.split("@")[0].replace(".", " ").title()
        elif channel == "whatsapp":
            # Use phone number as name temporarily
            return f"WhatsApp {contact}"
        elif channel == "telegram":
            # Use telegram ID as name temporarily
            return f"Telegram User {contact}"
        elif channel == "web":
            return "Web Visitor"
        else:
            return "Customer"


# Singleton instance
_message_router_service: Optional[MessageRouterService] = None


def get_message_router_service(supabase) -> MessageRouterService:
    """
    Get or create MessageRouterService instance.

    Args:
        supabase: Supabase client instance

    Returns:
        MessageRouterService instance
    """
    global _message_router_service
    if _message_router_service is None:
        _message_router_service = MessageRouterService(supabase)
    return _message_router_service
