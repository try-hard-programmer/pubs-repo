"""
CRM Chroma Service V2
The "Reader" for Team V2.
- Uses LocalProxyEmbeddingFunction (HTTP, No Model).
- Connects to ChromaDB (Port 4003).
- Read-Only Context Retrieval.
"""
import logging
import requests
import chromadb
from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from app.config import settings

logger = logging.getLogger(__name__)

# --- CUSTOM V2 EMBEDDING FUNCTION (HTTP / NO MODEL) ---
class LocalProxyEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        base_url = settings.PROXY_BASE_URL.rstrip("/")
        self.api_url = f"{base_url}/embeddings"

    def __call__(self, input: Documents) -> Embeddings:
        try:
            # Simple payload: Just the text. Proxy handles the model.
            payload = {"input": input}
            
            response = requests.post(
                self.api_url, 
                json=payload, 
                headers={"Content-Type": "application/json"},
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
        except Exception as e:
            logger.error(f"❌ V2 Embedding Failed: {e}")
            raise Exception(f"Embedding Service Unavailable: {e}")

# --- SERVICE ---
class CRMChromaServiceV2:
    def __init__(self):
        self.client = None
        self.embedding_function = LocalProxyEmbeddingFunction()
        self._connect()

    def _connect(self):
        """Connect to Local ChromaDB (usually port 4003)"""
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
            logger.info("✅ CRM Chroma V2: Connected.")
        except Exception as e:
            logger.warning(f"⚠️ CRM Chroma V2 Connection Failed: {e}")

    def query_context(self, query: str, organization_id: str, top_k: int = 5) -> str:
        """
        Retrieves context from agent_{id} collection.
        Returns empty string if anything fails (Fail-Safe).
        """
        if not self.client or not organization_id:
            return ""

        try:
            # In V2, we search the specific AGENT collection or ORG collection
            # Assuming 'agent_{id}' based on upload logic, but here we ask for organization_id?
            # Let's stick to org_{id} if that's your retrieval pattern, 
            # OR agent_{id} if you pass agent_id. 
            # Based on previous code, it used org_{id}. 
            collection_name = f"org_{organization_id}" 
            
            # Try to get collection
            try:
                collection = self.client.get_collection(
                    name=collection_name,
                    embedding_function=self.embedding_function
                )
            except Exception:
                return "" # No memories yet

            # Query
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

            documents = results.get("documents", [])
            if documents and len(documents) > 0:
                return "\n\n".join(documents[0])
            
            return ""

        except Exception as e:
            # [FIX] Gracefully handle the unavailability
            if "Embedding Service Unavailable" in str(e) or "503" in str(e):
                logger.warning(f"⚠️ RAG Skipped: Embedding Service Offline.")
            else:
                logger.warning(f"⚠️ V2 Retrieval Error: {e}")
            return ""

# Singleton
_crm_chroma_v2 = None
def get_crm_chroma_service_v2():
    global _crm_chroma_v2
    if _crm_chroma_v2 is None:
        _crm_chroma_v2 = CRMChromaServiceV2()
    return _crm_chroma_v2