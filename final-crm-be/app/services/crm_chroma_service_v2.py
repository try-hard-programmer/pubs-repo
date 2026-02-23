import logging
import requests
import chromadb
import torch
import os
import warnings

from chromadb import Settings
from typing import List, Any, Dict, Tuple
from app.config import settings
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from langchain_chroma import Chroma

from langchain.retrievers import EnsembleRetriever
from langchain_core.embeddings import Embeddings

warnings.filterwarnings("ignore", message=".*copying from a non-meta parameter.*")
warnings.filterwarnings("ignore", message=".*max_size.*parameter is deprecated.*")
logger = logging.getLogger(__name__)

class LocalProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = "proxy-managed"

    def _call_api(self, input: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        payload = { "input": input }
        headers = { "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}" }

        try:
            response = requests.post(self.base_url, json=payload, headers=headers, timeout=120)
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
                
                # 5. Device Setup Force to use cpu       
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

    def preload_pdf_models(self):
        """Download and cache layout + table models to local volume"""
        import os
        
        layout_path = settings.LAYOUT_MODEL_PATH
        table_path = settings.TABLE_MODEL_PATH
        
        # Force HF to use our paths
        os.environ["HF_HOME"] = layout_path
        os.environ["HUGGINGFACE_HUB_CACHE"] = layout_path
        os.environ["TORCH_HOME"] = table_path
        
        # 1. Layout model - Look at this change right here
        if os.path.exists(layout_path) and len(os.listdir(layout_path)) > 0:
            logger.info(f"🔄 Loading layout model from cache ({layout_path})...")
        else:
            logger.info(f"🔄 Downloading layout model to {layout_path}...")
            os.makedirs(layout_path, exist_ok=True)
        
        try:
            from unstructured_inference.models.base import get_model
            get_model("yolox")
            logger.info(f"✅ Layout model ready ({layout_path})")
        except Exception as e:
            logger.warning(f"⚠️ Layout model failed: {e}")
        
        # 2. Table model - Look at this change right here
        if os.path.exists(table_path) and len(os.listdir(table_path)) > 0:
            logger.info(f"🔄 Loading table model from cache ({table_path})...")
        else:
            logger.info(f"🔄 Downloading table model to {table_path}...")
            os.makedirs(table_path, exist_ok=True)
        
        try:
            from unstructured_inference.models.tables import UnstructuredTableTransformerModel
            table_model = UnstructuredTableTransformerModel()
            table_model.initialize("microsoft/table-transformer-structure-recognition")
            logger.info(f"✅ Table model ready ({table_path})")
        except Exception as e:
            logger.warning(f"⚠️ Table model failed: {e}")
            
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
        Triple-Layer Hybrid RAG + Context Healing
        Layer 1: BM25 (Keywords) - Crucial for short queries like "01"
        Layer 2: Vector Search (Semantic)
        Layer 3: Reranker (Sorting ONLY, No Filtering)
        Layer 4: Context Healing (Fetch neighbor chunks)
        """
        try:
            clean_query = query.strip()
            
            # Skip only if empty
            if not clean_query: return ""
            
            collection = self.get_or_create_collection(agent_id)
            
            # --- LAYERS 1 & 2 (Retrieval) ---
            all_docs_data = collection.get(include=['documents', 'metadatas'])
            if not all_docs_data['documents']: return ""
            
            documents = [
                Document(page_content=doc, metadata=meta or {})
                for doc, meta in zip(all_docs_data['documents'], all_docs_data['metadatas'])
            ]
            
            # Increase candidate pool to catch weak keyword matches
            bm25_retriever = BM25Retriever.from_documents(documents)
            bm25_retriever.k = 100
            
            vectorstore = Chroma(
                client=self.client,
                collection_name=agent_id,
                embedding_function=LangChainProxyEmbedding(self.embedding_fn)  
            )
            vector_retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 100})
            
            try:
                # Weighted Ensemble: Boost BM25 (0.5) because "01" is a keyword, not a semantic concept
                ensemble = EnsembleRetriever(retrievers=[bm25_retriever, vector_retriever], weights=[0.5, 0.5])
                hybrid_results = ensemble.invoke(clean_query)
            except Exception:
                hybrid_results = bm25_retriever.invoke(clean_query)

            if not hybrid_results: return ""
            
            # --- LAYER 3 (Reranking - SORT ONLY) ---
            final_docs = []
            model, tokenizer = self._get_reranker()
            
            if model and tokenizer:
                try:
                    candidates = [doc.page_content for doc in hybrid_results[:50]]
                    pairs = [[clean_query, doc] for doc in candidates]
                    
                    all_scores = []
                    batch_size = 16
                    for i in range(0, len(pairs), batch_size):
                        batch = pairs[i:i + batch_size]
                        with torch.no_grad():
                            inputs = tokenizer(batch, padding=True, truncation=True, return_tensors='pt', max_length=512)
                            logits = model(**inputs).logits.squeeze(-1)
                            probs = torch.sigmoid(logits)
                            if probs.is_cuda: probs = probs.cpu()
                            all_scores.extend(probs.tolist())

                    scored_results = sorted(zip(hybrid_results[:50], all_scores), key=lambda x: x[1], reverse=True)
                    
                    # LOGGING
                    for i, (doc, score) in enumerate(scored_results[:3]):
                        logger.info(f"   #{i+1} | {score:.6f} | {doc.metadata.get('section_title', 'No Title')}")

                    # [CRITICAL FIX] REMOVED THRESHOLD GATE.
                    # We take the Top N results regardless of how low the score is.
                    # Logic: If BM25 found it, it's relevant enough to show the LLM.
                    final_docs = [doc for doc, s in scored_results]
                    final_docs = final_docs[:n_results]

                except Exception as e:
                    logger.error(f"⚠️ Rerank failed: {e}")
                    final_docs = hybrid_results[:n_results]
            else:
                final_docs = hybrid_results[:n_results]

            if not final_docs: return ""

            # --- LAYER 4: CONTEXT HEALING ---
            healed_docs_map = {} 
            
            for doc in final_docs:
                meta = doc.metadata
                doc_id = meta.get('doc_id') or meta.get('file_id')
                current_idx = meta.get('chunk_index')
                
                # 1. Add Original
                key = f"{doc_id}_{current_idx}"
                if key not in healed_docs_map:
                    healed_docs_map[key] = doc

                # 2. Add Neighbor (Solution)
                if doc_id is not None and current_idx is not None:
                    next_idx = int(current_idx) + 1
                    next_key = f"{doc_id}_{next_idx}"
                    
                    if next_key not in healed_docs_map:
                        neighbor = collection.get(
                            where={
                                "$and": [
                                    {"doc_id": {"$eq": doc_id}},
                                    {"chunk_index": {"$eq": next_idx}}
                                ]
                            }
                        )
                        if neighbor and neighbor['documents']:
                            neighbor_doc = Document(
                                page_content=neighbor['documents'][0],
                                metadata=neighbor['metadatas'][0]
                            )
                            healed_docs_map[next_key] = neighbor_doc

            sorted_docs = sorted(
                healed_docs_map.values(), 
                key=lambda d: (d.metadata.get('doc_id', ''), d.metadata.get('chunk_index', 0))
            )

            formatted = []
            for doc in sorted_docs:
                content = doc.page_content.strip()
                meta = doc.metadata or {}
                header = f"Source: {meta.get('filename', 'Unknown')}"
                if 'section_title' in meta and meta['section_title']:
                    header += f" | {meta['section_title']}"
                formatted.append(f"[{header}]\n{content}")
            
            return "\n\n###\n\n".join(formatted)

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