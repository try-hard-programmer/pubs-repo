"""
Credit Service

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
            
        # Use service role key to bypass RLS for financial operations (Safe)
        key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY
        self._client = create_client(settings.SUPABASE_URL, key)

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Supabase not configured")
        return self._client

    async def get_balance(self, organization_id: str) -> CreditBalance:
        """
        Calculate current balance by summing all transactions.
        
        Args:
            organization_id: The organization UUID
        """
        try:
            # Efficient Sum in DB (RPC is better, but this works for simple cases)
            # Ensure you have a 'credits' table
            response = self.client.table("credits").select("amount").eq("organization_id", organization_id).execute()
            
            total = sum(item["amount"] for item in response.data) if response.data else 0.0
            
            return CreditBalance(organization_id=organization_id, total_balance=total)
        except Exception as e:
            logger.error(f"Failed to get balance for {organization_id}: {e}")
            raise RuntimeError(f"Balance check failed: {str(e)}")

    async def add_transaction(self, data: CreditTransactionCreate) -> CreditTransaction:
        """
        Record a credit transaction (Debit or Credit).
        """
        try:
            # 1. Insert Transaction
            payload = data.model_dump(mode="json")
            
            # Optional: Calculate balance_after if your DB trigger doesn't do it
            # For strict ledgers, we just insert and sum on read, or update a cache.
            
            response = self.client.table("credits").insert(payload).execute()
            
            if not response.data:
                raise RuntimeError("Transaction insert returned no data")
            
            trx = response.data[0]
            logger.info(f"💰 Credit {data.transaction_type}: {data.amount} for Org {data.organization_id}")
            
            return CreditTransaction(**trx)

        except Exception as e:
            logger.error(f"Credit transaction failed: {e}")
            raise RuntimeError(f"Transaction failed: {str(e)}")

    async def check_sufficient_funds(self, organization_id: str, cost: float) -> bool:
        """
        Helper guard to check if org has enough credits before running AI.
        """
        balance = await self.get_balance(organization_id)
        if balance.total_balance >= cost:
            return True
        logger.warning(f"🚫 Insufficient funds for Org {organization_id}: Has {balance.total_balance}, Needs {cost}")
        return False

# Global Instance
_credit_service = None

def get_credit_service():
    global _credit_service
    if _credit_service is None:
        _credit_service = CreditService()
    return _credit_service