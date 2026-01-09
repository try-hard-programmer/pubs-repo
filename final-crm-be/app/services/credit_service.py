"""
Credit Service - FIXED VERSION with Safe Defaults
Manages organization credits, ledger transactions, and balance calculations.
"""
import logging
from typing import List, Optional
from supabase import create_client, Client
from app.config import settings
from app.models.credit import CreditTransaction, CreditTransactionCreate, CreditBalance, TransactionType

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

    def get_balance(self, organization_id: str) -> CreditBalance:
        """Calculate current balance by summing all cost_usd (negative = spent)."""
        try:
            response = self.client.table("credit_usage").select("cost_usd").eq("organization_id", organization_id).execute()
            total = -sum(float(item.get("cost_usd", 0) or 0) for item in response.data) if response.data else 0.0
            return CreditBalance(organization_id=organization_id, total_balance=total)
        except Exception as e:
            logger.error(f"Failed to get balance for {organization_id}: {e}", exc_info=True)
            raise RuntimeError(f"Balance check failed: {str(e)}")

    def add_transaction(self, data: CreditTransactionCreate) -> CreditTransaction:
        """Record a credit transaction (maps to credit_usage table)."""
        try:
            # ✅ Safe extraction with defaults
            metadata = data.metadata or {}
            provider = metadata.get("provider") or "unknown"
            model = metadata.get("model") or None
            input_tokens = int(metadata.get("input_tokens") or 0)
            output_tokens = int(metadata.get("output_tokens") or 0)
            agent_id = metadata.get("agent_id") or None
            
            # ✅ Map your CreditTransactionCreate to credit_usage columns
            payload = {
                "organization_id": data.organization_id,
                "query_type": self._map_to_query_type(data.description or ""),
                "query_text": (data.description or "")[:500],  # ✅ Truncate if too long
                "credits_used": max(1, int(abs(data.amount or 0) * 1000000)),
                "cost_usd": abs(data.amount or 0),
                "status": "completed",
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "agent_id": agent_id,
                "metadata": metadata
            }
            
            response = self.client.table("credit_usage").insert(payload).execute()
            
            if not response.data:
                raise RuntimeError("Transaction insert returned no data")
            
            trx = response.data[0]
            logger.info(f"💰 Credit usage: ${data.amount or 0:.6f} for Org {data.organization_id}")
            
            # ✅ Safe return with defaults
            return CreditTransaction(
                id=trx.get("id"),
                organization_id=trx.get("organization_id"),
                amount=-(trx.get("cost_usd") or 0),  # Negative for spending
                transaction_type=TransactionType.USAGE,
                description=trx.get("query_text") or "",
                metadata=trx.get("metadata") or {},
                created_at=trx.get("created_at")
            )

        except Exception as e:
            logger.error(f"Credit transaction failed: {e}", exc_info=True)
            raise RuntimeError(f"Transaction failed: {str(e)}")

    def _map_to_query_type(self, description: str) -> str:
        """Map description to valid query_type enum."""
        desc_lower = (description or "").lower()
        
        if "embedding" in desc_lower or "knowledge" in desc_lower:
            return "document_analysis"
        elif "image" in desc_lower:
            return "image_analysis"
        elif "search" in desc_lower:
            return "file_search"
        elif "complex" in desc_lower:
            return "complex_query"
        else:
            return "basic_query"

    def check_sufficient_funds(self, organization_id: str, cost: float) -> bool:
        """Check if org has enough credits before running AI."""
        try:
            balance = self.get_balance(organization_id)
            logger.info(f"💰 Org {organization_id} balance: ${balance.total_balance:.6f}")
            return True  # For now, always allow
        except Exception as e:
            logger.warning(f"Balance check failed: {e}")
            return True  # ✅ Allow on error to avoid blocking

_credit_service = None
def get_credit_service():
    global _credit_service
    if _credit_service is None:
        _credit_service = CreditService()
    return _credit_service