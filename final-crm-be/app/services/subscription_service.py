import logging
from typing import Optional
from datetime import datetime
from supabase import create_client, Client

from app.config import settings
from app.models.subscription import Subscription, SubscriptionCreate, SubscriptionUpdate

logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(self):
        if not settings.is_supabase_configured:
            self._client = None
            return
            
        key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY
        self._client = create_client(settings.SUPABASE_URL, key)

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Supabase not configured")
        return self._client

    async def get_subscription(self, organization_id: str) -> Optional[Subscription]:
        """Fetch the active subscription for an organization."""
        try:
            response = self.client.table("subscriptions")\
                .select("*")\
                .eq("organization_id", organization_id)\
                .execute()
            
            if not response.data:
                return None
                
            return Subscription(**response.data[0])
            
        except Exception as e:
            logger.error(f"❌ Failed to get subscription for org {organization_id}: {e}")
            return None

    async def upsert_subscription(self, data: SubscriptionCreate) -> Subscription:
        """Create or update a subscription (handles the unique constraint)."""
        try:
            payload = data.model_dump(exclude_unset=True)
            # Format datetimes for PostgreSQL
            if "start_date" in payload:
                payload["start_date"] = payload["start_date"].isoformat()
            if "end_date" in payload:
                payload["end_date"] = payload["end_date"].isoformat()
                
            payload["updated_at"] = datetime.utcnow().isoformat()

            response = self.client.table("subscriptions").upsert(
                payload, 
                on_conflict="organization_id"
            ).execute()

            if not response.data:
                raise RuntimeError("No data returned from upsert")
                
            logger.info(f"✅ Subscription upserted for Org {data.organization_id}")
            return Subscription(**response.data[0])

        except Exception as e:
            logger.error(f"❌ Failed to upsert subscription: {e}", exc_info=True)
            raise RuntimeError(f"Subscription upsert failed: {str(e)}")

    async def can_consume_credits(self, organization_id: str, requested_credits: int) -> bool:
        """
        Check if the org has enough credits remaining in their subscription.
        Prevents the database 'chk_used_credits_not_exceed' constraint from throwing 500 errors.
        """
        sub = await self.get_subscription(organization_id)
        if not sub:
            logger.warning(f"⚠️ No active subscription for {organization_id}.")
            return False
            
        if sub.status != "active":
            logger.warning(f"⚠️ Subscription is not active for {organization_id}.")
            return False

        if sub.used_credits + requested_credits > sub.total_credits:
            logger.warning(
                f"⚠️ Credit limit reached for {organization_id}. "
                f"Used: {sub.used_credits}, Total: {sub.total_credits}, Requested: {requested_credits}"
            )
            return False
            
        return True

    async def increment_usage(self, organization_id: str, credits_used: int) -> Optional[Subscription]:
        """Increment the used_credits counter."""
        try:
            # 1. Fetch current usage (or rely on an RPC function if high concurrency is expected)
            sub = await self.get_subscription(organization_id)
            if not sub:
                raise ValueError("Subscription not found")

            new_used = sub.used_credits + credits_used
            
            # Pre-validate to avoid the SQL constraint crash
            if new_used > sub.total_credits:
                raise ValueError("Cannot exceed total_credits limit")

            # 2. Update the record
            response = self.client.table("subscriptions")\
                .update({
                    "used_credits": new_used,
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("organization_id", organization_id)\
                .execute()

            if not response.data:
                raise RuntimeError("Failed to update usage")

            return Subscription(**response.data[0])

        except Exception as e:
            logger.error(f"❌ Failed to increment usage for org {organization_id}: {e}")
            raise


# ==========================================
# SINGLETON INSTANCE
# ==========================================
_subscription_service = None

def get_subscription_service():
    global _subscription_service
    if _subscription_service is None:
        _subscription_service = SubscriptionService()
    return _subscription_service