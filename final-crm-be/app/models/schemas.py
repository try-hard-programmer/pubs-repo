"""
Pydantic models for request/response validation
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel


class DeleteItem(BaseModel):
    """Schema for deleting documents"""
    filename: str


class Item(BaseModel):
    """Schema for querying documents"""
    query: str
    top_k: Optional[int] = 5
    where: Optional[Dict[str, Any]] = None
    include_distances: Optional[bool] = True
    include_embeddings: Optional[bool] = False
    top_n: Optional[int] = 3
    include: Optional[List[str]] = None


class ItemAgent(BaseModel):
    """Schema for agent queries (Deprecated - use AgentRequest in app.api.agents)"""
    query: str
