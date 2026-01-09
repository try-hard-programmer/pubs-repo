"""
Credit/Ledger Models

Pydantic models for tracking organization credit usage and balance.
"""
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum

class TransactionType(str, Enum):
    DEPOSIT = "deposit"       # Adding credits (Top-up)
    USAGE = "usage"           # Consuming credits (AI, Messages, etc.)
    REFUND = "refund"         # Reverting a usage
    ADJUSTMENT = "adjustment" # Admin manual correction

class CreditTransactionBase(BaseModel):
    """Base schema for credit transactions"""
    amount: float = Field(..., description="Transaction amount (positive for deposit, negative for usage)")
    description: str = Field(..., description="Reason for the transaction")
    transaction_type: TransactionType = Field(default=TransactionType.USAGE, description="Type of transaction")
    metadata: Optional[dict] = Field(default_factory=dict, description="Extra details (e.g., related message_id)")

class CreditTransactionCreate(CreditTransactionBase):
    """Schema for creating a new transaction"""
    organization_id: str = Field(..., description="Organization UUID")

class CreditTransaction(CreditTransactionBase):
    """Schema for transaction response"""
    id: str = Field(..., description="Transaction UUID")
    organization_id: str = Field(..., description="Organization UUID")
    balance_after: Optional[float] = Field(None, description="Snapshot of balance after this transaction")
    created_at: datetime = Field(..., description="Transaction timestamp")

    class Config:
        from_attributes = True

class CreditBalance(BaseModel):
    """Schema for organization balance"""
    organization_id: str
    total_balance: float