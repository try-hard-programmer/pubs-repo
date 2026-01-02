"""
ChromaDB Service V2
The "Brutal" Version.
- Hardcoded to Localhost Proxy V2 (HTTP).
- Custom Embedding Function using direct requests.
"""
import uuid
import logging
import requests
from typing import List, Dict, Any, Optional, Set
import chromadb
from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from app.config import settings

logger = logging.getLogger(__name__)

# ==========================================
# CUSTOM EMBEDDING FUNCTION (The Fix)
# ==========================================
class LocalProxyEmbeddingFunction(EmbeddingFunction):
    """
    Custom Embedding Function that hits your Local Proxy directly via HTTP.
    """
    def __init__(self):
        # [CHANGE] http instead of https
        self.api_url = "http://localhost:6657/v2/embeddings"
        logger.info(f"🧠 Initialized LocalProxyEmbeddingFunction -> {self.api_url}")

    def __call__(self, input: Documents) -> Embeddings:
        """
        Embed a list of documents using the local proxy.
        """
        try:
            payload = {
                "input": input,
                "model": "text-embedding-3-small"
            }
            
            # [CHANGE] No verify=False needed for HTTP, but keeping timeout
            response = requests.post(
                self.api_url, 
                json=payload, 
                headers={"Content-Type": "application/json"},
                timeout=60
            )
            
            response.raise_for_status()
            data = response.json()
            
            embeddings = [item["embedding"] for item in data["data"]]
            return embeddings

        except Exception as e:
            logger.error(f"❌ Embedding Generation Failed: {e}")
            raise RuntimeError(f"Proxy Embedding Failed: {str(e)}")


# ==========================================
# SERVICE CLASS
# ==========================================
class ChromaDBServiceV2:
    """
    Service for managing ChromaDB operations (V2).
    """

    def __init__(self):
        """Initialize ChromaDB client with the HTTP embedding function"""
        
        # 1. Connect to ChromaDB (Port 4003 as you confirmed)
        if settings.is_chromadb_cloud_configured:
            logger.info(f"🌐 Connecting to Chroma Cloud")
            self.client = chromadb.CloudClient(
                tenant=settings.CHROMADB_CLOUD_TENANT,
                database=settings.CHROMADB_CLOUD_DATABASE,
                api_key=settings.CHROMADB_CLOUD_API_KEY
            )
        else:
            logger.info(f"🏠 Connecting to self-hosted ChromaDB ({settings.CHROMADB_HOST}:{settings.CHROMADB_PORT})")
            self.client = chromadb.HttpClient(
                host=settings.CHROMADB_HOST,
                port=settings.CHROMADB_PORT
            )

        # 2. Set the Brain (The HTTP Embedding Function)
        self.embedding_function = LocalProxyEmbeddingFunction()

    def _get_collection_name(self, organization_id: str) -> str:
        return f"org_{organization_id}"

    def get_or_create_organization_collection(self, organization_id: str):
        collection_name = self._get_collection_name(organization_id)
        try:
            return self.client.get_or_create_collection(
                name=collection_name,
                embedding_function=self.embedding_function,
                metadata={"hnsw:space": "cosine", "organization_id": organization_id}
            )
        except Exception as e:
            logger.error(f"Failed to get/create collection {collection_name}: {e}")
            raise RuntimeError(f"ChromaDB Collection Error: {e}")

    def add_chunks(
        self,
        chunks: List[str],
        filename: str,
        organization_id: str,
        file_id: Optional[str] = None,
        batch_size: int = 256,
        email: Optional[str] = None
    ) -> str:
        if not organization_id:
            raise ValueError("organization_id required")

        collection = self.get_or_create_organization_collection(organization_id)
        file_id = file_id or uuid.uuid4().hex

        all_ids = [f"{file_id}-{i}" for i in range(len(chunks))]
        all_metas = [
            {
                "file_id": file_id,
                "filename": filename,
                "chunk_index": i,
                "email": email,
                "organization_id": organization_id,
                "is_trashed": False,
                "processor": "v2_proxy_http"
            }
            for i in range(len(chunks))
        ]

        for start in range(0, len(chunks), batch_size):
            end = start + batch_size
            collection.add(
                documents=chunks[start:end],
                ids=all_ids[start:end],
                metadatas=all_metas[start:end]
            )

        logger.info(f"✅ [V2] Added {len(chunks)} chunks to {organization_id}")
        return file_id

    def delete_documents_by_file_id(self, organization_id: str, file_id: str) -> Dict[str, Any]:
        collection = self.get_or_create_organization_collection(organization_id)
        collection.delete(where={"file_id": {"$eq": file_id}})
        logger.info(f"🗑️ [V2] Deleted docs for file {file_id}")
        return {"status": "deleted", "file_id": file_id}