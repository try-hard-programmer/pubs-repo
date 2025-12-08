"""
RAG (Retrieval-Augmented Generation) Agent

Specialized agent for question answering using document retrieval from ChromaDB.
Combines semantic search with LLM-based reranking for accurate responses.
"""

import logging
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from .base_agent import BaseAgent
from .tools import (
    get_context_documents_ChromaDB,
    rerank_with_openai_after_get_context,
    get_current_metadata
)

logger = logging.getLogger(__name__)


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

    def get_agent_name(self) -> str:
        """Get unique agent name"""
        return "rag_agent"

    def create_agent(self) -> LlmAgent:
        """
        Create and configure the RAG agent.

        Returns:
            LlmAgent configured for RAG tasks
        """
        instruction = (
            "You are an AI Assistant using Retrieval-Augmented Generation (RAG). "
            "Your goal is to answer questions based on documents retrieved from the knowledge base. "
            "\n\n"
            "**Workflow:**\n"
            "1. ALWAYS call `get_context_documents_ChromaDB` tool first to retrieve relevant documents\n"
            "2. ALWAYS call `rerank_with_openai_after_get_context` tool to get the most relevant documents\n"
            "3. Carefully read the retrieved documents\n"
            "4. Answer the user's question based on the information found\n"
            "5. Respond in the SAME LANGUAGE as the user's question\n"
            "\n"
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
            "- Keep answers clear and professional"
        )

        agent = LlmAgent(
            name=self.get_agent_name(),
            model=LiteLlm(model="openai/gpt-3.5-turbo"),
            instruction=instruction,
            output_key="final_text",
            tools=[
                get_context_documents_ChromaDB,
                rerank_with_openai_after_get_context
            ]
        )

        logger.info(f"Created {self.get_agent_name()} with 2 tools")

        return agent

    async def run(
        self,
        user_id: str,
        query: str,
        email: str = None,
        organization_id: str = None,
        session_state: dict = None,
        session_id: str = None
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

        # Prepare session state
        state = session_state or {}
        state["user:email"] = email or user_id
        state["user:organization_id"] = organization_id  # ⭐ Add organization_id to state
        state["temp:last_query"] = query

        logger.info(f"Running RAG agent for org_{organization_id}")

        # Run base agent
        answer, final_session_id = await super().run(
            user_id=user_id,
            query=query,
            session_state=state,
            session_id=session_id
        )

        # Get metadata from module-level storage (set by reranking tool)
        reference_documents = []
        reranked_metas = get_current_metadata()

        logger.info(f"Found {len(reranked_metas)} reranked metadata entries from storage")

        if reranked_metas:
            logger.debug(f"Sample metadata: {reranked_metas[0]}")

            # Extract metadata into list format
            for meta in reranked_metas:
                if isinstance(meta, dict):
                    reference_documents.append({
                        "file_id": meta.get("file_id"),
                        "filename": meta.get("filename"),
                        "email": meta.get("email"),
                        "chunk_index": meta.get("chunk_index")
                    })

            logger.info(f"Extracted {len(reference_documents)} reference documents")

        return {
            "answer": answer,
            "email": email or user_id,
            "query": query,
            "reference_documents": reference_documents,
            "session_id": final_session_id,
            "organization_id": organization_id
        }
