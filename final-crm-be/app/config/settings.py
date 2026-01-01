"""
Application Configuration
Centralized configuration management using environment variables
"""
import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Settings:
    """Application settings loaded from environment variables"""

    # OpenAI Configuration
    OPENAI_API_KEY: str = os.getenv("PLATFORM_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://proxy.aigent.id/v1")
    OPENAI_MODEL: str = "text-embedding-3-large"
    GPT_MODEL: str = "gpt-3.5-turbo"

    # [NEW] Proxy Configuration
    PROXY_BASE_URL: str = os.getenv("PROXY_BASE_URL", "https://proxy.aigent.id/v1/chat/completions")
    PLATFORM_KEY: str = os.getenv("PLATFORM_KEY", "")

    # Whisper Configuration (deprecated - now using external API)
    WHISPER_MODEL_NAME: str = os.getenv("WHISPER_MODEL_NAME", "large-v3")

    # CDN Configuration
    CDN_UPLOAD_URL: str = os.getenv("CDN_UPLOAD_URL", "https://cdn.satuapp.id/api/upload")

    # Transcription API Configuration
    TRANSCRIPTION_API_TOKEN: str = os.getenv("PLATFORM_KEY", "")
    TRANSCRIPTION_MODEL: str = "v3-large"

    # ChromaDB Configuration (supports both self-hosted and Chroma Cloud)
    # For self-hosted ChromaDB (legacy)
    CHROMADB_HOST: str = os.getenv("CHROMADB_HOST", "103.175.218.139")
    CHROMADB_PORT: int = int(os.getenv("CHROMADB_PORT", "8080"))

    # For Chroma Cloud (recommended)
    CHROMADB_CLOUD_API_KEY: Optional[str] = os.getenv("CHROMADB_CLOUD_API_KEY")
    CHROMADB_CLOUD_TENANT: Optional[str] = os.getenv("CHROMADB_CLOUD_TENANT")
    CHROMADB_CLOUD_DATABASE: Optional[str] = os.getenv("CHROMADB_CLOUD_DATABASE")

    # Collection name
    CHROMADB_COLLECTION_NAME: str = os.getenv("CHROMADB_COLLECTION_NAME", "docs_openai")

    # File Processing Configuration
    IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "tiff", "bmp"}
    MUSIC_EXTENSIONS = {"mp3", "wav", "mp4", "webm", "ogg", "flac"}

    # Chunking Configuration
    DEFAULT_CHUNK_SIZE: int = 300
    DEFAULT_CHUNK_OVERLAP: int = 50
    DEFAULT_BATCH_SIZE: int = 256

    # CORS Configuration
    CORS_ORIGINS = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://console.syntra.id",  # Production frontend
        "http://localhost:3000",  # Development frontend (if using React/Next.js)
        "*"  # Allow all origins (can be removed in production for security)
    ]

    # Supabase Configuration
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")  # Anon key for client
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

    # WhatsApp API Configuration
    WHATSAPP_API_URL: str = os.getenv("WHATSAPP_API_URL", "http://localhost:3000")
    WHATSAPP_API_KEY: Optional[str] = os.getenv("WHATSAPP_API_KEY")

    # Telegram External Service Configuration (The Telethon Worker)
    TELEGRAM_API_URL: str = os.getenv("TELEGRAM_API_URL", "http://localhost:8085")
    TELEGRAM_SECRET_KEY_SERVICE: str = os.getenv("TELEGRAM_SECRET_KEY_SERVICE", "")

    # Webhook Configuration
    WEBHOOK_SECRET_KEY: str = os.getenv("WEBHOOK_SECRET_KEY", "")

    # Webhook Callback URLs (for sending messages to external services)
    WHATSAPP_WEBHOOK_URL: str = os.getenv(
        "WHATSAPP_WEBHOOK_URL",
        "http://localhost:3000/webhook/send"
    )
    TELEGRAM_WEBHOOK_URL: str = os.getenv(
        "TELEGRAM_WEBHOOK_URL",
        "http://localhost:3001/webhook/send"
    )
    EMAIL_WEBHOOK_URL: str = os.getenv(
        "EMAIL_WEBHOOK_URL",
        "http://localhost:3002/webhook/send"
    )

    # Google AI Configuration (for CRM AI agent)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")

    # WebSocket Configuration
    WEBSOCKET_ENABLED: bool = os.getenv("WEBSOCKET_ENABLED", "true").lower() == "true"

    # JWT Configuration (for WebSocket authentication)
    JWT_SECRET_KEY: str = os.getenv("SUPABASE_JWT_SECRET", "")  # Use same secret as Supabase

    @property
    def is_configured(self) -> bool:
        """Check if required configuration is present"""
        return bool(self.OPENAI_API_KEY)

    @property
    def is_supabase_configured(self) -> bool:
        """Check if Supabase configuration is present"""
        return bool(self.SUPABASE_URL and self.SUPABASE_JWT_SECRET)

    @property
    def is_chromadb_cloud_configured(self) -> bool:
        """Check if Chroma Cloud configuration is present"""
        return bool(
            self.CHROMADB_CLOUD_API_KEY
            and self.CHROMADB_CLOUD_TENANT
            and self.CHROMADB_CLOUD_DATABASE
        )


# Global settings instance
settings = Settings()
