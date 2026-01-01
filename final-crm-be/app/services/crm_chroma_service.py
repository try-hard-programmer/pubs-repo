"""
CRM Chroma Service
Dedicated, read-only service for CRM Agent RAG.
Designed to be fail-safe: if DB is down/incompatible, it returns empty context instead of crashing.
"""
import logging
import chromadb
from chromadb.utils import embedding_functions
from app.config import settings

logger = logging.getLogger(__name__)

class CRMChromaService:
    def __init__(self):
        self.client = None
        self.embedding_function = None
        self._connect()

    def _connect(self):
        """Attempt to connect to ChromaDB with error suppression"""
        try:
            if settings.is_chromadb_cloud_configured:
                self.client = chromadb.CloudClient(
                    tenant=settings.CHROMADB_CLOUD_TENANT,
                    database=settings.CHROMADB_CLOUD_DATABASE,
                    api_key=settings.CHROMADB_CLOUD_API_KEY
                )
            else:
                self.client = chromadb.HttpClient(
                    host=settings.CHROMADB_HOST,
                    port=settings.CHROMADB_PORT
                )
            
            # Setup Embeddings
            # Standard OpenAI Connector (Points to your Proxy via settings.OPENAI_BASE_URL)
            self.embedding_function = embedding_functions.OpenAIEmbeddingFunction(
                api_key=settings.OPENAI_API_KEY,
                api_base=settings.OPENAI_BASE_URL
            )
            logger.info("✅ CRM Chroma Service: Connected safely.")
            
        except Exception as e:
            logger.warning(f"⚠️ CRM Chroma Service: Connection failed ({e}). RAG disabled.")
            self.client = None

    def get_rag_context(self, query: str, organization_id: str, top_k: int = 3) -> str:
        """
        Safe retrieval of context. Returns empty string on any failure.
        """
        if not self.client or not organization_id:
            return ""

        try:
            collection_name = f"org_{organization_id}"
            
            # 1. Get Collection (Fail silently if not exists)
            try:
                collection = self.client.get_collection(
                    name=collection_name,
                    embedding_function=self.embedding_function
                )
            except Exception:
                # Collection doesn't exist yet
                return ""

            # 2. Query
            results = collection.query(
                query_texts=[query],
                n_results=top_k,
                where={
                    "$and": [
                        {"organization_id": {"$eq": organization_id}},
                        {"is_trashed": {"$eq": False}}
                    ]
                }
            )

            # 3. Format Results
            documents = results.get("documents", [])
            if documents and len(documents) > 0:
                flat_docs = documents[0]
                return "\n\n".join(flat_docs)
            
            return ""

        except Exception as e:
            logger.warning(f"⚠️ CRM Chroma Query Failed: {e}")
            return ""

# Singleton Pattern for easy import
_crm_chroma = None

def get_crm_chroma_service():
    global _crm_chroma
    if _crm_chroma is None:
        _crm_chroma = CRMChromaService()
    return _crm_chroma