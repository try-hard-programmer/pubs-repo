import logging
import requests
import chromadb
from chromadb import Settings
from typing import List, Any, Dict, Tuple
from app.config import settings
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType

logger = logging.getLogger(__name__)

class LocalProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _call_api(self, input: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        payload = { "model": "text-embedding-3-small", "input": input }
        headers = { "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}" }

        try:
            response = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                logger.error(f"❌ Proxy Error {response.status_code}")
                return [], {}

            data = response.json()
            
            # Extract Usage & Cost from Proxy Response
            usage = data.get("usage", {})
            metadata = data.get("metadata", {})
            
            # [NEW] Inject cost from proxy metadata into usage dict for easier access
            if "cost_usd" in metadata:
                usage["cost_usd"] = metadata["cost_usd"]

            embeddings = []
            if isinstance(data, dict) and "data" in data:
                embeddings = [item["embedding"] for item in data["data"]]
            elif isinstance(data, list):
                embeddings = data
            
            return embeddings, usage

        except Exception as e:
            logger.error(f"❌ Embedding API Crash: {e}")
            return [], {}

    def __call__(self, input: Any) -> List[List[float]]:
        if isinstance(input, str): input = [input]
        embeddings, _ = self._call_api(input)
        return embeddings

    def embed_with_usage(self, input: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        return self._call_api(input)


class CRMChromaServiceV2:
    def __init__(self):
        self.client = chromadb.HttpClient(
            host=settings.CHROMA_DB_HOST, 
            port=settings.CHROMA_DB_PORT,
            settings=Settings(allow_reset=True, anonymized_telemetry=False)
        )
        proxy_url = settings.CRM_EMBEDDING_API_URL or "http://localhost:6657/v2/embeddings"
        api_key = settings.CRM_EMBEDDING_API_KEY or "dummy-key"
        
        self.embedding_fn = LocalProxyEmbeddingFunction(base_url=proxy_url, api_key=api_key)
        self.credit_service = get_credit_service()

    def get_or_create_collection(self, agent_id: str):
        return self.client.get_or_create_collection(
            name=f"agent_{agent_id}",
            embedding_function=self.embedding_fn
        )

    # [ASYNC] Handles Billing + Chroma Save
    async def add_documents(self, agent_id: str, texts: List[str], metadatas: List[Dict], organization_id: str = None):
        try:
            if not texts: return False

            logger.info(f"🧠 Embedding {len(texts)} chunks for Agent {agent_id}...")
            embeddings, usage = self.embedding_fn.embed_with_usage(texts)

            if not embeddings:
                raise Exception("Embedding failed (Empty response)")

            # --- BILLING LOGIC ---
            if organization_id:
                cost = 0.0
                # Priority 1: Use Cost from Proxy
                if "cost_usd" in usage:
                    cost = float(usage["cost_usd"])
                # Priority 2: Calculate from Tokens (Fallback)
                elif "total_tokens" in usage:
                    cost = usage["total_tokens"] * 0.0000002
                
                if cost > 0:
                    await self.credit_service.add_transaction(CreditTransactionCreate(
                        organization_id=organization_id,
                        amount=-cost, 
                        description=f"Knowledge Embedding ({len(texts)} chunks)",
                        transaction_type=TransactionType.USAGE,
                        metadata={"agent_id": agent_id, "provider": "openai"}
                    ))
                    logger.info(f"💰 Deducted ${cost:.6f} for embedding.")

            # --- SAVE LOGIC ---
            collection = self.get_or_create_collection(agent_id)
            ids = [f"{m.get('doc_id')}_{i}" for i, m in enumerate(metadatas)]
            
            collection.add(
                embeddings=embeddings, 
                documents=texts,
                metadatas=metadatas,
                ids=ids
            )
            
            logger.info(f"✅ Successfully stored {len(texts)} chunks.")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to add documents: {e}")
            return False

    async def query_context(self, query: str, agent_id: str, n_results: int = 3) -> str:
        try:
            collection = self.get_or_create_collection(agent_id)
            results = collection.query(query_texts=[query], n_results=n_results)
            if not results["documents"] or not results["documents"][0]: return ""
            return "\n\n".join(results["documents"][0])
        except Exception as e:
            logger.error(f"⚠️ Query failed: {e}")
            return ""

    def delete_collection(self, agent_id: str):
        try:
            self.client.delete_collection(f"agent_{agent_id}")
            return True
        except: return False

_crm_chroma_service_v2 = None
def get_crm_chroma_service_v2():
    global _crm_chroma_service_v2
    if _crm_chroma_service_v2 is None:
        _crm_chroma_service_v2 = CRMChromaServiceV2()
    return _crm_chroma_service_v2