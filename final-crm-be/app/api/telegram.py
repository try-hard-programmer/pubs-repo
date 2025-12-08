"""
Telegram API Router
Strict endpoints: /send-otp and /verified-otp
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.services.telegram_service import get_telegram_service
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/telegram", tags=["telegram"])

# --- Strict Models ---
class SendOtpRequest(BaseModel):
    agent_id: str
    phone_number: str

class VerifyOtpRequest(BaseModel):
    agent_id: str
    phone_number: str
    otp_code: str  # Frontend must send this exact key

class SessionControlRequest(BaseModel):
    agent_id: str

# --- Endpoint 1: Send OTP ---
@router.post("/auth/send-otp")
async def send_otp_endpoint(
    payload: SendOtpRequest, 
    user=Depends(get_current_user)
):
    try:
        service = get_telegram_service()
        # Call service with exact arguments
        return await service.send_otp(
            agent_id=payload.agent_id,
            phone_number=payload.phone_number
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Endpoint 2: Verify OTP ---
@router.post("/auth/verified-otp")
async def verify_otp_endpoint(
    payload: VerifyOtpRequest, 
    user=Depends(get_current_user)
):
    try:
        service = get_telegram_service()
        # Call service with exact arguments
        return await service.verify_otp(
            agent_id=payload.agent_id,
            phone_number=payload.phone_number,
            otp_code=payload.otp_code
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# --- 3. Start Session (WAKE UP WORKER) ---
@router.post("/session/start")
async def start_session_endpoint(payload: SessionControlRequest, user=Depends(get_current_user)):
    """
    Call this if the server restarted to reconnect the listener.
    """
    try:
        service = get_telegram_service()
        return await service.start_session(payload.agent_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))