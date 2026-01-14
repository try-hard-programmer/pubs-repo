import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    # --- 1. THE LOCK (Protects THIS Worker) ---
    TELEGRAM_SECRET_KEY_SERVICE: str = os.getenv("TELEGRAM_SECRET_KEY_SERVICE", "change_me_in_prod")
    
    # --- 2. THE KEY (Opens the Main API) ---
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "change_me_in_prod")
    
    # --- 3. MAIN SERVICE URL (Where to send webhooks) ---
    MAIN_SERVICE_URL: str = os.getenv("MAIN_SERVICE_URL", "http://localhost:8000/webhook/telegram-userbot")
    
    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8085"))
    
    # Local Database
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "./data/telegram.db")
    
    # Supabase 
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    
    @classmethod
    def ensure_data_dir(cls) -> None:
        db_path = Path(cls.SQLITE_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)

config = Config()