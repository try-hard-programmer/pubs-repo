import logging
import requests
import chromadb
import torch
import os

from chromadb import Settings
from typing import List, Any, Dict, Tuple
from app.config import settings
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from langchain_community.vectorstores import Chroma
from langchain.retrievers import EnsembleRetriever
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

class LocalProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = "proxy-managed"

    def _call_api(self, input: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        payload = { "input": input }
        headers = { "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}" }

        try:
            response = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                logger.error(f"❌ Proxy Error {response.status_code}")
                return [], {}

            data = response.json()
            usage = data.get("usage", {})
            metadata = data.get("metadata", {})
            
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


class LangChainProxyEmbedding(Embeddings):
    def __init__(self, proxy_fn: LocalProxyEmbeddingFunction):
        self.proxy_fn = proxy_fn

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.proxy_fn(texts)

    def embed_query(self, text: str) -> List[float]:
        result = self.proxy_fn([text])
        return result[0] if result else []

# 2. SERVICE CLASS (With Transformer Reranking)
class CRMChromaServiceV2:
    def __init__(self):
        try:
            self.client = chromadb.HttpClient(
                host=settings.CHROMADB_HOST, 
                port=settings.CHROMADB_PORT,
                settings=Settings(allow_reset=True, anonymized_telemetry=False)
            )

            self.client.heartbeat()
            logger.info(f"✅ Connected to ChromaDB at {settings.CHROMADB_HOST}:{settings.CHROMADB_PORT}")
        except Exception as e:
            logger.error(f"⚠️ ChromaDB Connection Failed: {e}")
            self.client = None  # Set to None so we can check later

        default_proxy_url = f"{settings.PROXY_BASE_URL}/embeddings" if hasattr(settings, "PROXY_BASE_URL") else "http://localhost:6657/v2/embeddings"
        proxy_url = getattr(settings, "CRM_EMBEDDING_API_URL", None) or default_proxy_url
        
        self.embedding_fn = LocalProxyEmbeddingFunction(base_url=proxy_url)
        self.credit_service = get_credit_service()
        
        # Lazy load reranker
        self._reranker_model = None
        self._reranker_tokenizer = None
        self._reranker_loaded = False

    def _get_reranker(self):
        if not self._reranker_loaded:
            try:
                # 1. Path Configuration
                model_path = settings.RERANKER_MODEL_PATH
                model_name = "cross-encoder/ms-marco-MiniLM-L-6-v2"
                
                # 2. Check Existence & Define Source
                if os.path.exists(os.path.join(model_path, "config.json")):
                    logger.info(f"🔄 Loading reranker from {model_path}...")
                    source = model_path
                else:
                    logger.info(f"🔄 Downloading reranker (source: {model_name})...")
                    source = model_name
                
                # 3. Load Model & Tokenizer
                self._reranker_tokenizer = AutoTokenizer.from_pretrained(source)
                self._reranker_model = AutoModelForSequenceClassification.from_pretrained(source)
                
                # 4. Save if downloaded
                if source == model_name:
                    try:
                        os.makedirs(model_path, exist_ok=True)
                        self._reranker_tokenizer.save_pretrained(model_path)
                        self._reranker_model.save_pretrained(model_path)
                        logger.info(f"💾 Model saved to: {model_path}")
                    except Exception as save_err:
                        logger.warning(f"⚠️ Could not save model to {model_path}: {save_err}")
                
                # 5. Device Setup
                
                # [COMMENT 1] Standard logic: Check if GPU is available
                # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                
                # [COMMENT 2] Force into CPU (Overrides GPU check)
                device = torch.device("cpu")
                
                self._reranker_model = self._reranker_model.to(device)
                self._reranker_model.eval()
                self._reranker_loaded = True
                
                logger.info(f"✅ Reranker loaded on {device} (Forced CPU)")
                
            except Exception as e:
                logger.error(f"❌ Failed to load reranker: {e}")
                self._reranker_loaded = False
                return None, None
        
        return self._reranker_model, self._reranker_tokenizer
       
    def get_or_create_collection(self, agent_id: str):
        if not self.client:
            logger.error("❌ ChromaDB client is not connected.")
            raise ConnectionError("ChromaDB is unavailable")
            
        return self.client.get_or_create_collection(
            name=agent_id, 
            embedding_function=self.embedding_fn
        )

    async def add_documents(self, agent_id: str, texts: List[str], metadatas: List[Dict], organization_id: str = None):
        try:
            if not texts: return False

            embeddings, usage = self.embedding_fn.embed_with_usage(texts)

            if not embeddings: raise Exception("Embedding failed")

            # 💰 BILLING LOGIC
            if organization_id:
                try:
                    cost = 0.0
                    if "cost_usd" in usage: cost = float(usage["cost_usd"])
                    elif "total_tokens" in usage: cost = usage["total_tokens"] * 0.0000002
                    
                    if cost > 0:
                        logger.info(f"💸 Calculated Cost: ${cost:.6f}. Attempting deduction for Org {organization_id}...")
                        
                        tx_result = await self.credit_service.add_transaction(CreditTransactionCreate(
                            organization_id=organization_id,
                            amount=-cost, 
                            description=f"Knowledge Embedding ({len(texts)} chunks)",
                            transaction_type=TransactionType.USAGE,
                            metadata={"agent_id": agent_id, "provider": "openai"}
                        ))
                        
                        logger.info(f"💰 Deduction successful. Transaction Object: {tx_result}")
                    else:
                        logger.info("🆓 Cost was $0.00. No deduction made.")
                        
                except Exception as billing_err:
                    logger.error(f"🚨 BILLING CRASHED (Money saved, but logic failed): {billing_err}")

            # 💾 STORAGE LOGIC
            collection = self.get_or_create_collection(agent_id)
            ids = [f"{m.get('doc_id')}_{i}" for i, m in enumerate(metadatas)]
            
            collection.add(embeddings=embeddings, documents=texts, metadatas=metadatas, ids=ids)
            logger.info(f"✅ Successfully stored {len(texts)} chunks.")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to add documents: {e}")
            return False

    async def query_context(self, query: str, agent_id: str, n_results: int = 5) -> str:
        """
        Triple-Layer Hybrid RAG (Robust Mode)
        Layer 1: BM25 (Keywords) - Works even if AI is down
        Layer 2: Vector Search (Semantic) - Requires Embedding API
        Layer 3: Transformer Reranker (Neural) - Requires CPU/GPU Model
        """
        try:
            clean_query = query.strip()
            
            # Skip meaningless queries
            if len(clean_query) < 5 or clean_query.lower() in ["test", "halo", "hi", "ping", "p", "hello"]:
                logger.info(f"⏭️ Skipping trivial query: '{clean_query}'")
                return ""
            
            collection = self.get_or_create_collection(agent_id)
            
            # Get all documents from ChromaDB
            all_docs_data = collection.get(include=['documents', 'metadatas'])
            
            if not all_docs_data['documents'] or len(all_docs_data['documents']) == 0:
                logger.warning(f"⚠️ No documents found for agent {agent_id}")
                return ""
            
            total_docs = len(all_docs_data['documents'])
            logger.info(f"🔎 RAG Query: '{clean_query}' → Searching {total_docs} documents")
            
            # Convert to LangChain Document format
            documents = [
                Document(
                    page_content=doc, 
                    metadata=meta if meta else {}
                )
                for doc, meta in zip(all_docs_data['documents'], all_docs_data['metadatas'])
            ]
            
            # LAYER 1: BM25 Retriever (Keyword Matching)            
            bm25_retriever = BM25Retriever.from_documents(documents)
            bm25_retriever.k = 50  # Get top 50 candidates
                        
            # LAYER 2: Vector Search (Semantic Matching)
            vectorstore = Chroma(
                client=self.client,
                collection_name=agent_id,
                embedding_function=LangChainProxyEmbedding(self.embedding_fn)  
            )
            vector_retriever = vectorstore.as_retriever(
                search_type="similarity",
                search_kwargs={"k": 50}  # Get top 50 candidates
            )
            
            logger.info("🧠 Vector retriever initialized")
            
            # [CRITICAL FIX] SAFE EXECUTION BLOCK
            # We try Hybrid Search. If Embedding API is down, it crashes.
            # We catch that crash and fallback to BM25 (Keywords) only.
            hybrid_results = []
            
            try:
                # ENSEMBLE: Combine BM25 + Vector
                ensemble_retriever = EnsembleRetriever(
                    retrievers=[bm25_retriever, vector_retriever],
                    weights=[0.3, 0.7]
                )
                hybrid_results = ensemble_retriever.invoke(clean_query)
                logger.info(f"✅ Hybrid search returned {len(hybrid_results)} candidates")
                
            except Exception as vector_error:
                # 🚨 EMBEDDING API IS DOWN
                logger.error(f"⚠️ Vector Search Failed (Embedding API likely down): {vector_error}")
                logger.warning("🔄 FALLBACK: Switching to BM25 (Keyword Only) Search mode.")
                
                # Fallback to just Keyword Search
                hybrid_results = bm25_retriever.invoke(clean_query)
                logger.info(f"✅ BM25 Fallback returned {len(hybrid_results)} candidates")

            if not hybrid_results or len(hybrid_results) == 0:
                logger.warning("⚠️ No results found.")
                return ""
            
            # LAYER 3: Neural Reranker (Transformer Model)   
            model, tokenizer = self._get_reranker()
            
            if model and tokenizer and len(hybrid_results) > n_results:
                try:
                    # Extract text content from LangChain Documents
                    candidates = [doc.page_content for doc in hybrid_results[:50]]
                    
                    # Prepare query-document pairs
                    pairs = [[clean_query, doc] for doc in candidates]
                    
                    logger.info(f"🎯 Reranking top {len(pairs)} candidates...")
                    
                    with torch.no_grad():
                        inputs = tokenizer(
                            pairs, 
                            padding=True, 
                            truncation=True, 
                            return_tensors='pt', 
                            max_length=512
                        )
                        scores = model(**inputs).logits.squeeze(-1)
                        if scores.is_cuda: scores = scores.cpu()
                        scores = scores.tolist()
                    
                    scored_results = list(zip(hybrid_results[:50], scores))
                    scored_results.sort(key=lambda x: x[1], reverse=True)
                    
                    final_results = [doc.page_content for doc, score in scored_results[:n_results]]
                    logger.info(f"🏆 Reranking complete. Top score: {scored_results[0][1]:.4f}")
                    
                except Exception as rerank_error:
                    logger.error(f"⚠️ Reranking failed, using raw results: {rerank_error}")
                    final_results = [doc.page_content for doc in hybrid_results[:n_results]]
            
            else:
                final_results = [doc.page_content for doc in hybrid_results[:n_results]]
            
            return "\n\n###\n\n".join(final_results)
            
        except Exception as e:
            logger.error(f"❌ Query context failed: {e}", exc_info=True)
            return ""
        
    def delete_document(self, agent_id: str, file_id: str):
        """
        Smart Delete:
        1. Deletes vectors for the specific file.
        2. Checks if collection is empty.
        3. If empty, DELETES THE COLLECTION (Clean Slate).
        """
        try:
            try:
                collection = self.client.get_collection(name=agent_id, embedding_function=self.embedding_fn)
            except ValueError:
                logger.warning(f"⚠️ Collection '{agent_id}' already gone. Skipping.")
                return True

            collection.delete(where={"file_id": {"$eq": file_id}})
            
            remaining_count = collection.count()
            
            if remaining_count == 0:
                self.client.delete_collection(name=agent_id)
                logger.info(f"🔥 [Auto-Cleanup] Collection '{agent_id}' is empty. Deleted successfully.")
            else:
                logger.info(f"🗑️ Deleted vectors for file {file_id}. Remaining docs: {remaining_count}")
                
            return True

        except Exception as e:
            logger.error(f"❌ Delete Operation Failed: {e}")
            return False
        
    def delete_collection(self, agent_id: str):
        try:
            self.client.delete_collection(name=agent_id)
            logger.info(f"🔥 Deleted collection {agent_id}")
            return True
        except: return False


_crm_chroma_service_v2 = None
def get_crm_chroma_service_v2():
    global _crm_chroma_service_v2
    if _crm_chroma_service_v2 is None:
        _crm_chroma_service_v2 = CRMChromaServiceV2()
    return _crm_chroma_service_v2