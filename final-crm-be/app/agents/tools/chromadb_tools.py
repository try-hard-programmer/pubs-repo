"""
ChromaDB Tools for Google ADK Agents

Provides tools for document retrieval and context management using ChromaDB.
Supports organization-specific collections for multi-tenant isolation.
"""

import logging
from typing import Optional, Dict, Any
from google.adk.tools.tool_context import ToolContext
from app.services import ChromaDBService

logger = logging.getLogger(__name__)

# State keys for storing results
RESULT_KEY = "temp:result_chromadb"
RESULT_METAS_KEY = "temp:result_chromadb_metas"


async def get_context_documents_ChromaDB(
    tool_context: ToolContext,
    where: Optional[Dict[str, Any]] = None,
    include_distances: Optional[bool] = True,
    include_embeddings: Optional[bool] = False
) -> Dict[str, Any]:
    """
    Tool for retrieving documents from organization-specific ChromaDB collection.

    This tool:
    1. Retrieves user email, organization_id, and query from session state
    2. Queries organization-specific ChromaDB collection for relevant documents
    3. Stores results in session state for later use
    4. Extracts unique file IDs

    Args:
        tool_context: ADK tool context with state access
        where: Optional ChromaDB filter conditions
        include_distances: Whether to include distance scores
        include_embeddings: Whether to include embeddings

    Returns:
        Dict with status and count of retrieved documents
    """
    email = tool_context.state.get("user:email")
    organization_id = tool_context.state.get("user:organization_id")
    query = tool_context.state.get("temp:last_query")

    logger.info(f"🔍 ChromaDB Tool Called - Query: '{query}', Email: {email}, OrgID: {organization_id}")

    if not email or not query:
        logger.error("Error: Email or query not found in context")
        return {"status": "error", "count": 0, "message": "Missing email or query"}

    if not organization_id:
        logger.error("Error: Organization ID not found in context")
        return {"status": "error", "count": 0, "message": "Missing organization_id"}

    chromadb_service = ChromaDBService()

    try:
        # ⭐ Query organization-specific collection
        # Increased top_k to retrieve more candidates for reranking
        results = chromadb_service.query_documents(
            query=query,
            organization_id=organization_id,  # Organization-specific collection
            email=email,
            top_k=15,  # ⬆️ Increased from 10 to 15 for better recall
            where=where,
            include_distances=include_distances,
            include_embeddings=include_embeddings
        )

        docs = results.get("documents", [[]])[0] or []
        metas = results.get("metadatas", [[]])[0] or []

        logger.info(f"📊 Retrieved {len(docs)} documents from ChromaDB")
        if docs:
            logger.info(f"📄 First document preview: {docs[0][:100]}...")
        if metas:
            logger.info(f"📋 First metadata: {metas[0]}")

        # Store in state for other tools to use
        tool_context.state[RESULT_KEY] = docs
        tool_context.state[RESULT_METAS_KEY] = metas

        # Extract unique file IDs and store
        file_ids = list(chromadb_service.extract_unique_file_ids(metas))
        tool_context.state["temp:file_ids"] = file_ids

        logger.info(f"Retrieved {len(docs)} documents from org_{organization_id}")
        logger.info(f"Found {len(file_ids)} unique files")

        tool_context.actions.skip_summarization = False

        # Warning if no documents found
        if len(docs) == 0:
            logger.warning(f"⚠️  No documents found for query: '{query}'")
            logger.warning(f"⚠️  This might indicate: empty collection, filter mismatch, or no relevant documents")
            return {
                "status": "no_results",
                "count": 0,
                "file_count": 0,
                "organization_id": organization_id,
                "message": "No documents found matching the query"
            }

        return {
            "status": "retrieved",
            "count": len(docs),
            "file_count": len(file_ids),
            "organization_id": organization_id,
            "message": f"Found {len(docs)} relevant documents"
        }

    except ValueError as e:
        logger.error(f"Organization collection not found: {e}")
        return {
            "status": "error",
            "count": 0,
            "message": "Organization collection not found. Please contact administrator."
        }

    except Exception as e:
        logger.error(f"Error in get_context_documents_ChromaDB: {type(e).__name__} – {e}")
        return {
            "status": "error",
            "count": 0,
            "message": str(e)
        }


def get_retrieved_file_ids(tool_context: ToolContext) -> list:
    """
    Helper function to get file IDs from context.

    Args:
        tool_context: ADK tool context

    Returns:
        List of file IDs
    """
    return tool_context.state.get("temp:file_ids", [])
