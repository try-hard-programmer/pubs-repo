import logging
import requests
import chromadb
from chromadb import Settings
from typing import List, Any, Dict, Tuple
from app.config import settings
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType
import os
from app.config import settings
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch

logger = logging.getLogger(__name__)

# ==========================================
# 1. EMBEDDING FUNCTION (Proxy Mode)
# ==========================================
class LocalProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # Proxy handles key, we just send a placeholder
        self.api_key = "proxy-managed"

    def _call_api(self, input: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        # [CHANGE] No model param, let Proxy default to text-embedding-3-small
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


# ==========================================
# 2. SERVICE CLASS (With Transformer Reranking)
# ==========================================
class CRMChromaServiceV2:
    def __init__(self):
        # [FIX] Wrap connection in try-except to prevent app crash on startup
        try:
            self.client = chromadb.HttpClient(
                host=settings.CHROMADB_HOST, 
                port=settings.CHROMADB_PORT,
                settings=Settings(allow_reset=True, anonymized_telemetry=False)
            )
            # Test connection immediately to catch errors early
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

            logger.info(f"🧠 Embedding {len(texts)} chunks for Agent {agent_id}...")
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

    async def query_context(self, query: str, agent_id: str, n_results: int = 50) -> str:
        """
        [OPTIMIZED V4] High-Accuracy RAG with "Unlimited Space" Regex
        """
        try:
            clean_query = query.strip()
            # 1. Skip meaningless queries
            if len(clean_query) < 5 or clean_query.lower() in ["test", "halo", "hi", "ping", "p", "hello"]:
                return ""

            import re
            def extract_codes(text: str) -> list:
                # [FIX] Allow unlimited spaces/hyphens between letters and numbers
                # Matches: "RC58", "RC 58", "RC   58", "RC-58", "RC - 58"
                pattern = r'\b([A-Za-z]+[\s\-_]*\d+[A-Za-z0-9]*)\b'
                raw_codes = re.findall(pattern, text)
                # Clean up: "RC - 58" -> "RC58"
                return [re.sub(r'[\s\-_]+', '', c).upper() for c in raw_codes]

            # 2. Detect codes
            query_codes = extract_codes(query)
            if query_codes:
                logger.info(f"🔢 Detected codes: {query_codes}")
            
            collection = self.get_or_create_collection(agent_id)
            
            # 3. Semantic Search (Fetch 50 to ensure target is in the pool)
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                include=['documents', 'distances', 'metadatas']
            )

            if not results["documents"] or not results["documents"][0]:
                return ""

            candidates = results['documents'][0]
            distances = results['distances'][0]
            
            logger.info(f"🔎 RAG Query: '{query}' → Retrieved {len(candidates)} candidates")

            # 4. Code Filter (Strict Logic)
            if query_codes:
                filtered_candidates = []
                for doc, dist in zip(candidates, distances):
                    doc_codes = extract_codes(doc)
                    # Check if ANY query code exists in the document
                    if any(qc in doc_codes for qc in query_codes):
                        filtered_candidates.append((doc, dist))
                
                # Update BOTH candidates AND distances
                if filtered_candidates:
                    candidates = [doc for doc, _ in filtered_candidates]
                    distances = [dist for _, dist in filtered_candidates]
                    logger.info(f"✂️ Code filter: {len(results['documents'][0])} → {len(candidates)} docs")
                else:
                    logger.warning("⚠️ No docs matched code filter, reverting to semantic matches.")

            # 5. Reranking (Try Neural, Fallback to Distance)
            final_docs = []
            
            # Try to load reranker
            model, tokenizer = self._get_reranker()
            
            if model and tokenizer and candidates:
                try:
                    # Prepare pairs for Cross-Encoder
                    pairs = [[query, doc] for doc in candidates]
                    
                    with torch.no_grad():
                        inputs = tokenizer(pairs, padding=True, truncation=True, return_tensors='pt', max_length=512)
                        
                        if next(model.parameters()).is_cuda:
                            inputs = {k: v.cuda() for k, v in inputs.items()}
                        
                        scores = model(**inputs).logits.squeeze(-1)
                        if scores.is_cuda: scores = scores.cpu()
                        scores = scores.tolist()

                    # Sort by Score (High is better)
                    scored_docs = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)
                    final_docs = [doc for doc, score in scored_docs[:5]]
                    
                    logger.info(f"🎯 Reranked Top Match: Score {scored_docs[0][1]:.4f}")

                except Exception as rerank_error:
                    logger.error(f"⚠️ Reranking failed, using distance: {rerank_error}")
                    final_docs = [doc for doc, dist in zip(candidates, distances) if dist < 1.4][:5]
            else:
                final_docs = [doc for doc, dist in zip(candidates, distances) if dist < 1.4][:5]

            if not final_docs:
                return ""
            
            return "\n\n###\n\n".join(final_docs)

        except Exception as e:
            logger.error(f"❌ Query failed: {e}", exc_info=True)
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