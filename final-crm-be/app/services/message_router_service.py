"""
Message Router Service
Handles message routing from external services (WhatsApp, Telegram, Email) to correct chats.
Based on MESSAGE_ROUTING_CHAT_MATCHING.md documentation.
"""
import logging
import asyncio # <--- Added
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from app.config import settings
from app.services.redis_service import acquire_lock

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
        [FIX] Naming logic reverted to standard (Sender Name), distinguishing Groups via metadata only.
        """
        try:
            # =========================================================
            # 1. STRICT VALIDATION (Prevent Ghost Data)
            # =========================================================
            if not contact or str(contact).strip() == "" or str(contact).lower() == "none":
                logger.warning(f"🛑 [ROUTER] Aborting: Empty contact identifier received for {channel}")
                raise ValueError(f"Cannot create customer with empty contact for {channel}")

            logger.info(f"🔍 Finding customer: channel={channel}, contact={contact}")

            # =========================================================
            # 2. BUILD QUERY
            # =========================================================
            if channel == "whatsapp":
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("phone", contact)
            elif channel == "telegram":
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("metadata->>telegram_id", contact)
            elif channel == "email":
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("email", contact)
            elif channel == "web":
                query = self.supabase.table("customers") \
                    .select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("metadata->>session_id", contact)
            else:
                raise ValueError(f"Unsupported channel: {channel}")

            # Execute query
            response = query.execute()

            # =========================================================
            # 3. EXISTING CUSTOMER FOUND
            # =========================================================
            if response.data:
                customer = response.data[0]
                current_name = customer.get('name', 'Unknown')
                
                # Check if name update is needed
                def is_placeholder(value: str) -> bool:
                    if not value: return True
                    clean = value.replace("+", "").replace("-", "").replace(" ", "")
                    return clean.isdigit() or "WhatsApp" in value or value == "Unknown"

                # Update name if current is placeholder AND new name is valid
                should_update = (
                    is_placeholder(current_name) and
                    customer_name and
                    not is_placeholder(customer_name)
                )

                if should_update:
                    self.supabase.table("customers") \
                        .update({"name": customer_name}) \
                        .eq("id", customer["id"]) \
                        .execute()
                    logger.info(f"✅ Customer name updated: '{current_name}' -> '{customer_name}'")

                return customer

            # =========================================================
            # 4. CREATE NEW CUSTOMER
            # =========================================================
            logger.info(f"📝 Creating NEW customer for '{contact}'")
            
            # Prepare Fields
            phone_val = contact if channel == "whatsapp" else (metadata or {}).get("phone")
            email_val = contact if channel == "email" else (metadata or {}).get("email")
            
            # [REVERTED] Standard Naming Logic (Consistent with other records)
            final_name = customer_name
            if not final_name or final_name == "Unknown":
                final_name = self._extract_name_from_contact(contact, channel)

            # Extract is_group flag
            is_group = (metadata or {}).get("is_group", False)

            customer_data = {
                "organization_id": organization_id,
                "name": final_name,
                "phone": phone_val,
                "email": email_val,
                "metadata": {
                    **(metadata or {}),
                    "is_group": is_group, # Metadata distinguishes the type
                    "first_contact_at": datetime.utcnow().isoformat(),
                    "first_contact_channel": channel,
                    "message_count": 0,
                    "channels_used": [channel]
                }
            }

            # Enforce channel metadata
            if channel == "telegram":
                customer_data["metadata"]["telegram_id"] = contact
            elif channel == "web":
                customer_data["metadata"]["session_id"] = contact

            # Insert
            create_response = self.supabase.table("customers").insert(customer_data).execute()

            if not create_response.data:
                raise Exception("Failed to create customer")

            customer = create_response.data[0]
            logger.info(f"✅ Customer created: {customer['id']} ({customer['name']})")

            return customer

        except Exception as e:
            logger.error(f"❌ Customer lookup/creation failed: {e}")
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
        [FIXED] Routes message with Blocking Lock.
        Prevents duplicate customers/chats by forcing sequential processing for the same contact.
        """
        organization_id = agent["organization_id"]
        
        # LOCK KEY: Unique to the Organization + Contact
        lock_key = f"router:{organization_id}:{contact}"
        
        # Wait up to 5 seconds for the lock. 
        async with acquire_lock(lock_key, expire=20, wait_time=5) as acquired:
            if not acquired:
                logger.warning(f"🔒 Router Lock Timeout for {contact}. Rejecting request to prevent duplicate.")
                raise Exception("System busy processing previous message for this user. Please retry.")

            # ✅ LOCK ACQUIRED: Run logic safely. 
            return await self._execute_routing_logic(
                agent, channel, contact, message_content, 
                customer_name, message_metadata, customer_metadata
            )
    async def _execute_routing_logic(self, agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata):
        """
        Internal method containing 100% of your original routing logic.
        Protected by the Lock in route_incoming_message.
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
            is_merged_event = False 

            # Robust Duplicate Check (Your original fix)
            wa_msg_id = (message_metadata or {}).get("whatsapp_message_id")
            existing_message = None
            
            if active_chat and wa_msg_id:
                try:
                    check_res = self.supabase.table("messages") \
                        .select("id, content, metadata") \
                        .eq("chat_id", active_chat["id"]) \
                        .contains("metadata", {"whatsapp_message_id": wa_msg_id}) \
                        .execute()
                    
                    if check_res.data:
                        existing_message = check_res.data[0]
                except Exception as e:
                    logger.warning(f"⚠️ Metadata lookup warning: {e}")

            if active_chat:
                # Chat exists
                chat_id = active_chat["id"]
                chat_status = active_chat["status"]
                handled_by = active_chat.get("handled_by", "unassigned")
                current_assigned_id = active_chat.get("assigned_agent_id")

                # [PROD FIX] Validate Assignment Integrity
                # If chat claims to be 'assigned' but has no valid Agent ID, recover to AI.
                if chat_status == "assigned" or handled_by == "human":
                    if not current_assigned_id:
                        logger.warning(f"⚠️ Chat {chat_id} is 'assigned' but has NO Agent ID. Auto-recovering to AI.")
                        chat_status = "open"
                        handled_by = "ai"
                        # We will apply this status update to DB in the insert block below

                if existing_message:
                    # MERGE SPLIT EVENTS (Your original logic)
                    logger.info(f"🔄 Merging split message events for ID: {wa_msg_id}")
                    message_id = existing_message["id"]
                    is_merged_event = True
                    
                    current_meta = existing_message.get("metadata") or {}
                    new_meta = message_metadata or {}
                    merged_meta = {**current_meta, **new_meta}
                    
                    update_data = {"metadata": merged_meta}
                    if message_content and not existing_message.get("content"):
                        update_data["content"] = message_content
                        
                    self.supabase.table("messages").update(update_data).eq("id", message_id).execute()
                    
                else:
                    # INSERT NEW MESSAGE
                    logger.info(f"📥 Adding message to existing chat: {chat_id}")

                    # Prepare Chat Update Payload (Consolidated)
                    chat_update = {
                        "last_message_at": datetime.utcnow().isoformat()
                    }

                    # Handle Reopening logic
                    if chat_status == "resolved":
                        logger.info(f"♻️ Reopening resolved chat: {chat_id}")
                        if handled_by == "ai": new_status = "open"
                        elif handled_by == "human": new_status = "assigned"
                        else: new_status = "open"

                        chat_status = new_status
                        chat_update["status"] = new_status
                        was_reopened = True
                    
                    # [PROD FIX] Apply Auto-Recovery Status if changed above
                    if chat_status != active_chat["status"]:
                        chat_update["status"] = chat_status
                        if chat_status == "open":
                            chat_update["handled_by"] = "ai"
                            if not current_assigned_id:
                                chat_update["assigned_agent_id"] = None
                    
                    # [PROD FIX] Increment Unread Count
                    current_unread = active_chat.get("unread_count", 0) or 0
                    chat_update["unread_count"] = current_unread + 1

                    # Execute Consolidated Chat Update
                    self.supabase.table("chats").update(chat_update).eq("id", chat_id).execute()

                    # Insert Message
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

            else:
                # CREATE NEW CHAT
                logger.info(f"📝 Creating new chat for customer: {customer_id}")

                chat_data = {
                    "organization_id": organization_id,
                    "customer_id": customer_id,
                    "channel": channel,
                    "sender_agent_id": agent_id,
                    "unread_count": 1,
                    "last_message_at": datetime.utcnow().isoformat()
                }

                if is_ai_agent:
                    chat_data.update({"ai_agent_id": agent_id, "assigned_agent_id": agent_id, "handled_by": "ai", "status": "open"})
                    handled_by = "ai"
                else:
                    chat_data.update({"human_agent_id": agent_id, "assigned_agent_id": agent_id, "handled_by": "human", "status": "assigned"})
                    handled_by = "human"

                chat_response = self.supabase.table("chats").insert(chat_data).execute()

                if chat_response.data:
                    chat_id = chat_response.data[0]["id"]
                    is_new_chat = True
                    logger.info(f"✅ New chat created: {chat_id}")

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

            # Step 3: Update customer metadata
            await self.update_customer_metadata(
                customer_id=customer_id,
                channel=channel,
                organization_id=organization_id,
                new_metadata=customer_metadata
            )

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
                "organization_id": organization_id,
                "is_merged_event": is_merged_event
            }

            logger.info(f"✅ Message routed successfully: chat={chat_id}")
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
