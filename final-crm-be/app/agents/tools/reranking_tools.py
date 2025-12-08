"""
Reranking Tools for Google ADK Agents

Provides tools for reranking retrieved documents using LLM-based scoring.
"""

import logging
import json
from typing import Optional, Dict, Any, List
from google.adk.tools.tool_context import ToolContext
from openai import OpenAI
from app.config import settings

logger = logging.getLogger(__name__)


def set_current_metadata(metadata: List[Dict]):
    """Store metadata in module-level variable for access by agent"""
    global _last_reranked_metadata
    _last_reranked_metadata = metadata


def get_current_metadata() -> List[Dict]:
    """Get last stored metadata"""
    global _last_reranked_metadata
    return _last_reranked_metadata if '_last_reranked_metadata' in globals() else []

# State keys
RESULT_KEY = "temp:result_chromadb"
RESULT_METAS_KEY = "temp:result_chromadb_metas"
RERANK_TOP_KEY = "temp:rerank_top"
RERANK_TOP_METAS_KEY = "temp:rerank_top_metas"


async def rerank_with_openai_after_get_context(
    tool_context: ToolContext,
    top_n: int = 5,  # ⬆️ Increased from 3 to 5
    model: str = "gpt-3.5-turbo",
    min_score: float = 0.3  # ⭐ NEW: Minimum score threshold (lowered for flexibility)
) -> Dict[str, Any]:
    """
    Tool for reranking documents after retrieval.

    This tool:
    1. Retrieves candidates from ChromaDB tool results
    2. Uses LLM to score each document's relevance
    3. Sorts by relevance score
    4. Returns top N documents (with scores above min_score)
    5. Stores reranked results in state

    Args:
        tool_context: ADK tool context with state access
        top_n: Number of top documents to return (default: 5)
        model: OpenAI model to use for scoring
        min_score: Minimum relevance score threshold (default: 0.3)

    Returns:
        Dict with reranked documents and metadata
    """
    query = tool_context.state.get("temp:last_query")
    candidates = tool_context.state.get(RESULT_KEY) or []
    metas = tool_context.state.get(RESULT_METAS_KEY) or []

    logger.info(f"🔄 Reranking {len(candidates)} candidates for query: '{query}'")

    if not candidates:
        logger.warning("⚠️  No candidates found for reranking")
        return {"documents": [], "metas": [], "count": 0, "message": "No candidates to rerank"}

    # Prepare scoring prompt (more lenient)
    system = (
        "You are a helpful document reranker. Score each passage based on its relevance to the query. "
        "Give HIGHER scores to passages that contain relevant information, even if partial. "
        "Use the full scale [0.0, 1.0]: "
        "- 0.8-1.0: Highly relevant and directly answers the query "
        "- 0.5-0.7: Somewhat relevant, contains related information "
        "- 0.3-0.4: Tangentially relevant, mentions related topics "
        "- 0.0-0.2: Not relevant at all "
        "Return only valid JSON."
    )

    passages = "\n\n".join([f"[{i}] {doc[:500]}" for i, doc in enumerate(candidates)])  # Limit to 500 chars per doc

    user = (
        f"Query: {query}\n\n"
        f"Passages:\n{passages}\n\n"
        "Instructions:\n"
        "- Be generous with scoring - if a passage has ANY relevant information, give it at least 0.3\n"
        '- Return ONLY a JSON object with key "items"\n'
        '- "items" is a JSON array: [{"index": int, "score": float}, ...]\n'
        'Example: {"items":[{"index":0,"score":0.82},{"index":1,"score":0.45}]}'
    )

    try:
        client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = resp.choices[0].message.content or ""

        # Parse scores
        try:
            obj = json.loads(raw)
            items = obj.get("items") if isinstance(obj, dict) else None

            if not isinstance(items, list):
                # Fallback to uniform scores (0.5 = moderate relevance)
                logger.warning("Reranking returned invalid format, using uniform scores")
                items = [
                    {"index": i, "score": 0.5}
                    for i in range(len(candidates))
                ]

            # Normalize types
            for it in items:
                it["index"] = int(it["index"])
                it["score"] = float(it["score"])

        except Exception as e:
            logger.error(f"Error parsing reranking scores: {type(e).__name__} – {e}")
            logger.error(f"Raw response: {raw[:200]}...")
            items = [
                {"index": i, "score": 0.5}
                for i in range(len(candidates))
            ]

        # Log all scores
        logger.info(f"📊 Reranking scores: {[(i['index'], round(i['score'], 2)) for i in items]}")

        # Sort by score (descending)
        items.sort(key=lambda x: x["score"], reverse=True)

        # Filter by min_score and get top N
        filtered_items = [item for item in items if item["score"] >= min_score]
        top = filtered_items[:min(top_n, len(filtered_items))]

        # Fallback: if no items pass min_score, take top items anyway (with warning)
        if not top and items:
            logger.warning(f"⚠️  No documents scored above {min_score}, using top {min(3, len(items))} anyway")
            top = items[:min(3, len(items))]

        top_idx = [t["index"] for t in top if 0 <= t["index"] < len(candidates)]

        # Get reranked documents
        reranked_docs = [candidates[i] for i in top_idx]
        reranked_metas = [metas[i] for i in top_idx] if metas else []

        # Store in state
        tool_context.state[RERANK_TOP_KEY] = reranked_docs
        tool_context.state[RERANK_TOP_METAS_KEY] = reranked_metas

        # ALSO store in module-level variable for easy access
        set_current_metadata(reranked_metas)

        logger.info(f"✅ Reranked {len(candidates)} → kept top {len(reranked_docs)} documents")
        if top:
            logger.info(f"📈 Top scores: {[round(t['score'], 2) for t in top]}")
        if reranked_metas:
            logger.info(f"📋 Top document: {reranked_metas[0].get('filename', 'unknown')}")

        return {
            "documents": reranked_docs,
            "metas": reranked_metas,
            "count": len(reranked_docs),
            "scores": [t["score"] for t in top],
            "filtered_count": len(filtered_items),
            "total_candidates": len(candidates)
        }

    except Exception as e:
        logger.error(f"❌ Error in reranking: {type(e).__name__} – {e}")
        logger.info(f"🔄 Fallback: returning first {top_n} documents without reranking")

        # Fallback: return first top_n documents (already sorted by ChromaDB distance)
        fallback_docs = candidates[:top_n]
        fallback_metas = metas[:top_n] if metas else []

        # Store in state
        tool_context.state[RERANK_TOP_KEY] = fallback_docs
        tool_context.state[RERANK_TOP_METAS_KEY] = fallback_metas

        # Store in module-level variable
        set_current_metadata(fallback_metas)

        logger.info(f"✅ Fallback: returned {len(fallback_docs)} documents")

        return {
            "documents": fallback_docs,
            "metas": fallback_metas,
            "count": len(fallback_docs),
            "error": str(e),
            "fallback": True,
            "total_candidates": len(candidates)
        }


def get_reranked_documents(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Helper function to get reranked documents from context.

    Args:
        tool_context: ADK tool context

    Returns:
        Dict with documents and metadata
    """
    return {
        "documents": tool_context.state.get(RERANK_TOP_KEY, []),
        "metas": tool_context.state.get(RERANK_TOP_METAS_KEY, [])
    }
