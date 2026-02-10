from typing import Optional, Dict, Any, List
from app.services.chromadb_service import ChromaDBService 
import aiohttp, json
from app.config import settings

RESULT_KEY = "temp:result_chromadb"
RESULT_METAS_KEY = "temp:result_chromadb_metas"

async def retrieve_chromadb_documents(
    query: str,
    email: str,
    organization_id: str,
    top_k: int = 15,
    where: Optional[Dict[str, Any]] = None,
    include_distances: bool = True,
    include_embeddings: bool = False
) -> Dict[str, Any]:
    chromadb_service = ChromaDBService()
    results = chromadb_service.query_documents(
        query=query,
        organization_id=organization_id,
        email=email,
        top_k=top_k,
        where=where,
        include_distances=include_distances,
        include_embeddings=include_embeddings
    )
    docs = results.get("documents", [[]])[0] or []
    metas = results.get("metadatas", [[]])[0] or []
    return {
        "documents": docs,
        "metas": metas,
        "count": len(docs),
        "file_ids": list(chromadb_service.extract_unique_file_ids(metas)),
        "organization_id": organization_id
    }

async def rerank_with_proxy(
    query: str,
    candidates: List[str],
    metas: List[Dict[str, Any]],
    organization_id: str,
    top_n: int = 5,
    min_score: float = 0.3
) -> Dict[str, Any]:
    if not candidates:
        return {"documents": [], "metas": [], "scores": [], "count": 0}

    # batasi panjang per passage
    passages = "\n\n".join([f"[{i}] {doc[:500]}" for i, doc in enumerate(candidates)])

    system = (
        "You are a document reranker. Score each passage relevance to the query. "
        "Return ONLY valid JSON: {\"items\":[{\"index\":int,\"score\":float}, ...]} "
        "Scores 0.0-1.0."
    )
    user = f"Query: {query}\n\nPassages:\n{passages}"

    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "organization_id": organization_id
    }

    base = settings.PROXY_BASE_URL.rstrip("/")
    proxy_url = f"{base}/chat"

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(proxy_url, json=payload) as resp:
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"Proxy rerank error {resp.status}: {txt}")
            res = await resp.json()

    content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        obj = json.loads(content)
        items = obj.get("items", [])
        items = [{"index": int(it["index"]), "score": float(it["score"])} for it in items]
    except Exception:
        items = [{"index": i, "score": 0.5} for i in range(len(candidates))]

    items.sort(key=lambda x: x["score"], reverse=True)
    filtered = [it for it in items if it["score"] >= min_score]
    top = filtered[:min(top_n, len(filtered))] or items[:min(3, len(items))]

    top_idx = [it["index"] for it in top if 0 <= it["index"] < len(candidates)]
    reranked_docs = [candidates[i] for i in top_idx]
    reranked_metas = []
    if metas:
        for i in top_idx:
            m = (metas[i] or {}).copy()
            m["size"] = len(candidates[i])
            reranked_metas.append(m)

    return {
        "documents": reranked_docs,
        "metas": reranked_metas,
        "scores": [it["score"] for it in top if it["index"] in top_idx],
        "count": len(reranked_docs),
        "total_candidates": len(candidates)
    }
