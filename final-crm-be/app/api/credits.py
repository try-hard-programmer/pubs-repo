"""
Credits & Billing API Endpoints
Provides routes for fetching subscription status, transaction history, and usage stats.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import logging
from typing import List, Optional

# Auth & User Models
from app.auth.dependencies import get_current_user
from app.models.user import User

# Models & Service
from app.models.credit import CreditUsage
from app.models.subscription import Subscription
from app.services.credit_service import get_credit_service
from app.services.subscription_service import get_subscription_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing-and-credits"])

# 1. Define the Request Body schema locally
class BillingRequest(BaseModel):
    organization_id: Optional[str] = Field(None, description="Organization ID passed from frontend body")

def get_org_id(user: User, body_org_id: Optional[str] = None) -> str:
    """Helper to extract organization_id from token metadata or fallback to Request Body."""
    # 1. Try to get it from the JWT token
    org_id = user.user_metadata.get("organization_id") or user.app_metadata.get("organization_id")
    
    # 2. Fallback to explicit Body if token doesn't have it
    if not org_id and body_org_id:
        org_id = body_org_id
        
    # 3. Hard fail if neither exists
    if not org_id:
        logger.error(f"User {user.email} attempted billing access without an organization_id.")
        raise HTTPException(
            status_code=400, 
            detail="Organization ID missing. Frontend must pass 'organization_id' in the JSON body."
        )
    return org_id


# 2. Changed from GET to POST to accept the JSON body
@router.post("/subscription", response_model=Subscription)
async def get_active_subscription(
    request: BillingRequest,
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        
        # USE THE SUBSCRIPTION SERVICE HERE
        sub_service = get_subscription_service()
        subscription_data = await sub_service.get_subscription(org_id)
        
        if not subscription_data:
            raise HTTPException(status_code=500, detail="Failed to retrieve or provision subscription.")
            
        return subscription_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching subscription for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 3. Changed from GET to POST to accept the JSON body
@router.post("/transactions", response_model=List[CreditUsage])
async def list_transactions(
    request: BillingRequest,
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        service = get_credit_service()
        
        # Change 'get_transactions' to 'get_usage_history'
        transactions = await service.get_usage_history(
            organization_id=org_id,
            limit=limit,
            offset=offset
        )
        return transactions

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transactions for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch transaction history.")


# 4. Changed from GET to POST to accept the JSON body
@router.post("/stats")
async def get_billing_stats(
    request: BillingRequest,
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        service = get_credit_service()
        
        stats = await service.get_usage_stats(org_id)
        return stats

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching billing stats for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch billing statistics.")