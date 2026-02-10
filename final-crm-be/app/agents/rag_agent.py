import aiohttp
from google.adk.agents import LlmAgent
from app.agents.tools.tools_rag import rerank_with_proxy, retrieve_chromadb_documents
from app.config import settings
from app.services.chat_service import get_chat_service
from .base_agent import BaseAgent

class RAGAgent(BaseAgent):
    """
    RAG Agent for document-based question answering.

    Features:
    - Retrieves relevant documents from ChromaDB
    - Reranks documents using LLM scoring
    - Generates answers based only on retrieved documents
    - Multilingual support (detects and responds in same language)
    - Strict adherence to source documents (no hallucination)

    Workflow:
    1. Detect user's language
    2. Retrieve relevant documents from ChromaDB
    3. Rerank documents by relevance
    4. Generate answer from top documents only
    5. Respond in same language as user's question
    """
    def __init__(self):
        super().__init__()
        base = settings.PROXY_BASE_URL.rstrip("/")
        self.proxy_url = f"{base}/chat"
        self.http_timeout = aiohttp.ClientTimeout(total=120)
    def get_agent_name(self) -> str:
        """Get unique agent name"""
        return "rag_agent"

    def create_agent(self) -> LlmAgent:
        """
        Create and configure the RAG agent.

        Returns:
            LlmAgent configured for RAG tasks
        """
        raise NotImplementedError("ADK agent creation disabled in proxy-based RAGAgent")

    async def initialize(self) -> None:
        self._initialized = True

    async def initialize(self) -> None:
        if self._initialized:
            return

        base = (settings.PROXY_BASE_URL or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("PROXY_BASE_URL is not set")

        self.proxy_url = f"{base}/chat"
        self.http_timeout = aiohttp.ClientTimeout(total=120)
        self._initialized = True

    async def run(
        self,
        user_id: str,
        query: str,
        email: str = None,
        organization_id: str = None,
        session_state: dict = None,
        session_id: str = None,
        topic_id: str = None, 
        history_limit: int = 8
    ) -> dict:
        """
        Run RAG agent with query.

        Args:
            user_id: User identifier
            query: User's question
            email: User's email (for document filtering)
            organization_id: Organization UUID (REQUIRED for multi-tenant isolation)
            session_state: Optional initial state
            session_id: Optional existing session

        Returns:
            Dict with answer, file references, and metadata

        Raises:
            ValueError: If organization_id is not provided
        """
        if not organization_id:
            raise ValueError("organization_id is required for RAG agent")
        
        history_messages = []
        if topic_id:
            chat_service = get_chat_service()
            rows = await chat_service.get_messages(topic_id,user_id)
            rows = rows[-history_limit:] if rows else []
            for m in rows:
                role = getattr(m, "role", None) or m.get("role")
                content = getattr(m, "content", None) or m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    history_messages.append({"role": role, "content": content})

        # 1) Retrieve candidates dari ChromaDB 
        r = await retrieve_chromadb_documents(query, email, organization_id, top_k=15)
        candidates, metas = r["documents"], r["metas"]

        # 2) Rerank candidates
        rr = await rerank_with_proxy(query, candidates, metas, organization_id, top_n=5, min_score=0.3)
        reranked_docs = rr.get("documents", [])
        reranked_metas = rr.get("metas", [])
        scores = rr.get("scores", [])
        print("RERANK      ", rr)


        top_score = scores[0] if scores else 0.0

        # 3) Jika query terlalu pendek, tidak tampilkan referensi
        q = (query or "").strip().lower()
        is_smalltalk = q in {"hi", "hello", "hallo", "halo", "hai", "tes", "test"} or len(q) < 4

        # 4) Jika skor evidence rendah, anggap tidak relevan -> tidak tampilkan referensi
        CITATION_THRESHOLD = 0.60
        has_strong_evidence = top_score >= CITATION_THRESHOLD

        if is_smalltalk or not has_strong_evidence:
            reranked_docs = []
            reranked_metas = []

        # build context
        rag_context = "\n\n".join(
            [f"Source Knowlage [{i}]: {doc}" for i, doc in enumerate(reranked_docs)]
        )

        reference_documents = list({
            m["file_id"]: {
                "file_id": m["file_id"],
                "filename": m.get("filename"),
                "email": m.get("email"),
                "chunk_index": m.get("chunk_index"),
            }
            for m in reranked_metas
            if isinstance(m, dict) and m.get("file_id")
        }.values())

        system_prompt = (
            "You are an AI Assistant using Retrieval-Augmented Generation (RAG). "
            "Your goal is to answer questions based on documents retrieved from the knowledge base. "
            "\n\n"
            "**Answer Guidelines:**\n"
            "- If documents contain relevant information: Provide a clear, helpful answer based on the documents\n"
            "- If documents contain partial information: Answer what you can and mention what's available\n"
            "- If documents are completely irrelevant to the question: Say you couldn't find specific information\n"
            "- Be flexible and helpful - if the documents have ANY related information, use it\n"
            "- Respond naturally and professionally\n"
            "\n"
            "**Language Detection:**\n"
            "- Indonesian question → Indonesian answer\n"
            "- English question → English answer\n"
            "\n"
            "**Insufficient Data Response (ONLY when NO documents retrieved or ALL documents completely irrelevant):**\n"
            "- English: 'I apologize, but I could not find relevant information in the documents to answer your question.'\n"
            "- Indonesian: 'Maaf, saya tidak menemukan informasi yang relevan dalam dokumen untuk menjawab pertanyaan Anda.'\n"
            "\n"
            "**Important:**\n"
            "- ALWAYS use the tools to retrieve documents first\n"
            "- Be helpful and try to answer based on available information\n"
            "- Don't be overly strict - if documents have useful information, share it\n"
            "- Keep answers clear and professional.\n\n"
            f"## KNOWLEDGE BASE\n{rag_context}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history_messages)  
        messages.append({"role": "user", "content": query})


        payload = {
            "messages": messages,
            "files": [],
            "temperature": 0.2,
            "organization_id": organization_id,
        }

        async with aiohttp.ClientSession(timeout=self.http_timeout) as session:
            async with session.post(self.proxy_url, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"Proxy error {resp.status}: {err}")
                result = await resp.json()

        answer = (
            result.get("choices", [{}])[0]
                  .get("message", {})
                  .get("content", "")
        ) or result.get("reply") or result.get("content") or "Maaf, tidak ada respons."

        # 5) Session_id: karena bukan ADK session, kamu bisa:
        final_session_id = session_id or "proxy-session"

        return {
            "answer": answer,
            "email": email or user_id,
            "query": query,
            "reference_documents": reference_documents,
            "session_id": final_session_id,
            "organization_id": organization_id
        }
