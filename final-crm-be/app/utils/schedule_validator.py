"""
Schedule Validator Utility
Validate if current time is within agent's working hours based on schedule_config
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


async def get_agent_schedule_config(
    agent_id: str,
    supabase
) -> Optional[Dict[str, Any]]:
    """
    Get agent's schedule configuration from agent_settings table.

    Args:
        agent_id: Agent UUID
        supabase: Supabase client instance

    Returns:
        schedule_config dict or None if not found or not enabled

    Example:
        {
            "enabled": true,
            "timezone": "Asia/Jakarta",
            "workingHours": [
                {"day": "monday", "enabled": true, "start": "09:00", "end": "17:00"},
                {"day": "sunday", "enabled": false, "start": "00:00", "end": "00:00"}
            ]
        }
    """
    try:
        logger.debug(f"🔍 Fetching schedule config for agent: {agent_id}")

        # Query agent_settings for this agent
        response = supabase.table("agent_settings") \
            .select("schedule_config") \
            .eq("agent_id", agent_id) \
            .execute()

        if not response.data:
            logger.warning(f"⚠️  No agent_settings found for agent {agent_id}")
            return None

        schedule_config = response.data[0].get("schedule_config")

        if not schedule_config:
            logger.debug(f"ℹ️  No schedule_config found for agent {agent_id}")
            return None

        # Check if scheduling is enabled
        if not schedule_config.get("enabled", False):
            logger.debug(f"ℹ️  Schedule config is disabled for agent {agent_id}")
            return None

        logger.debug(f"✅ Schedule config found for agent {agent_id}: timezone={schedule_config.get('timezone')}")
        return schedule_config

    except Exception as e:
        logger.error(f"❌ Error fetching agent schedule config: {e}")
        return None


def is_within_schedule(
    schedule_config: Optional[Dict[str, Any]],
    current_time: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    """
    Check if current time is within agent's working hours.

    Args:
        schedule_config: Agent's schedule configuration dict
        current_time: Current datetime (optional, defaults to now UTC)

    Returns:
        Tuple of (is_within_schedule: bool, reason: Optional[str])
        - (True, None) if within schedule
        - (False, reason_string) if outside schedule

    Examples:
        >>> is_within_schedule(None)
        (True, None)  # No schedule = always available

        >>> config = {
        ...     "enabled": True,
        ...     "timezone": "Asia/Jakarta",
        ...     "workingHours": [
        ...         {"day": "monday", "enabled": True, "start": "09:00", "end": "17:00"}
        ...     ]
        ... }
        >>> is_within_schedule(config)  # If Monday 10:00 in Jakarta
        (True, None)

        >>> is_within_schedule(config)  # If Sunday in Jakarta
        (False, "Outside working hours: sunday is not a working day")
    """
    try:
        # If no schedule config or not enabled, agent is always available
        if not schedule_config or not schedule_config.get("enabled", False):
            logger.debug("✅ No schedule restrictions - agent is available")
            return (True, None)

        # Get timezone from config (default to UTC if not specified)
        timezone_str = schedule_config.get("timezone", "UTC")
        try:
            tz = ZoneInfo(timezone_str)
        except Exception as e:
            logger.error(f"❌ Invalid timezone '{timezone_str}': {e}, using UTC")
            tz = ZoneInfo("UTC")

        # Get current time in agent's timezone
        if current_time is None:
            current_time = datetime.now(ZoneInfo("UTC"))

        # Convert to agent's timezone
        local_time = current_time.astimezone(tz)

        # Get day of week (Python: 0=Monday, 6=Sunday)
        weekday_index = local_time.weekday()
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        current_day = day_names[weekday_index]

        # Get current time string (HH:MM format)
        current_time_str = local_time.strftime("%H:%M")

        logger.debug(
            f"📅 Checking schedule: day={current_day}, time={current_time_str}, "
            f"timezone={timezone_str}"
        )

        # Get working hours configuration
        working_hours_list = schedule_config.get("workingHours", [])

        if not working_hours_list:
            logger.warning("⚠️  No working hours configured - treating as unavailable")
            return (False, "No working hours configured")

        # Find configuration for current day
        day_config = None
        for wh in working_hours_list:
            if wh.get("day", "").lower() == current_day.lower():
                day_config = wh
                break

        if not day_config:
            reason = f"Outside working hours: {current_day} is not configured"
            logger.info(f"⏰ {reason}")
            return (False, reason)

        # Check if day is enabled
        if not day_config.get("enabled", False):
            reason = f"Outside working hours: {current_day} is not a working day"
            logger.info(f"⏰ {reason}")
            return (False, reason)

        # Get start and end times
        start_time_str = day_config.get("start", "00:00")
        end_time_str = day_config.get("end", "23:59")

        logger.debug(
            f"📋 Working hours for {current_day}: {start_time_str} - {end_time_str}"
        )

        # Compare times (simple string comparison works for HH:MM format)
        if current_time_str < start_time_str or current_time_str > end_time_str:
            reason = (
                f"Outside working hours: {current_day} {current_time_str} "
                f"(working hours: {start_time_str}-{end_time_str})"
            )
            logger.info(f"⏰ {reason}")
            return (False, reason)

        # Within schedule
        logger.debug(
            f"✅ Within schedule: {current_day} {current_time_str} "
            f"is between {start_time_str}-{end_time_str}"
        )
        return (True, None)

    except Exception as e:
        logger.error(f"❌ Error checking schedule: {e}")
        # On error, default to available (fail-open)
        return (True, None)
