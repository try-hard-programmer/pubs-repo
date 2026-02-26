# """
# Message Router Service
# Handles message routing from external services (WhatsApp, Telegram, Email) to correct chats.
# Based on MESSAGE_ROUTING_CHAT_MATCHING.md documentation.
# """
# import logging
# import asyncio 
# from typing import Optional, Dict, Any, Tuple
# from datetime import datetime, timedelta
# from app.config import settings
# from app.services.redis_service import acquire_lock
# from uuid import uuid4

# logger = logging.getLogger(__name__)

# class MessageRouterService:
#     """Service for routing incoming messages to correct chats"""

#     def __init__(self, supabase):
#         """
#         Initialize Message Router Service

#         Args:
#             supabase: Supabase client instance
#         """
#         self.supabase = supabase
#         self.resolved_chat_reopen_enabled = True  # Enable reopening resolved chats
    
#     async def find_or_create_customer(
#         self,
#         organization_id: str,
#         channel: str,
#         contact: str,
#         customer_name: Optional[str] = None,
#         metadata: Optional[Dict[str, Any]] = None
#     ) -> Dict[str, Any]:
#         """
#         Find existing customer or create new one.
#         [FIX] STRICT SEPARATION & NORMALIZATION
#         """
#         try:
#             if not contact or str(contact).strip() == "" or str(contact).lower() == "none":
#                 raise ValueError(f"Cannot create customer with empty contact for {channel}")

#             clean_contact = contact

#             # 1. WHATSAPP
#             if channel == "whatsapp":
#                 import re
#                 # 🛡️ THE FIX: Strip EVERYTHING except numbers (kills spaces, dashes, @lid, @c.us)
#                 clean_contact = re.sub(r'[^\d]', '', str(contact))

#                 # A. Primary Check: Robust OR Query (Handles 62... vs 0... automatically)
#                 no_prefix = clean_contact[2:] if clean_contact.startswith('62') else clean_contact
#                 or_query = f"phone.eq.{clean_contact},phone.eq.0{no_prefix},phone.eq.62{no_prefix}"
                
#                 query = self.supabase.table("customers").select("*") \
#                     .eq("organization_id", organization_id) \
#                     .or_(or_query)
                
#                 response = query.execute()
#                 if response.data:
#                     return self._update_customer_name_if_needed(response.data[0], customer_name)

#                 # B. Secondary Check: LID Lookup in Metadata
#                 # Now that clean_contact is purely digits, the length check actually works
#                 if len(clean_contact) >= 14:
#                     lid_query = self.supabase.table("customers").select("*") \
#                         .eq("organization_id", organization_id) \
#                         .eq("metadata->>whatsapp_lid", clean_contact) \
#                         .execute()
                    
#                     if lid_query.data:
#                         real_customer = lid_query.data[0]
#                         return self._update_customer_name_if_needed(real_customer, customer_name)

#             # 2. TELEGRAM 
#             elif channel == "telegram":
#                 is_group_context = (metadata or {}).get("is_group", False)
#                 query = self.supabase.table("customers").select("*") \
#                     .eq("organization_id", organization_id) \
#                     .eq("metadata->>telegram_id", contact) \
#                     .contains("metadata", {"is_group": is_group_context})
                
#                 response = query.execute()
#                 if response.data:
#                     return self._update_customer_name_if_needed(response.data[0], customer_name)

#             # 3. OTHERS
#             elif channel == "email":
#                 query = self.supabase.table("customers").select("*").eq("organization_id", organization_id).eq("email", contact)
#                 response = query.execute()
#                 if response.data:
#                     return self._update_customer_name_if_needed(response.data[0], customer_name)
#             elif channel == "web":
#                 query = self.supabase.table("customers").select("*").eq("organization_id", organization_id).eq("metadata->>session_id", contact)
#                 response = query.execute()
#                 if response.data:
#                     return self._update_customer_name_if_needed(response.data[0], customer_name)
#             else:
#                 raise ValueError(f"Unsupported channel: {channel}")
            
#             # --- CREATE NEW CUSTOMER ---
#             final_name = customer_name or self._extract_name_from_contact(contact, channel)

#             customer_data = {
#                 "organization_id": organization_id,
#                 "name": final_name,
#                 # 🛡️ THE FIX: Save the purely sanitized number into the database
#                 "phone": clean_contact if channel == "whatsapp" else (metadata or {}).get("phone"),
#                 "email": (metadata or {}).get("email"),
#                 "metadata": {
#                     **(metadata or {}),
#                     "first_contact_at": datetime.utcnow().isoformat(),
#                     "channels_used": [channel]
#                 }
#             }

#             if channel == "telegram":
#                 customer_data["metadata"]["telegram_id"] = contact
#                 customer_data["metadata"]["is_group"] = (metadata or {}).get("is_group", False)
#             elif channel == "whatsapp":
#                 # Ensure the LID is properly stored if it was originally an LID
#                 if "@lid" in str(contact):
#                     customer_data["metadata"]["whatsapp_lid"] = clean_contact

#             res = self.supabase.table("customers").insert(customer_data).execute()
#             if not res.data: raise Exception("Failed to create customer")
#             return res.data[0]

#         except Exception as e:
#             logger.error(f"❌ Customer lookup/creation failed: {e}")
#             raise
        
#     def _update_customer_name_if_needed(self, customer: Dict[str, Any], new_name: Optional[str]) -> Dict[str, Any]:
#         """
#         Helper to update 'Unknown' names if we get a better name later.
#         """
#         try:
#             current_name = customer.get("name", "")
#             # If we have a new name, and the current one is "Unknown" or empty
#             if new_name and (not current_name or "Unknown" in current_name):
                
#                 # Update DB
#                 self.supabase.table("customers").update({
#                     "name": new_name
#                 }).eq("id", customer["id"]).execute()
                
#                 # Return updated object locally
#                 customer["name"] = new_name
#         except Exception as e:
#             logger.warning(f"⚠️ Failed to update customer name: {e}")
            
#         return customer
    
#     async def find_active_chat(
#         self,
#         customer_id: str,
#         channel: str,
#         organization_id: str,
#         agent_id: str
#     ) -> Optional[Dict[str, Any]]:
#         """
#         Find active chat for customer on specific channel.
#         """
#         try:

#             query = self.supabase.table("chats") \
#                 .select("*") \
#                 .eq("customer_id", customer_id) \
#                 .eq("channel", channel) \
#                 .eq("organization_id", organization_id)\
#                 .eq("sender_agent_id", agent_id)

#             if self.resolved_chat_reopen_enabled:
#                 query = query.in_("status", ["open", "assigned", "resolved"])
#             else:
#                 query = query.in_("status", ["open", "assigned"])

#             query = query.order("last_message_at", desc=True).limit(1)
#             response = query.execute()

#             if response.data:
#                 chat = response.data[0]
#                 return chat

#             return None

#         except Exception as e:
#             logger.error(f"❌ Error in find_active_chat: {e}")
#             raise

#     async def update_customer_metadata(
#         self,
#         customer_id: str,
#         channel: str,
#         organization_id: str,
#         new_metadata: Optional[Dict[str, Any]] = None 
#     ) -> None:
#         """
#         Update customer metadata with contact tracking info.
#         """
#         try:
#             customer_response = self.supabase.table("customers") \
#                 .select("metadata") \
#                 .eq("id", customer_id) \
#                 .execute()

#             if not customer_response.data:
#                 return

#             current_metadata = customer_response.data[0].get("metadata", {}) or {}
#             now_iso = datetime.utcnow().isoformat()

#             updated_metadata = {
#                 **current_metadata,
#                 **(new_metadata or {}),
#                 "last_contact_at": now_iso,
#                 "message_count": current_metadata.get("message_count", 0) + 1,
#                 "preferred_channel": channel
#             }

#             if "first_contact_at" not in current_metadata:
#                 updated_metadata["first_contact_at"] = now_iso
#             if "first_contact_channel" not in current_metadata:
#                 updated_metadata["first_contact_channel"] = channel

#             channels_used = current_metadata.get("channels_used", [])
#             if channel not in channels_used:
#                 channels_used.append(channel)
#             updated_metadata["channels_used"] = channels_used

#             self.supabase.table("customers") \
#                 .update({"metadata": updated_metadata}) \
#                 .eq("id", customer_id) \
#                 .execute()

#         except Exception as e:
#             logger.error(f"❌ Metadata sync error: {e}")

#     async def route_incoming_message(
#         self,
#         agent: Dict[str, Any],
#         channel: str,
#         contact: str,
#         message_content: str,
#         customer_name: Optional[str] = None,
#         message_metadata: Optional[Dict[str, Any]] = None,
#         customer_metadata: Optional[Dict[str, Any]] = None
#     ) -> Dict[str, Any]:
#         """
#         Routes message with Blocking Lock.
#         """
#         organization_id = agent["organization_id"]
        
#         # LOCK KEY: Unique to the Organization + Contact + GroupContext
#         is_group_flag = (message_metadata or {}).get("is_group", False)
#         lock_key = f"router:{organization_id}:{contact}:{is_group_flag}"
        
#         async with acquire_lock(lock_key, expire=20, wait_time=5) as acquired:
#             if not acquired:
#                 logger.warning(f"🔒 Lock Timeout. Rejecting.")
#                 raise Exception("System busy.")

#             return await self._execute_routing_logic(
#                 agent, channel, contact, message_content, 
#                 customer_name, message_metadata, customer_metadata
#             )

#     async def _execute_routing_logic(self, agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata):
#         try:
#             organization_id = agent["organization_id"]
#             agent_id = agent["id"]
#             is_ai_agent = agent.get("user_id") is None 

#             # Metadata Helpers
#             meta = message_metadata or {}
#             is_group_msg = meta.get("is_group", False)
#             group_id_context = contact if is_group_msg else None

            
#             # 1. WHATSAPP LOGIC (Restored from your 'waworks' file)
#             if channel == "whatsapp":
#                 raw_participant = meta.get("real_contact_number") or meta.get("participant")
#                 participant_name = meta.get("sender_display_name") or meta.get("push_name")

#                 # SWAP LOGIC: Group ID -> Participant ID
#                 if is_group_msg and raw_participant:
#                     clean_part = str(raw_participant).replace("@c.us", "").replace("@lid", "").strip()
#                     clean_group = str(contact).replace("@g.us", "").strip()

#                     if clean_part and clean_part != clean_group:
#                         logger.info(f"🔀 [WA SWAP] Group({contact}) -> User({clean_part})")
#                         contact = clean_part
#                         if participant_name: 
#                             customer_name = participant_name
                        
#                         if customer_metadata is None: customer_metadata = {}
#                         customer_metadata["last_seen_in_group"] = group_id_context
#                         if "lid" in str(raw_participant):
#                             customer_metadata["is_lid_user"] = True
#                             customer_metadata["whatsapp_lid"] = clean_part

#             # 2. TELEGRAM LOGIC (Keeps the new fixes)
#             elif channel == "telegram":
#                 raw_participant = meta.get("participant") or meta.get("telegram_sender_id")
                
#                 # SWAP LOGIC: Group ID -> User ID
#                 if is_group_msg and raw_participant and str(contact) != str(raw_participant):
#                     contact = raw_participant 
                    
#                     if customer_metadata is None: customer_metadata = {}
#                     customer_metadata["last_seen_in_group"] = group_id_context
#                     customer_metadata["telegram_user_id"] = raw_participant
#                     customer_metadata["identity_swapped"] = True
            
#             # COMMON EXECUTION
#             # Step 1: Find/Create Customer
#             customer = await self.find_or_create_customer(
#                 organization_id, channel, contact, customer_name, customer_metadata
#             )
#             customer_id = customer["id"]

#             # Step 2: Find Active Chat
#             active_chat = await self.find_active_chat(customer_id, channel, organization_id, agent_id)
            
#             chat_id = None
#             message_id = None
#             is_new_chat = False
#             was_reopened = False
#             handled_by = "unassigned" 
#             status = "open"
#             is_merged_event = False 

#             # [CRITICAL RESTORATION] DUPLICATE CHECK
#             # This block was missing in the broken file. It prevents the double write.
#             wa_msg_id = meta.get("whatsapp_message_id")
#             existing_message = None
            
#             if active_chat and wa_msg_id:
#                 try:
#                     # Check if this message ID already exists in this chat
#                     check_res = self.supabase.table("messages") \
#                         .select("id, content, metadata") \
#                         .eq("chat_id", active_chat["id"]) \
#                         .contains("metadata", {"whatsapp_message_id": wa_msg_id}) \
#                         .execute()
#                     if check_res.data: existing_message = check_res.data[0]
#                 except Exception: pass

#             if active_chat:
#                 chat_id = active_chat["id"]
#                 status = active_chat["status"]
#                 handled_by = active_chat.get("handled_by", "unassigned")
                
#                 # Validation: Assigned but no ID? Recover to AI.
#                 if status == "assigned" and not active_chat.get("assigned_agent_id"):
#                     status = "open"
#                     handled_by = "ai"

#                 if existing_message:
#                     # [THE FIX] MERGE INSTEAD OF INSERT
#                     message_id = existing_message["id"]
#                     is_merged_event = True
                    
#                     current_meta = existing_message.get("metadata") or {}
#                     merged_meta = {**current_meta, **meta}
#                     if group_id_context: merged_meta["target_group_id"] = group_id_context
                    
#                     update_data = {"metadata": merged_meta}
#                     # Only update content if it was previously empty (e.g. image arrived before caption)
#                     if message_content and not existing_message.get("content"):
#                         update_data["content"] = message_content
                        
#                     self.supabase.table("messages").update(update_data).eq("id", message_id).execute()

#                 else:
#                     # INSERT NEW (Only if it doesn't exist)                    
#                     update_data = {"last_message_at": datetime.utcnow().isoformat()}
#                     if status == "resolved":
#                         update_data["status"] = "open"
#                         was_reopened = True
#                     else:
#                         update_data["status"] = status
                        
#                     self.supabase.table("chats").update(update_data).eq("id", chat_id).execute()

#                     final_meta = meta or {}
#                     if group_id_context: final_meta["target_group_id"] = group_id_context

#                     m_res = self.supabase.table("messages").insert({
#                         "chat_id": chat_id, "sender_type": "customer", "sender_id": customer_id,
#                         "content": message_content, "metadata": final_meta
#                     }).execute()
#                     if m_res.data: message_id = m_res.data[0]["id"]
            
#             else:
#                 # NEW CHAT
#                 handled_by = "ai" if is_ai_agent else "human"
                
#                 c_res = self.supabase.table("chats").insert({
#                     "organization_id": organization_id, "customer_id": customer_id, "channel": channel,
#                     "sender_agent_id": agent_id, "status": "open", "handled_by": handled_by,
#                     "unread_count": 1, "last_message_at": datetime.utcnow().isoformat(),
#                     "ai_agent_id": agent_id if is_ai_agent else None,
#                     "human_agent_id": agent_id if not is_ai_agent else None,
#                     "assigned_agent_id": agent_id if not is_ai_agent else None
#                 }).execute()
                
#                 if c_res.data:
#                     chat_id = c_res.data[0]["id"]
#                     is_new_chat = True
                    
#                     final_meta = meta or {}
#                     if group_id_context: final_meta["target_group_id"] = group_id_context

#                     m_res = self.supabase.table("messages").insert({
#                         "chat_id": chat_id, "sender_type": "customer", "sender_id": customer_id,
#                         "content": message_content, "metadata": final_meta
#                     }).execute()
#                     if m_res.data: message_id = m_res.data[0]["id"]

#             await self.update_customer_metadata(customer_id, channel, organization_id, customer_metadata)

#             return {
#                 "success": True, "chat_id": chat_id, "message_id": message_id, "customer_id": customer_id,
#                 "is_new_chat": is_new_chat, "was_reopened": was_reopened, "handled_by": handled_by,
#                 "status": status, "channel": channel, "agent_id": agent_id,
#                 "is_merged_event": is_merged_event # <--- Needed by Webhook to prevent double broadcast
#             }

#         except Exception as e:
#             logger.error(f"❌ Router Error: {e}", exc_info=True)
#             raise
            
#     def _extract_name_from_contact(self, contact: str, channel: str) -> str:
#         if channel == "email":
#             return contact.split("@")[0].replace(".", " ").title()
#         elif channel == "whatsapp":
#             return f"WhatsApp {contact}"
#         elif channel == "telegram":
#             return f"Telegram User {contact}"
#         elif channel == "web":
#             return "Web Visitor"
#         else:
#             return "Customer"


# # Singleton instance
# _message_router_service: Optional[MessageRouterService] = None


# def get_message_router_service(supabase) -> MessageRouterService:
#     global _message_router_service
#     if _message_router_service is None:
#         _message_router_service = MessageRouterService(supabase)
#     return _message_router_service

"""
Message Router Service
Handles message routing from external services (WhatsApp, Telegram, Email) to correct chats.
Based on MESSAGE_ROUTING_CHAT_MATCHING.md documentation.
"""
import logging
import asyncio 
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from app.config import settings
from app.services.redis_service import acquire_lock
from uuid import uuid4

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
        Find existing customer or create new one.
        [FIX] STRICT SEPARATION & NORMALIZATION
        """
        try:
            if not contact or str(contact).strip() == "" or str(contact).lower() == "none":
                raise ValueError(f"Cannot create customer with empty contact for {channel}")

            clean_contact = contact

            # 1. WHATSAPP
            if channel == "whatsapp":
                import re
                # 🛡️ THE FIX: Strip EVERYTHING except numbers (kills spaces, dashes, @lid, @c.us)
                clean_contact = re.sub(r'[^\d]', '', str(contact))

                # A. Primary Check: Robust OR Query (Handles 62... vs 0... automatically)
                no_prefix = clean_contact[2:] if clean_contact.startswith('62') else clean_contact
                or_query = f"phone.eq.{clean_contact},phone.eq.0{no_prefix},phone.eq.62{no_prefix}"
                
                query = self.supabase.table("customers").select("*") \
                    .eq("organization_id", organization_id) \
                    .or_(or_query)
                
                response = query.execute()
                if response.data:
                    return self._update_customer_name_if_needed(response.data[0], customer_name)

                # B. Secondary Check: LID Lookup in Metadata
                # Now that clean_contact is purely digits, the length check actually works
                if len(clean_contact) >= 14:
                    lid_query = self.supabase.table("customers").select("*") \
                        .eq("organization_id", organization_id) \
                        .eq("metadata->>whatsapp_lid", clean_contact) \
                        .execute()
                    
                    if lid_query.data:
                        real_customer = lid_query.data[0]
                        return self._update_customer_name_if_needed(real_customer, customer_name)

            # 2. TELEGRAM 
            elif channel == "telegram":
                is_group_context = (metadata or {}).get("is_group", False)
                query = self.supabase.table("customers").select("*") \
                    .eq("organization_id", organization_id) \
                    .eq("metadata->>telegram_id", contact) \
                    .contains("metadata", {"is_group": is_group_context})
                
                response = query.execute()
                if response.data:
                    return self._update_customer_name_if_needed(response.data[0], customer_name)

            # 3. OTHERS
            elif channel == "email":
                query = self.supabase.table("customers").select("*").eq("organization_id", organization_id).eq("email", contact)
                response = query.execute()
                if response.data:
                    return self._update_customer_name_if_needed(response.data[0], customer_name)
            elif channel == "web":
                query = self.supabase.table("customers").select("*").eq("organization_id", organization_id).eq("metadata->>session_id", contact)
                response = query.execute()
                if response.data:
                    return self._update_customer_name_if_needed(response.data[0], customer_name)
            else:
                raise ValueError(f"Unsupported channel: {channel}")
            
            # --- CREATE NEW CUSTOMER ---
            final_name = customer_name or self._extract_name_from_contact(contact, channel)

            customer_data = {
                "organization_id": organization_id,
                "name": final_name,
                # 🛡️ THE FIX: Save the purely sanitized number into the database
                "phone": clean_contact if channel == "whatsapp" else (metadata or {}).get("phone"),
                "email": (metadata or {}).get("email"),
                "metadata": {
                    **(metadata or {}),
                    "first_contact_at": datetime.utcnow().isoformat(),
                    "channels_used": [channel]
                }
            }

            if channel == "telegram":
                customer_data["metadata"]["telegram_id"] = contact
                customer_data["metadata"]["is_group"] = (metadata or {}).get("is_group", False)
            elif channel == "whatsapp":
                # Ensure the LID is properly stored if it was originally an LID
                if "@lid" in str(contact):
                    customer_data["metadata"]["whatsapp_lid"] = clean_contact

            res = self.supabase.table("customers").insert(customer_data).execute()
            if not res.data: raise Exception("Failed to create customer")
            return res.data[0]

        except Exception as e:
            logger.error(f"❌ Customer lookup/creation failed: {e}")
            raise
        
    def _update_customer_name_if_needed(self, customer: Dict[str, Any], new_name: Optional[str]) -> Dict[str, Any]:
        """
        Helper to update 'Unknown' names if we get a better name later.
        """
        try:
            current_name = customer.get("name", "")
            # If we have a new name, and the current one is "Unknown" or empty
            if new_name and (not current_name or "Unknown" in current_name):
                
                # Update DB
                self.supabase.table("customers").update({
                    "name": new_name
                }).eq("id", customer["id"]).execute()
                
                # Return updated object locally
                customer["name"] = new_name
        except Exception as e:
            logger.warning(f"⚠️ Failed to update customer name: {e}")
            
        return customer
    
    async def find_active_chat(
        self,
        customer_id: str,
        channel: str,
        organization_id: str,
        agent_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find active chat for customer on specific channel.
        """
        try:

            query = self.supabase.table("chats") \
                .select("*") \
                .eq("customer_id", customer_id) \
                .eq("channel", channel) \
                .eq("organization_id", organization_id)\
                .eq("sender_agent_id", agent_id)

            if self.resolved_chat_reopen_enabled:
                query = query.in_("status", ["open", "assigned", "resolved"])
            else:
                query = query.in_("status", ["open", "assigned"])

            query = query.order("last_message_at", desc=True).limit(1)
            response = query.execute()

            if response.data:
                chat = response.data[0]
                return chat

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
        """
        try:
            customer_response = self.supabase.table("customers") \
                .select("metadata") \
                .eq("id", customer_id) \
                .execute()

            if not customer_response.data:
                return

            current_metadata = customer_response.data[0].get("metadata", {}) or {}
            now_iso = datetime.utcnow().isoformat()

            updated_metadata = {
                **current_metadata,
                **(new_metadata or {}),
                "last_contact_at": now_iso,
                "message_count": current_metadata.get("message_count", 0) + 1,
                "preferred_channel": channel
            }

            if "first_contact_at" not in current_metadata:
                updated_metadata["first_contact_at"] = now_iso
            if "first_contact_channel" not in current_metadata:
                updated_metadata["first_contact_channel"] = channel

            channels_used = current_metadata.get("channels_used", [])
            if channel not in channels_used:
                channels_used.append(channel)
            updated_metadata["channels_used"] = channels_used

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
        Routes message with Blocking Lock.
        """
        organization_id = agent["organization_id"]
        
        # LOCK KEY: Unique to the Organization + Contact + GroupContext
        is_group_flag = (message_metadata or {}).get("is_group", False)
        lock_key = f"router:{organization_id}:{contact}:{is_group_flag}"
        
        async with acquire_lock(lock_key, expire=20, wait_time=5) as acquired:
            if not acquired:
                logger.warning(f"🔒 Lock Timeout. Rejecting.")
                raise Exception("System busy.")

            return await self._execute_routing_logic(
                agent, channel, contact, message_content, 
                customer_name, message_metadata, customer_metadata
            )

    async def _execute_routing_logic(self, agent, channel, contact, message_content, customer_name, message_metadata, customer_metadata):
        try:
            organization_id = agent["organization_id"]
            agent_id = agent["id"]
            is_ai_agent = agent.get("user_id") is None 

            # Metadata Helpers
            meta = message_metadata or {}
            is_group_msg = meta.get("is_group", False)
            group_id_context = contact if is_group_msg else None

            
            # 1. WHATSAPP LOGIC (Restored from your 'waworks' file)
            if channel == "whatsapp":
                raw_participant = meta.get("real_contact_number") or meta.get("participant")
                participant_name = meta.get("sender_display_name") or meta.get("push_name")

                # SWAP LOGIC: Group ID -> Participant ID
                if is_group_msg and raw_participant:
                    clean_part = str(raw_participant).replace("@c.us", "").replace("@lid", "").strip()
                    clean_group = str(contact).replace("@g.us", "").strip()

                    if clean_part and clean_part != clean_group:
                        logger.info(f"🔀 [WA SWAP] Group({contact}) -> User({clean_part})")
                        contact = clean_part
                        if participant_name: 
                            customer_name = participant_name
                        
                        if customer_metadata is None: customer_metadata = {}
                        customer_metadata["last_seen_in_group"] = group_id_context
                        if "lid" in str(raw_participant):
                            customer_metadata["is_lid_user"] = True
                            customer_metadata["whatsapp_lid"] = clean_part

            # 2. TELEGRAM LOGIC (Keeps the new fixes)
            elif channel == "telegram":
                raw_participant = meta.get("participant") or meta.get("telegram_sender_id")
                
                # SWAP LOGIC: Group ID -> User ID
                if is_group_msg and raw_participant and str(contact) != str(raw_participant):
                    contact = raw_participant 
                    
                    if customer_metadata is None: customer_metadata = {}
                    customer_metadata["last_seen_in_group"] = group_id_context
                    customer_metadata["telegram_user_id"] = raw_participant
                    customer_metadata["identity_swapped"] = True
            
            # COMMON EXECUTION
            # Step 1: Find/Create Customer
            customer = await self.find_or_create_customer(
                organization_id, channel, contact, customer_name, customer_metadata
            )
            customer_id = customer["id"]

            # Step 2: Find Active Chat
            active_chat = await self.find_active_chat(customer_id, channel, organization_id, agent_id)
            
            chat_id = None
            message_id = None
            is_new_chat = False
            was_reopened = False
            handled_by = "unassigned" 
            status = "open"
            is_merged_event = False 

            # [CRITICAL RESTORATION] DUPLICATE CHECK
            # This block was missing in the broken file. It prevents the double write.
            wa_msg_id = meta.get("whatsapp_message_id")
            existing_message = None
            
            if active_chat and wa_msg_id:
                try:
                    # Check if this message ID already exists in this chat
                    check_res = self.supabase.table("messages") \
                        .select("id, content, metadata") \
                        .eq("chat_id", active_chat["id"]) \
                        .contains("metadata", {"whatsapp_message_id": wa_msg_id}) \
                        .execute()
                    if check_res.data: existing_message = check_res.data[0]
                except Exception: pass

            if active_chat:
                chat_id = active_chat["id"]
                status = active_chat["status"]
                handled_by = active_chat.get("handled_by", "unassigned")
                
                # Validation: Assigned but no ID? Recover to AI.
                if status == "assigned" and not active_chat.get("assigned_agent_id"):
                    status = "open"
                    handled_by = "ai"

                if existing_message:
                    # [THE FIX] MERGE INSTEAD OF INSERT
                    message_id = existing_message["id"]
                    is_merged_event = True
                    
                    current_meta = existing_message.get("metadata") or {}
                    merged_meta = {**current_meta, **meta}
                    if group_id_context: merged_meta["target_group_id"] = group_id_context
                    
                    update_data = {"metadata": merged_meta}
                    # Only update content if it was previously empty (e.g. image arrived before caption)
                    if message_content and not existing_message.get("content"):
                        update_data["content"] = message_content
                        
                    self.supabase.table("messages").update(update_data).eq("id", message_id).execute()

                else:
                    # INSERT NEW (Only if it doesn't exist)                    
                    update_data = {"last_message_at": datetime.utcnow().isoformat()}
                    if status == "resolved":
                        update_data["status"] = "open"
                        was_reopened = True
                    else:
                        update_data["status"] = status
                        
                    self.supabase.table("chats").update(update_data).eq("id", chat_id).execute()

                    final_meta = meta or {}
                    if group_id_context: final_meta["target_group_id"] = group_id_context

                    m_res = self.supabase.table("messages").insert({
                        "chat_id": chat_id, "sender_type": "customer", "sender_id": customer_id,
                        "content": message_content, "metadata": final_meta
                    }).execute()
                    if m_res.data: message_id = m_res.data[0]["id"]
            
            else:
                # NEW CHAT
                handled_by = "ai" if is_ai_agent else "human"
                
                c_res = self.supabase.table("chats").insert({
                    "organization_id": organization_id, "customer_id": customer_id, "channel": channel,
                    "sender_agent_id": agent_id, "status": "open", "handled_by": handled_by,
                    "unread_count": 1, "last_message_at": datetime.utcnow().isoformat(),
                    "ai_agent_id": agent_id if is_ai_agent else None,
                    "human_agent_id": agent_id if not is_ai_agent else None,
                    "assigned_agent_id": agent_id if not is_ai_agent else None
                }).execute()
                
                if c_res.data:
                    chat_id = c_res.data[0]["id"]
                    is_new_chat = True
                    
                    final_meta = meta or {}
                    if group_id_context: final_meta["target_group_id"] = group_id_context

                    m_res = self.supabase.table("messages").insert({
                        "chat_id": chat_id, "sender_type": "customer", "sender_id": customer_id,
                        "content": message_content, "metadata": final_meta
                    }).execute()
                    if m_res.data: message_id = m_res.data[0]["id"]

            await self.update_customer_metadata(customer_id, channel, organization_id, customer_metadata)

            return {
                "success": True, "chat_id": chat_id, "message_id": message_id, "customer_id": customer_id,
                "is_new_chat": is_new_chat, "was_reopened": was_reopened, "handled_by": handled_by,
                "status": status, "channel": channel, "agent_id": agent_id,
                "is_merged_event": is_merged_event # <--- Needed by Webhook to prevent double broadcast
            }

        except Exception as e:
            logger.error(f"❌ Router Error: {e}", exc_info=True)
            raise
            
    def _extract_name_from_contact(self, contact: str, channel: str) -> str:
        if channel == "email":
            return contact.split("@")[0].replace(".", " ").title()
        elif channel == "whatsapp":
            return f"WhatsApp {contact}"
        elif channel == "telegram":
            return f"Telegram User {contact}"
        elif channel == "web":
            return "Web Visitor"
        else:
            return "Customer"


def get_message_router_service(supabase) -> MessageRouterService:
    return MessageRouterService(supabase)