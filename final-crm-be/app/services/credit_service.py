"""
Credit Service - ASYNC VERSION with Safe Defaults
Records AI and file processing usage strictly into the credit_usage ledger.
"""
import logging
from typing import List, Optional
from supabase import create_client, Client
from app.config import settings

# Import the NEW models we just created
from app.models.credit import CreditUsageCreate, CreditUsage

logger = logging.getLogger(__name__)

class CreditService:
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

    async def log_usage(self, usage_data: CreditUsageCreate) -> CreditUsage:
        """
        Records an AI action or file upload strictly into the credit_usage ledger.
        """
        try:
            # Map the exact fields from the Pydantic model to the database columns
            payload = {
                "organization_id": usage_data.organization_id,
                "query_type": usage_data.query_type.value,
                "query_text": usage_data.query_text,
                "credits_used": usage_data.credits_used,
                "status": usage_data.status.value,
                "input_tokens": usage_data.input_tokens,
                "output_tokens": usage_data.output_tokens,
                "cost": usage_data.cost,
                "metadata": usage_data.metadata or {}
            }
            
            logger.info(f"💳 Recording usage: {usage_data.credits_used} credits for org {usage_data.organization_id}")
            
            response = self.client.table("credit_usage").insert(payload).execute()
            
            if not response.data:
                raise RuntimeError("Usage insert returned no data")
            
            logger.info(f"✅ Usage recorded: {usage_data.credits_used} credits for Org {usage_data.organization_id}")
            
            return CreditUsage(**response.data[0])

        except Exception as e:
            logger.error(f"❌ Usage logging failed: {e}", exc_info=True)
            raise RuntimeError(f"Logging failed: {str(e)}")

    async def get_usage_history(
        self, 
        organization_id: str, 
        limit: int = 20,
        offset: int = 0
    ) -> List[CreditUsage]:
        """Get consumption history for an organization."""
        try:
            response = self.client.table("credit_usage")\
                .select("*")\
                .eq("organization_id", organization_id)\
                .order("created_at", desc=True)\
                .limit(limit)\
                .offset(offset)\
                .execute()
            
            if not response.data:
                return []
            
            transactions = [CreditUsage(**row) for row in response.data]
            logger.info(f"📜 Retrieved {len(transactions)} usage logs for org {organization_id}")
            return transactions
            
        except Exception as e:
            logger.error(f"❌ Failed to get usage history: {e}", exc_info=True)
            return []

    async def get_usage_stats(self, organization_id: str) -> dict:
        """Get usage statistics for an organization."""
        try:
            response = self.client.table("credit_usage")\
                .select("cost, query_type, created_at, metadata")\
                .eq("organization_id", organization_id)\
                .execute()
            
            if not response.data:
                return {"total_spent": 0.0, "total_transactions": 0, "by_type": {}, "by_provider": {}}
            
            total_spent = sum(float(item.get("cost", 0) or 0) for item in response.data)
            
            # Group by our specific Enums
            by_type = {}
            for item in response.data:
                qtype = item.get("query_type", "unknown")
                cost = float(item.get("cost", 0) or 0)
                by_type[qtype] = by_type.get(qtype, 0.0) + cost
            
            # Group by provider from metadata
            by_provider = {}
            for item in response.data:
                metadata = item.get("metadata") or {}
                provider = metadata.get("provider", "unknown")
                cost = float(item.get("cost", 0) or 0)
                by_provider[provider] = by_provider.get(provider, 0.0) + cost
            
            return {
                "total_spent": total_spent,
                "total_transactions": len(response.data),
                "by_type": by_type,
                "by_provider": by_provider
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to get usage stats: {e}", exc_info=True)
            return {"total_spent": 0.0, "total_transactions": 0, "by_type": {}, "by_provider": {}}

# ==========================================
# SINGLETON INSTANCE
# ==========================================
_credit_service = None

def get_credit_service():
    global _credit_service
    if _credit_service is None:
        _credit_service = CreditService()
    return _credit_service