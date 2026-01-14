"""Main Entry Point."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from src.config.config import config
from src.database import db
from src.telegram import telegram_manager
from src.api import routes
from src.services.messaging import handle_incoming_message
from src.services.supabase_sync import sync_sessions_from_supabase

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("main")

# --- Lifespan (Startup & Shutdown Logic) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Telegram Worker Service Starting...") 
    
    # 1. Register Message Handler
    telegram_manager.register_message_handler(handle_incoming_message)
    logger.info("Message Handler Registered successfully!")
    
    # 2. Connect to Local Database
    await db.connect()

    # 3. [NEW] Sync Sessions from Supabase
    await sync_sessions_from_supabase()
    
    # 4. Auto-Start Saved Sessions
    try:
        saved_sessions = await db.get_all_sessions()
        count = len(saved_sessions)
        
        if count > 0:
            logger.info(f"Found {count} saved sessions in DB. Auto-starting...")
            
            for session in saved_sessions:
                try:
                    logger.info(f"Connecting {session['account_id']}...")
                    
                    await telegram_manager.add_client(
                        account_id=session['account_id'],
                        api_id=int(session['api_id']),
                        api_hash=session['api_hash'],
                        session_string=session['session_string']
                    )
                except Exception as e:
                    logger.error(f"Failed to restore {session['account_id']}: {e}")
        else:
            logger.info("No sessions found to start.")
            
    except Exception as e:
        logger.error(f"Database auto-start failed: {e}")

    yield  # The application runs here
    
    logger.info("Worker Service Stopping...")
    await telegram_manager.disconnect_all()
    await db.close()


# --- App Definition ---
app = FastAPI(title="Telegram Gateway Userbot", version="3.0.0", lifespan=lifespan)

# --- Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routes ---
@app.get("/")
async def root():
    return {
        "service": "Telegram Worker",
        "status": "Running",
        "type": "Userbot Gateway"
    }

app.include_router(routes.router, prefix="/api")

# --- Execution ---
if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)