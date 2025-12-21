import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from src.database import db
from src.telegram import telegram_manager
from src.middleware.auth import verify_secret_key
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

# Set up logger
logger = logging.getLogger("telegram_backend")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

router = APIRouter(dependencies=[Depends(verify_secret_key)])

# --- Models ---
class StartSessionRequest(BaseModel):
    account_id: str
    api_id: int
    api_hash: str
    session_string: str

class WebhookSendRequest(BaseModel):
    agent_id: str   
    chat_id: str    
    text: str = "" # Default to empty to allow file-only messages
    media_url: Optional[str] = None # Added for file support

class InitAuthRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str

class VerifyAuthRequest(BaseModel):
    phone: str
    code: str
    api_id: int
    api_hash: str

class SendMessageRequest(BaseModel):
    chat_id: str
    text: str

# --- SESSION MANAGEMENT (Stateless) ---

@router.post("/sessions/start")
async def start_session(req: StartSessionRequest):
    logger.info(f"Start session requested for account_id: {req.account_id}")
    try:
        # 1. Start in Memory (Immediate connection)
        await telegram_manager.add_client(
            account_id=req.account_id,
            api_id=req.api_id,
            api_hash=req.api_hash,
            session_string=req.session_string
        )
        
        # 2. [NEW] Save to DB (Persistence)
        # This writes it to the telegram.db file
        await db.save_session(
            account_id=req.account_id,
            api_id=str(req.api_id),
            api_hash=req.api_hash,
            session_string=req.session_string
        )

        logger.info(f"Session started AND saved to DB: {req.account_id}")
        return {"status": "started", "account_id": req.account_id}
    except Exception as e:
        logger.error(f"Error starting session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/sessions/stop/{account_id}")
async def stop_session(account_id: str):
    """Stop a running client in memory."""
    logger.info(f"Stop session requested for account_id: {account_id}")
    try:
        await telegram_manager.remove_client(account_id)
        logger.info(f"Session stopped successfully for account_id: {account_id}")
        return {"status": "stopped"}
    except Exception as e:
        logger.error(f"Error during stop session for account_id: {account_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions")
async def list_active_sessions():
    """List currently running clients in memory."""
    logger.info("Listing active sessions")
    active_accounts = list(telegram_manager.clients.keys())
    logger.info(f"Active accounts: {active_accounts}")
    return {"active_accounts": active_accounts}

@router.post("/auth/init")
async def init_auth(req: InitAuthRequest):
    """Helper to request login code. Returns hash to caller."""
    
    # 1. NORMALIZE PHONE HERE TOO! (Crucial)
    clean_phone = req.phone.replace("+", "").replace(" ", "").strip()
    
    logger.info(f"Init auth requested for phone: {clean_phone}, api_id: {req.api_id}")
    
    try:
        # Create client
        client = TelegramClient(StringSession(), req.api_id, req.api_hash)
        await client.connect()
        
        # Send code (Telegram accepts + format, so we can use req.phone or clean_phone)
        sent = await client.send_code_request(req.phone)
        
        # 2. SAVE USING CLEAN PHONE KEY
        key = f"temp_{clean_phone}"
        telegram_manager.clients[key] = client

        logger.info(f"✅ Login code sent. Stored client with key: {key}")
        
        return {
            "status": "code_sent", 
            "phone_code_hash": sent.phone_code_hash
        }
    except Exception as e:
        logger.error(f"Error during init_auth for phone: {req.phone}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/auth/verify")
async def verify_auth(req: VerifyAuthRequest):
    """
    Helper to submit code. 
    RETURNS session string so Main Service can save it.
    """
    # 1. Normalize Phone
    clean_phone = req.phone.replace("+", "").replace(" ", "").strip()
    temp_key = f"temp_{clean_phone}"
    
    logger.info(f"🔍 Looking for session key: {temp_key}")
    
    # 2. Retrieve Client
    client = telegram_manager.clients.get(temp_key)
    
    if not client:
        logger.error(f"❌ Session NOT found for {temp_key}")
        raise HTTPException(status_code=400, detail="Session expired or server restarted.")
        
    try:
        # 3. Sign In
        await client.sign_in(req.phone, req.code)
        
        # 4. Get the Session String
        final_session = client.session.save()
        
        # 5. Get User ID (MUST AWAIT THESE!)
        me = await client.get_me()
        user_id = me.id if me else None
        
        # 6. Cleanup
        await client.disconnect()
        del telegram_manager.clients[temp_key]
        
        logger.info(f"✅ Authentication successful for {clean_phone} (ID: {user_id})")
        
        return {
            "status": "success", 
            "session_string": final_session,
            "user_id": user_id
        }

    except Exception as e:
        logger.error(f"❌ Verify failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/messages/send/{account_id}")
async def send_message(account_id: str, req: SendMessageRequest):
    """Send outgoing message using active client."""
    logger.info(f"Send message requested for account_id: {account_id}, chat_id: {req.chat_id}, text: {req.text}")
    try:
        msg_id = await telegram_manager.send_message(account_id, req.chat_id, req.text)
        if not msg_id:
            logger.error(f"Failed to send message for account_id: {account_id}, chat_id: {req.chat_id}")
            raise HTTPException(500, "Client not active or failed to send")
        
        # Save to DB (History)
        await db.save_message(
            telegram_account_id=account_id,
            chat_id=req.chat_id,
            message_id=str(msg_id),
            direction="outgoing",
            text=req.text,
            status="sent"
        )
        logger.info(f"Message sent successfully for account_id: {account_id}, chat_id: {req.chat_id}")
        return {"status": "sent", "message_id": msg_id}
    except Exception as e:
        logger.error(f"Error sending message for account_id: {account_id}, chat_id: {req.chat_id}: {str(e)}")
        raise HTTPException(500, detail=str(e))

@router.get("/conversations/{account_id}/{chat_id}")
async def get_history(account_id: str, chat_id: str):
    """Get conversation history from local DB."""
    logger.info(f"Fetching conversation history for account_id: {account_id}, chat_id: {chat_id}")
    try:
        history = await db.get_messages(account_id, chat_id)
        logger.info(f"Conversation history fetched for account_id: {account_id}, chat_id: {chat_id}")
        return history
    except Exception as e:
        logger.error(f"Error fetching conversation history for account_id: {account_id}, chat_id: {chat_id}: {str(e)}")
        raise HTTPException(500, detail=str(e))

# [CRITICAL ENDPOINT FOR YOUR FIX]
@router.post("/webhook/send")
async def webhook_send_message(req: WebhookSendRequest):
    """
    The 'Catcher' endpoint. 
    Main API thinks it's hitting a generic webhook, but we catch it 
    and use Telethon to send the reply.
    """
    logger.info(f"🪝 Webhook received reply for account: {req.agent_id} -> user: {req.chat_id}")
    
    try:
        # [FIX] Pass media_url to manager
        result = await telegram_manager.send_message(
            account_id=req.agent_id,
            chat_id=req.chat_id,
            text=req.text,
            media_url=req.media_url
        )
        
        if not result:
             raise HTTPException(500, "Failed to send message (client active?)")
             
        msg_id, resolved_chat_id = result

        # Optional: Save to local DB History (Outgoing)
        await db.save_message(
            telegram_account_id=req.agent_id,
            chat_id=str(resolved_chat_id),
            message_id=str(msg_id),
            direction="outgoing",
            text=req.text or "[MEDIA]",
            status="sent"
        )

        return {
            "status": "success", 
            "message_id": msg_id, 
            "resolved_chat_id": str(resolved_chat_id),
            "detail": "Sent via Userbot Loopback"
        }

    except Exception as e:
        logger.error(f"❌ Webhook send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))    

