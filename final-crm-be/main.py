"""
File Manager API - Main Entry Point
Clean, modular structure with multi-agent architecture
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware
import logging
import os
import re  # [ADDED] Required for the Preprocessor

# Test Update docker build
# Import configuration
from app.config import settings

# Import API routers
from app.api import documents, agents, chat, organizations, file_manager, crm_agents, crm_chats, whatsapp, webhook, websocket as ws_router, telegram

# Import services
from app.services import get_agent_service

# [ADDED] Import ML Guard for startup verification
from app.utils.ml_guard import ml_guard

# Initialize logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =========================================================================
# 1. DEFINE PREPROCESSOR IN __main__ (CRITICAL FOR PICKLE LOADING)
# =========================================================================
# This class MUST exist here because the .pkl file was trained in a script 
# running as __main__. Moving this will break the model loader.
class MultiServicePreprocessor:
    def __init__(self):
        self.noise_words = [
            'tolong','mohon','bantu','gan','sis','min','kak','minta','harap',
            'halo','permisi','mas','mbak','pak','bu','om','boss','bro'
        ]

        self.service_keywords = {
            'listrik': ['listrik','mcb','token','kwh','meteran','voltase','padam','mati','sekring'],
            'air': ['air','pdam','pipa','bocor','pompa','keruh','tekanan'],
            'internet': ['internet','wifi','modem','router','ont','fiber','lemot','koneksi'],
            'gas': ['gas','lpg','tabung','regulator','kompor','selang'],
            'sanitasi': ['wc','toilet','closet','septictank','saluran','mampet'],
            'ac': ['ac','air conditioner','dingin','freon','kompresor'],
            'elektronik': ['kulkas','mesin cuci','tv','kipas','lampu','setrika'],
            'gedung': ['lift','elevator','eskalator','genset','cctv'],
            'telepon': ['telepon','fax','pabx'],
            'tv_kabel': ['tv kabel','parabola','decoder','channel']
        }

        self.urgency_keywords = [
            'mati total','down','meledak','terbakar','asap','api',
            'bocor','banjir','meluap','bahaya','darurat','cepat',
            'segera','urgent','parah','berbahaya'
        ]

        self.billing_keywords = [
            'tagihan','bayar','biaya','invoice','mahal','tarif',
            'denda','telat','tertunggak','pasang baru','upgrade'
        ]

    def clean_text(self, text):
        text = str(text).lower()
        for w in self.noise_words:
            text = re.sub(rf'\b{w}\b', '', text)
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def inject_features(self, text):
        tags = []

        for svc, kws in self.service_keywords.items():
            if any(k in text for k in kws):
                tags.append(f"__SVC_{svc.upper()}__")

        if any(k in text for k in self.urgency_keywords):
            tags.extend(["__URGENT__"] * 3)

        if any(k in text for k in self.billing_keywords):
            tags.append("__BILLING__")

        return tags

    def process_batch(self, texts):
        out = []
        for t in texts:
            clean = self.clean_text(t)
            tags = self.inject_features(clean)
            out.append(f"{clean} {' '.join(tags)}".strip())
        return out

# [CRITICAL] Inject into __main__ namespace so pickle finds it
import __main__
setattr(__main__, "MultiServicePreprocessor", MultiServicePreprocessor)


# Custom middleware to handle proxy headers from Traefik
class ProxyHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to trust proxy headers from Traefik.
    Fixes 307 redirect issues when using HTTPS with reverse proxy.
    """
    async def dispatch(self, request: Request, call_next):
        # Get forwarded protocol from Traefik headers
        forwarded_proto = request.headers.get("X-Forwarded-Proto")
        forwarded_host = request.headers.get("X-Forwarded-Host")
        forwarded_port = request.headers.get("X-Forwarded-Port")

        # If behind proxy (Traefik), trust the forwarded protocol
        if forwarded_proto:
            # Force scheme to HTTPS if that's what Traefik received
            request.scope["scheme"] = forwarded_proto
            logger.debug(f"🔒 Proxy detected: scheme={forwarded_proto}, host={forwarded_host}, port={forwarded_port}")

        response = await call_next(request)
        return response


# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan (startup/shutdown)"""
    # Startup
    logger.info("Starting File Manager API...")

    # Initialize agents
    agent_service = get_agent_service()
    await agent_service.initialize_agents()

    # [ADDED] Force Load NLP Model (to verify it works on startup)
    logger.info("🧠 Verifying NLP Guard Model...")
    ml_guard._load_model()

    logger.info("Application startup complete")
    yield

    # Shutdown
    logger.info("Application shutdown")


# Create FastAPI application
app = FastAPI(
    title="File Manager API",
    description="""
## 🚀 Syntra AI File Manager & RAG System

A comprehensive document management and retrieval-augmented generation (RAG) platform with multi-agent architecture.
""",
    version="2.0.0",
    lifespan=lifespan,
    # Re-enable redirect_slashes for better UX (307 issues fixed by ProxyHeadersMiddleware)
    redirect_slashes=True,
    # OpenAPI metadata
    contact={
        "name": "Syntra AI Support",
        "url": "https://syntra.id",
        "email": "support@syntra.id",
    },
    license_info={
        "name": "Proprietary",
        "url": "https://syntra.id/license",
    },
    # Swagger UI configuration
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,  # Hide schemas section by default
        "docExpansion": "list",  # Expand only tags, not operations
        "filter": True,  # Enable search
        "syntaxHighlight.theme": "monokai",
        "persistAuthorization": True,  # Remember auth token
    },
    # ReDoc configuration
    redoc_url="/redoc",
    docs_url="/docs",
    openapi_url="/openapi.json"
)

# Add ProxyHeadersMiddleware FIRST (before CORS)
# This ensures request scheme is corrected before any redirects
app.add_middleware(ProxyHeadersMiddleware)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Mount static files if directory exists
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(documents.router)  # Document endpoints (/documents/*)
app.include_router(agents.router)  # Agent endpoints (/agent/*)
app.include_router(chat.router)  # Chat history endpoints (/chat/*)
app.include_router(organizations.router)  # Organization/business endpoints (/organizations/*)
app.include_router(file_manager.router)  # File Manager endpoints (/filemanager/*)
app.include_router(crm_agents.router)  # CRM Agent Management endpoints (/crm/agents/*)
app.include_router(crm_chats.router)  # CRM Chats, Customers, Tickets endpoints (/crm/*)
app.include_router(whatsapp.router)  # WhatsApp Integration endpoints (/whatsapp/*)
app.include_router(webhook.router)  # Webhook endpoints for external services (/webhook/*)
app.include_router(ws_router.router)  # WebSocket endpoint for real-time notifications (/ws/*)
app.include_router(telegram.router)   # Telegram Integration endpoints (/telegram/*)

# Custom OpenAPI schema with enhanced documentation
def custom_openapi():
    """Generate custom OpenAPI schema with enhanced documentation"""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="File Manager API",
        version="2.0.0",
        description=app.description,
        routes=app.routes,
        contact=app.contact,
        license_info=app.license_info,
    )

    # Add security scheme for JWT Bearer authentication
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Enter your JWT token obtained from authentication endpoint"
        },
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API Key for webhook authentication. Use this for webhook endpoints that require authentication via X-API-Key header."
        }
    }

    # Add global security requirement (can be overridden per endpoint)
    openapi_schema["security"] = [{"BearerAuth": []}]

    # Add tags descriptions
    openapi_schema["tags"] = [
        {
            "name": "health",
            "description": "Health check and system status endpoints"
        },
        {
            "name": "file-manager",
            "description": "📁 **File and folder management** - Complete CRUD operations with permissions, sharing, and organization isolation. Includes automatic embeddings and rollback mechanisms."
        },
        {
            "name": "agents",
            "description": "🤖 **Multi-agent AI system** - Specialized AI agents powered by Google ADK. Includes RAG agent for document Q&A and Data Analyst agent for data analysis."
        },
        {
            "name": "documents",
            "description": "📄 **Document processing** - Upload, delete, and query documents with automatic text extraction and embedding. Supports PDF, DOCX, CSV, XLSX, images, and audio."
        },
        {
            "name": "organizations",
            "description": "🏢 **Organization management** - Multi-tenant organization and user management with role-based access control."
        },
        {
            "name": "chat",
            "description": "💬 **Chat history** - Conversation tracking and topic management for agent interactions."
        },
        {
            "name": "crm-agents",
            "description": "👥 **CRM Agent Management** - Comprehensive agent management for customer service. Includes CRUD operations for agents, settings configuration, multi-channel integrations (WhatsApp, Telegram, Email, MCP), and knowledge base management."
        },
        {
            "name": "crm-chats",
            "description": "💬 **CRM Customer Service** - Complete customer service management including customers, chats, messages, tickets, and analytics. Supports multi-channel communication and ticketing system."
        },
        {
            "name": "whatsapp",
            "description": "📱 **WhatsApp Integration** - WhatsApp API integration for agents to send and receive messages. Includes session management, QR code authentication, and message handling (text, media, files)."
        },
        {
            "name": "webhook",
            "description": "🔗 **Webhook Endpoints** - Receive incoming messages from external services (WhatsApp, Telegram, Email). Automatically routes messages to correct chats with customer matching and AI assignment. Requires API key authentication."
        },
        {
            "name": "websocket",
            "description": "⚡ **WebSocket** - Real-time notifications for chat updates and new messages. Frontend clients can connect to receive instant updates for their organization. Requires JWT authentication."
        }
    ]

    # Add examples to common schemas
    if "components" in openapi_schema and "schemas" in openapi_schema["components"]:
        # Add example for common error response
        openapi_schema["components"]["schemas"]["HTTPValidationError"] = {
            "title": "HTTPValidationError",
            "type": "object",
            "properties": {
                "detail": {
                    "title": "Detail",
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "loc": {"type": "array", "items": {"type": "string"}},
                            "msg": {"type": "string"},
                            "type": {"type": "string"}
                        }
                    }
                }
            },
            "example": {
                "detail": [
                    {
                        "loc": ["body", "name"],
                        "msg": "field required",
                        "type": "value_error.missing"
                    }
                ]
            }
        }

    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi


# Root endpoint
@app.get(
    "/",
    tags=["health"],
    summary="API Health Check",
    description="Check if the API is running and healthy. Returns system information and version.",
    response_description="System health status and information"
)
def root():
    """
    Health check endpoint

    Returns basic information about the API including:
    - Status: Whether the API is healthy
    - Message: Welcome message
    - Version: Current API version
    - Architecture: System architecture description
    """
    return {
        "status": "healthy",
        "message": "File Manager API with Multi-Agent System",
        "version": "2.0.0",
        "architecture": "Google ADK Multi-Agent",
        "docs": {
            "swagger_ui": "/docs",
            "redoc": "/redoc",
            "openapi_json": "/openapi.json"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        # WebSocket keepalive configuration
        ws_ping_interval=20.0,  # Send ping every 20 seconds
        ws_ping_timeout=60.0,   # Wait 60 seconds for pong response before closing
        # These settings help keep WebSocket connections alive through proxies and load balancers
    )