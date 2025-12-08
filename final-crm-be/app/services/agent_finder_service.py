"""
Agent Finder Service
Find agents by integration mapping (WhatsApp number, Telegram bot, Email address)
"""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class AgentFinderService:
    """Service for finding agents by channel integration"""

    def __init__(self, supabase):
        """
        Initialize Agent Finder Service

        Args:
            supabase: Supabase client instance
        """
        self.supabase = supabase

    async def find_agent_by_whatsapp_number(
        self,
        phone_number: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find agent by WhatsApp business phone number.

        Looks up agent_integrations table for enabled WhatsApp integration
        with matching phoneNumber in config.

        Args:
            phone_number: WhatsApp business number (e.g., "+6281234567890")

        Returns:
            Agent data dict with organization_id, or None if not found

        Example:
            {
                "id": "agent-uuid",
                "organization_id": "org-uuid",
                "name": "Agent Name",
                "user_id": null,  # null = AI agent
                "status": "active",
                "integration_config": {"phoneNumber": "+628..."}
            }
        """
        try:
            logger.info(f"🔍 Finding agent for WhatsApp number: {phone_number}")

            # Query agent_integrations with join to agents table
            response = self.supabase.table("agent_integrations") \
                .select("""
                    id,
                    agent_id,
                    config,
                    status,
                    agents!inner (
                        id,
                        organization_id,
                        user_id,
                        name,
                        email,
                        status
                    )
                """) \
                .eq("channel", "whatsapp") \
                .eq("enabled", True) \
                .eq("config->>phoneNumber", phone_number) \
                .execute()

            print("Phone Number : "+phone_number)

            if not response.data:
                logger.warning(f"❌ No agent integration found for WhatsApp: {phone_number}")
                return None

            # Get first matching integration
            integration = response.data[0]
            agent_data = integration["agents"]

            print("Agent Data : "+str(agent_data))

            # Check if agent is active or busy (busy agents can still receive messages)
            allowed_statuses = ["active", "busy"]
            if agent_data["status"] not in allowed_statuses:
                logger.warning(
                    f"⚠️  Agent {agent_data['id']} found but status is '{agent_data['status']}' (not in {allowed_statuses})"
                )
                return None

            # Check if integration is connected
            if integration["status"] != "connected":
                logger.warning(
                    f"⚠️  Integration found but status is '{integration['status']}'"
                )
                return None

            # Build agent result
            result = {
                **agent_data,
                "integration_id": integration["id"],
                "integration_config": integration["config"],
                "integration_status": integration["status"]
            }

            logger.info(
                f"✅ Found agent: {agent_data['name']} (org={agent_data['organization_id']}, "
                f"is_ai={agent_data['user_id'] is None})"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Error finding agent by WhatsApp number: {e}")
            raise

    async def find_agent_by_telegram_bot(
        self,
        bot_token: Optional[str] = None,
        bot_username: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Find agent by Telegram bot token or username.

        Args:
            bot_token: Telegram bot API token (optional)
            bot_username: Telegram bot username (optional)

        Returns:
            Agent data dict, or None if not found
        """
        try:
            if not bot_token and not bot_username:
                raise ValueError("Either bot_token or bot_username must be provided")

            logger.info(
                f"🔍 Finding agent for Telegram bot: "
                f"token={bot_token[:20] if bot_token else 'N/A'}..., "
                f"username={bot_username}"
            )

            # Build query
            query = self.supabase.table("agent_integrations") \
                .select("""
                    id,
                    agent_id,
                    config,
                    status,
                    agents!inner (
                        id,
                        organization_id,
                        user_id,
                        name,
                        email,
                        status
                    )
                """) \
                .eq("channel", "telegram") \
                .eq("enabled", True)

            # Add filter by token or username
            if bot_token:
                query = query.eq("config->>botToken", bot_token)
            elif bot_username:
                query = query.eq("config->>botUsername", bot_username)

            response = query.execute()

            if not response.data:
                logger.warning(
                    f"❌ No agent integration found for Telegram bot: "
                    f"{bot_username or 'token=' + bot_token[:20]}"
                )
                return None

            # Get first matching integration
            integration = response.data[0]
            agent_data = integration["agents"]

            # Validate agent status (allow active and busy)
            allowed_statuses = ["active", "busy"]
            if agent_data["status"] not in allowed_statuses:
                logger.warning(f"⚠️  Agent {agent_data['id']} status is '{agent_data['status']}' (not in {allowed_statuses})")
                return None

            if integration["status"] != "connected":
                logger.warning(f"⚠️  Integration status is '{integration['status']}'")
                return None

            # Build result
            result = {
                **agent_data,
                "integration_id": integration["id"],
                "integration_config": integration["config"],
                "integration_status": integration["status"]
            }

            logger.info(
                f"✅ Found agent: {agent_data['name']} (org={agent_data['organization_id']})"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Error finding agent by Telegram bot: {e}")
            raise

    async def find_agent_by_email(
        self,
        email: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find agent by email address.

        Args:
            email: Email address configured in integration

        Returns:
            Agent data dict, or None if not found
        """
        try:
            logger.info(f"🔍 Finding agent for Email: {email}")

            response = self.supabase.table("agent_integrations") \
                .select("""
                    id,
                    agent_id,
                    config,
                    status,
                    agents!inner (
                        id,
                        organization_id,
                        user_id,
                        name,
                        email,
                        status
                    )
                """) \
                .eq("channel", "email") \
                .eq("enabled", True) \
                .eq("config->>email", email) \
                .execute()

            if not response.data:
                logger.warning(f"❌ No agent integration found for Email: {email}")
                return None

            # Get first matching integration
            integration = response.data[0]
            agent_data = integration["agents"]

            # Validate agent status (allow active and busy)
            allowed_statuses = ["active", "busy"]
            if agent_data["status"] not in allowed_statuses:
                logger.warning(f"⚠️  Agent {agent_data['id']} status is '{agent_data['status']}' (not in {allowed_statuses})")
                return None

            if integration["status"] != "connected":
                logger.warning(f"⚠️  Integration status is '{integration['status']}'")
                return None

            # Build result
            result = {
                **agent_data,
                "integration_id": integration["id"],
                "integration_config": integration["config"],
                "integration_status": integration["status"]
            }

            logger.info(
                f"✅ Found agent: {agent_data['name']} (org={agent_data['organization_id']})"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Error finding agent by email: {e}")
            raise

    async def find_agent_by_integration(
        self,
        channel: str,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Find agent by integration - wrapper method.

        Args:
            channel: Communication channel (whatsapp, telegram, email)
            **kwargs: Channel-specific parameters:
                - WhatsApp: phone_number
                - Telegram: bot_token or bot_username
                - Email: email

        Returns:
            Agent data dict, or None if not found

        Example:
            # WhatsApp
            agent = await find_agent_by_integration(
                channel="whatsapp",
                phone_number="+6281234567890"
            )

            # Telegram
            agent = await find_agent_by_integration(
                channel="telegram",
                bot_token="123456:ABC..."
            )

            # Email
            agent = await find_agent_by_integration(
                channel="email",
                email="support@example.com"
            )
        """
        if channel == "whatsapp":
            phone_number = kwargs.get("phone_number")
            if not phone_number:
                raise ValueError("phone_number required for WhatsApp")
            return await self.find_agent_by_whatsapp_number(phone_number)

        elif channel == "telegram":
            bot_token = kwargs.get("bot_token")
            bot_username = kwargs.get("bot_username")
            return await self.find_agent_by_telegram_bot(bot_token, bot_username)

        elif channel == "email":
            email = kwargs.get("email")
            if not email:
                raise ValueError("email required for Email")
            return await self.find_agent_by_email(email)

        else:
            raise ValueError(f"Unsupported channel: {channel}")


# Singleton instance
_agent_finder_service: Optional[AgentFinderService] = None


def get_agent_finder_service(supabase) -> AgentFinderService:
    """
    Get or create AgentFinderService instance.

    Args:
        supabase: Supabase client instance

    Returns:
        AgentFinderService instance
    """
    global _agent_finder_service
    if _agent_finder_service is None:
        _agent_finder_service = AgentFinderService(supabase)
    return _agent_finder_service
