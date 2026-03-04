from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum

class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

class SubscriptionBase(BaseModel):
    """Base schema for subscription data"""
    plan_name: str = Field(..., description="Name of the pricing plan")
    status: SubscriptionStatus = Field(default=SubscriptionStatus.ACTIVE)
    total_credits: int = Field(default=0, description="Maximum credits allowed")
    used_credits: int = Field(default=0, description="Credits consumed so far")
    total_cost: float = Field(default=0.0, description="Cost of the subscription")
    start_date: datetime
    end_date: datetime

class SubscriptionCreate(SubscriptionBase):
    """Schema for creating a new subscription"""
    organization_id: str = Field(..., description="Organization UUID")

class SubscriptionUpdate(BaseModel):
    """Schema for updating an existing subscription"""
    plan_name: Optional[str] = None
    status: Optional[SubscriptionStatus] = None
    total_credits: Optional[int] = None
    used_credits: Optional[int] = None
    total_cost: Optional[float] = None
    end_date: Optional[datetime] = None

class Subscription(SubscriptionBase):
    """Schema for returning subscription data"""
    id: int
    organization_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True