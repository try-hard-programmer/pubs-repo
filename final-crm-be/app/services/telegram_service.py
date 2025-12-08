"""
Telegram Service (Middle Service Proxy)
Strict implementation for OTP Flow.
"""
import logging
import httpx
from typing import Optional, Dict, Any
from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)

class TelegramService:
    def __init__(self):
        # Config pointing to Worker (Port 8085)
        self.base_url = settings.TELEGRAM_API_URL.rstrip("/")
        self.api_key = settings.TELEGRAM_SECRET_KEY_SERVICE
        self.timeout = 60.0
        
        # Supabase Client for DB Access
        self._supabase: Optional[Client] = None

    @property
    def supabase(self) -> Client:
        if not self._supabase:
            self._supabase = create_client(
                settings.SUPABASE_URL, 
                settings.SUPABASE_SERVICE_KEY
            )
        return self._supabase

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Service-Key": self.api_key
        }

    async def _get_credentials(self, agent_id: str) -> Dict[str, Any]:
        """Fetch API ID/Hash from agent_integrations table"""
        response = self.supabase.table("agent_integrations") \
            .select("config") \
            .eq("agent_id", agent_id) \
            .eq("channel", "telegram") \
            .execute()

        if not response.data:
            raise Exception("Telegram integration not configured for this agent")

        config = response.data[0].get("config") or {}
        if not config.get("api_id") or not config.get("api_hash"):
            raise Exception("Missing api_id or api_hash in configuration")
            
        return config

    # =================================================================
    # 1. SEND OTP FUNCTION
    # =================================================================
    async def send_otp(self, agent_id: str, phone_number: str) -> Dict[str, Any]:
        """
        Step 1: Get Creds -> Call Worker -> Return Hash
        """
        try:
            # 1. Get Creds from DB
            creds = await self._get_credentials(agent_id)

            # 2. Prepare Payload for Worker
            payload = {
                "api_id": int(creds["api_id"]),
                "api_hash": str(creds["api_hash"]),
                "phone": phone_number
            }

            # 3. Call Worker (/auth/init)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                url = f"{self.base_url}/auth/init"
                response = await client.post(url, json=payload, headers=self._get_headers())
                
                if response.status_code >= 400:
                    raise Exception(f"Worker Error: {response.text}")
                
                return response.json()

        except Exception as e:
            logger.error(f"Send OTP Failed: {e}")
            raise

    # =================================================================
    # 2. VERIFY OTP FUNCTION
    # =================================================================
    async def verify_otp(self, agent_id: str, phone_number: str, otp_code: str) -> Dict[str, Any]:
        """
        Step 2: Get Creds -> Call Worker -> Save Session to DB
        """
        try:
            # 1. Get Creds from DB
            creds = await self._get_credentials(agent_id)

            # 2. Prepare Payload for Worker
            payload = {
                "api_id": int(creds["api_id"]),
                "api_hash": str(creds["api_hash"]),
                "phone": phone_number,
                "code": otp_code
            }

            # 3. Call Worker (/auth/verify)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                url = f"{self.base_url}/auth/verify"
                response = await client.post(url, json=payload, headers=self._get_headers())
                
                if response.status_code >= 400:
                    raise Exception(f"Worker Error: {response.text}")
                
                result = response.json()
                
                # 4. SAVE SESSION TO DB (Crucial Step)
                if "session_string" in result:
                    await self._update_db_session(agent_id, result["session_string"], creds)
                
                return result

        except Exception as e:
            logger.error(f"Verify OTP Failed: {e}")
            raise

    async def _update_db_session(self, agent_id: str, session: str, old_config: Dict):
        """Helper to save the session string back to Supabase"""
        new_config = old_config.copy()
        new_config["session"] = session # Use "session" key for consistency
        new_config["status"] = "connected"
        
        self.supabase.table("agent_integrations").update({
            "config": new_config,
            "status": "connected",
            "last_connected_at": "now()"
        }).eq("agent_id", agent_id).eq("channel", "telegram").execute()

    # =================================================================
    # 3. START SESSION (The Missing Piece!)
    # =================================================================
    async def start_session(self, agent_id: str) -> Dict[str, Any]:
        """Wake up the worker and attach listener"""
        try:
            creds = await self._get_credentials(agent_id)
            # Support both key names just in case
            session = creds.get("session") or creds.get("session_string")
            
            if not session:
                raise Exception("No session found. Please login first.")

            # Payload matches Worker's /sessions/start
            payload = {
                "account_id": agent_id,
                "api_id": int(creds["api_id"]),
                "api_hash": str(creds["api_hash"]),
                "session_string": session
            }

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                url = f"{self.base_url}/sessions/start"
                response = await client.post(url, json=payload, headers=self._get_headers())
                if response.status_code >= 400: raise Exception(f"Worker Error: {response.text}")
                return response.json()
                
        except Exception as e:
            logger.error(f"Start Session Failed: {e}")
            raise


# Singleton
_service = TelegramService()
def get_telegram_service():
    return _service