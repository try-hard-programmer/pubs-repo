"""
File Manager API - Main Entry Point
Clean, modular structure with multi-agent architecture
"""
import logging 
import os
import asyncio 
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

# Import configuration
from app.config import settings
from app.services.llm_queue_service import get_llm_queue

# Import API routers
from app.api import documents, agents, chat, organizations, file_manager, crm_agents, crm_chats, whatsapp, webhook, websocket as ws_router, telegram

# Import services
from app.services import get_agent_service
from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2 
from app.services.document_queue_service import get_document_worker
from app.services.websocket_service import start_redis_pubsub_listener, connection_manager # Initialize logger

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


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

    # [NEW] Start LLM Queue Worker
    queue_service = get_llm_queue()
    asyncio.create_task(queue_service.start_worker())

    # Preload reranker model (avoid 18s delay on first query)
    chroma_service = get_crm_chroma_service_v2()
    chroma_service.preload_pdf_models()  # ← FIRST (sets HF_HOME env vars)
    chroma_service._get_reranker()       # ← SECOND (uses env vars already set)

    # Start Document Processing Worker (Redis-based, runs in daemon thread)
    doc_worker = get_document_worker()
    doc_worker.start_in_thread()

    # TURN ON THE REDIS LISTENER
    redis_listener_task = asyncio.create_task(start_redis_pubsub_listener(connection_manager))

    logger.info("Application startup complete")
    yield

    # Shutdown 
    logger.info("Application shutdown")
    queue_service.is_running = False
    doc_worker.stop()
    # Safely cancel the listener when the server shuts down
    redis_listener_task.cancel()


# Create FastAPI application
app = FastAPI(
    title="File Manager API",
    description="""
## 🚀 Syntra AI File Manager & RAG System

A comprehensive document management and retrieval-augmented generation (RAG) platform with multi-agent architecture.

### Key Features

- **📁 File Management**: Complete CRUD operations for files and folders with organization-scoped isolation
- **🤖 Multi-Agent System**: Specialized AI agents powered by Google ADK
- **🔍 Semantic Search**: Advanced document retrieval using ChromaDB vector database
- **🔐 Permission System**: Granular access control with 5 permission levels (view, edit, delete, share, manage)
- **🤝 Sharing**: Share files with users, groups, or create public links
- **🎯 Organization Isolation**: Complete data separation for multi-tenant architecture
- **📊 Embeddings**: Automatic document embedding with rollback on failure
- **💬 Chat History**: Conversation tracking with reference documents

### Architecture

- **Backend**: FastAPI 3.0+ with async/await
- **Database**: PostgreSQL with Supabase (Row Level Security)
- **Storage**: Supabase Storage with hierarchical organization
- **Vector DB**: ChromaDB for semantic search
- **AI**: Google Generative AI (Gemini) with ADK agents
- **Auth**: JWT-based authentication with role-based access control

### Authentication

All endpoints (except health checks and public shares) require JWT authentication:
```bash
Authorization: Bearer <your-jwt-token>
```

### Organization Model

Users belong to organizations, and all data is scoped by organization:
- Documents are stored in org-specific ChromaDB collections
- Files are stored in org-specific Supabase Storage buckets
- Permissions and shares are isolated per organization

### Getting Started

1. **Create Organization**: POST /organizations/
2. **Upload Document**: POST /documents/upload
3. **Query Agent**: POST /agent/rag
4. **Manage Files**: Use /filemanager/* endpoints

### Support

- Documentation: [GitHub Wiki](https://github.com/yourusername/filemanager-api/wiki)
- Issues: [GitHub Issues](https://github.com/yourusername/filemanager-api/issues)
- API Version: v2.0.0
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
