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
            logger.info(f"🔍 [4. ROUTER LOOKUP] Searching DB for: '{contact}' (Channel: {channel})")
            logger.info(f"🔍 [DB LOOKUP] Table: customers, Query: organization_id={organization_id} AND phone='{contact}'")
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
                logger.warning(f"❌ [4. ROUTER LOOKUP] No match for '{contact}'. Creating duplicate entry.")
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
            else:
                # 📝 ADD THIS: This confirms why the duplicate is made
                logger.warning(f"❌ [DB MISMATCH] No customer found for '{contact}'. Creating NEW record.")
      
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
        organization_id: str,
        new_metadata: Optional[Dict[str, Any]] = None 
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
            # 1. Fetch current customer metadata
            customer_response = self.supabase.table("customers") \
                .select("metadata") \
                .eq("id", customer_id) \
                .execute()

            if not customer_response.data:
                return

            current_metadata = customer_response.data[0].get("metadata", {}) or {}
            now_iso = datetime.utcnow().isoformat()

            # 2. Perform Smart Merge
            # We preserve existing keys but update/add contact tracking
            updated_metadata = {
                **current_metadata,
                **(new_metadata or {}), # Merge incoming (whatsapp_name, whatsapp_lid, etc)
                "last_contact_at": now_iso,
                "message_count": current_metadata.get("message_count", 0) + 1,
                "preferred_channel": channel
            }

            # 3. Handle First Contact (Crucial for Dashboard-created records)
            if "first_contact_at" not in current_metadata:
                updated_metadata["first_contact_at"] = now_iso
            if "first_contact_channel" not in current_metadata:
                updated_metadata["first_contact_channel"] = channel

            # 4. Update channels list
            channels_used = current_metadata.get("channels_used", [])
            if channel not in channels_used:
                channels_used.append(channel)
            updated_metadata["channels_used"] = channels_used

            # 5. Save back to DB
            self.supabase.table("customers") \
                .update({"metadata": updated_metadata}) \
                .eq("id", customer_id) \
                .execute()

        except Exception as e:
            logger.error(f"❌ Metadata sync error: {e}")


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
