"""
Agent API Endpoints

Provides HTTP endpoints for interacting with Google ADK agents.
Supports both main orchestrator and direct agent access.
"""

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import logging

from app.services import get_agent_service
from app.services.chat_service import get_chat_service
from app.agents.tools import get_retrieved_file_ids
from app.auth.dependencies import get_current_user, get_optional_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agents"])


# Pydantic Schemas

class AgentRequest(BaseModel):
    """Request schema for agent queries"""
    query: str = Field(
        ...,
        description="User's question or query",
        example="What are the key findings in the Q1 2024 report?"
    )
    topic_id: Optional[str] = Field(
        None,
        description="Optional topic ID for chat history",
        example="topic-abc-123"
    )
    save_history: bool = Field(
        True,
        description="Whether to save this interaction to chat history"
    )
    session_id: Optional[str] = Field(
        None,
        description="Optional session ID for conversation continuity",
        example="session-xyz-789"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "query": "Summarize the revenue trends from uploaded financial reports",
                "topic_id": "quarterly-review-2024",
                "save_history": True,
                "session_id": None
            }
        }


class DocumentMetadata(BaseModel):
    """Document metadata schema"""
    file_id: Optional[str] = None
    filename: Optional[str] = None
    email: Optional[str] = None
    chunk_index: Optional[int] = None


class AgentResponse(BaseModel):
    """Response schema for agent queries"""
    email: str = Field(..., example="user@example.com")
    query: str = Field(..., example="What are the key findings in Q1 2024 report?")
    answer: str = Field(
        ...,
        example="Based on the Q1 2024 report, key findings include: 1) Revenue increased by 23%, 2) User growth of 45%, 3) Expansion into 3 new markets."
    )
    reference_documents: List[DocumentMetadata] = Field(
        default_factory=list,
        description="List of referenced document metadata"
    )
    agent_name: str = Field(..., example="rag_agent")
    session_id: Optional[str] = Field(None, example="session-xyz-789")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "query": "Summarize Q1 2024 financial report",
                "answer": "The Q1 2024 financial report shows strong growth with revenue up 23% YoY to $45M. Key drivers include increased user adoption (45% growth) and successful expansion into Southeast Asian markets.",
                "reference_documents": [
                    {
                        "file_id": "file-123-abc",
                        "filename": "Q1_2024_Financial_Report.pdf",
                        "email": "user@example.com",
                        "chunk_index": 5
                    }
                ],
                "agent_name": "rag_agent",
                "session_id": "session-xyz-789"
            }
        }


class AgentStatusResponse(BaseModel):
    """Response schema for agent status"""
    name: str
    registered: bool
    initialized: bool
    class_name: str
    tools_count: int


class AgentListResponse(BaseModel):
    """Response schema for listing agents"""
    agents: List[str]
    count: int

class AgentAnalysisRequest(BaseModel):
    """
    Request schema for agent analysis queries
    """
    email: str = Field(..., description="User's email address")
    query: str = Field(..., description="User's question or query")
    session_id: Optional[str] = Field(None, description="Optional session ID for conversation continuity")

class AgentFileResponse(BaseModel):
    """
    Response schema for agent actions that result in a downloadable file.
    """
    message: str = Field(..., description="A human-readable message indicating the outcome (e.g., 'Document created successfully').")
    session_id: Optional[str] = None


# Endpoints

@router.post("/", response_model=AgentResponse)
async def main_agent(
    request: AgentRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Main agent endpoint (orchestrator).

    Currently routes to RAG agent. Can be extended to:
    - Analyze query and route to appropriate specialized agent
    - Coordinate multiple agents for complex tasks
    - Handle multi-turn conversations with agent switching

    Args:
        request: Agent request with query
        current_user: Authenticated user from JWT token

    Returns:
        Agent response with answer
    """
    # For now, route directly to RAG agent
    # TODO: Add orchestration logic to choose appropriate agent
    return await run_rag_agent(request, current_user)


@router.post("/rag", response_model=AgentResponse)
async def run_rag_agent(
    request: AgentRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Direct access to RAG (Retrieval-Augmented Generation) agent.

    Answers questions based on documents in organization-specific ChromaDB collection.

    **Requirements:**
    - User must belong to an organization
    - Queries only search within user's organization documents

    Args:
        request: Agent request with query
        current_user: Authenticated user from JWT token

    Returns:
        Agent response with answer and document references

    Raises:
        400: If user has no organization or query is missing
        503: If agent service unavailable
    """
    logger.debug(f"RAG agent request from {current_user.email} (user_id: {current_user.user_id})")

    # Validate request
    if not request.query:
        raise HTTPException(
            status_code=400,
            detail="Query is required"
        )

    # ⭐ Get user's organization
    from app.services.organization_service import get_organization_service
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(current_user.user_id)

    if not user_org:
        raise HTTPException(
            status_code=400,
            detail="User must belong to an organization to use RAG agent"
        )

    organization_id = user_org.id
    logger.info(f"Running RAG agent for organization: {organization_id}")

    try:
        agent_service = get_agent_service()

        # Run RAG agent using user data from JWT token + organization_id
        result = await agent_service.run_agent(
            agent_name="rag_agent",
            user_id=current_user.user_id,
            query=request.query,
            email=current_user.email,
            organization_id=organization_id,  # ⭐ Organization-specific query
            session_id=request.session_id,
            topic_id=request.topic_id,
        )

        # Extract answer and metadata
        if isinstance(result, dict):
            answer = result.get("answer", "Maaf, tidak ada respons.")
            ref_docs_raw = result.get("reference_documents", [])
            result_session_id = result.get("session_id", request.session_id)

            # Convert to DocumentMetadata objects
            ref_docs = [
                DocumentMetadata(**doc) if isinstance(doc, dict) else doc
                for doc in ref_docs_raw
            ]
        else:
            answer = result
            ref_docs = []
            result_session_id = request.session_id

        logger.info(f"RAG agent completed for {current_user.email} with {len(ref_docs)} references")

        # Save to chat history if topic_id provided and save_history is True
        if request.topic_id and request.save_history:
            try:
                chat_service = get_chat_service()
                # Convert ref_docs to list of dicts for storage
                ref_docs_data = [
                    {
                        "file_id": doc.file_id,
                        "filename": doc.filename,
                        "email": doc.email,
                        "chunk_index": doc.chunk_index
                    }
                    for doc in ref_docs
                ]
                await chat_service.save_conversation(
                    topic_id=request.topic_id,
                    user_id=current_user.user_id,
                    user_message=request.query,
                    assistant_message=answer,
                    agent_name="rag_agent",
                    reference_documents=ref_docs_data
                )
                logger.info(f"Saved conversation to topic {request.topic_id}")
            except Exception as e:
                # Log error but don't fail the request
                logger.error(f"Failed to save chat history: {e}")

        return AgentResponse(
            email=current_user.email,
            query=request.query,
            answer=answer,
            reference_documents=ref_docs,
            agent_name="rag_agent",
            session_id=result_session_id
        )

    except ValueError as e:
        logger.error(f"Agent error: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    except Exception as e:
        logger.error(f"Unexpected error in RAG agent: {type(e).__name__} – {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )

@router.post("/agent_analysis", response_model=AgentFileResponse)
async def run_analysis_agent(request: AgentAnalysisRequest):
    if not request.email or not request.query:
        raise HTTPException(
            status_code=400,
            detail="Email and query are required"
        )

    try:
        agent_service = get_agent_service()

         # Run RAG agent
        result = await agent_service.run_agent_analyst(
            agent_name="analysis_agent",
            user_id=request.email,
            query=request.query,
            email=request.email,
            session_id=request.session_id
        )

        return AgentFileResponse(
            message=result["message"],
            session_id = result["session_id"]
        )

    except Exception as e:
        logger.error(f"Error in analysis: {e}")
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.post("/data-analyst", response_model=AgentResponse)
async def run_data_analyst_agent(
    request: AgentRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Direct access to Data Analyst agent.

    **Requirements:**
    - User must belong to an organization

    Args:
        request: Agent request with query
        current_user: Authenticated user from JWT token

    Returns:
        Agent response with answer

    Raises:
        400: If user has no organization
    """
    logger.info(f"Data Analyst agent request from {current_user.email}")

    # ⭐ Get user's organization for consistency
    from app.services.organization_service import get_organization_service
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(current_user.user_id)

    if not user_org:
        raise HTTPException(
            status_code=400,
            detail="User must belong to an organization to use Data Analyst agent"
        )

    organization_id = user_org.id
    logger.info(f"Running Data Analyst agent for organization: {organization_id}")

    try:
        agent_service = get_agent_service()

        # Run Data Analyst agent using user data from JWT token
        result = await agent_service.run_agent(
            agent_name="data_analyst_agent",
            user_id=current_user.user_id,
            query=request.query,
            email=current_user.email,
            organization_id=organization_id,  # For consistency
            session_id=request.session_id
        )

        # Extract answer and metadata
        if isinstance(result, dict):
            answer = result.get("answer", "Maaf, tidak ada respons.")
            ref_docs_raw = result.get("reference_documents", [])
            result_session_id = result.get("session_id", request.session_id)

            # Convert to DocumentMetadata objects
            ref_docs = [
                DocumentMetadata(**doc) if isinstance(doc, dict) else doc
                for doc in ref_docs_raw
            ]
        else:
            answer = result
            ref_docs = []
            result_session_id = request.session_id

        logger.info(f"Data Analyst agent completed for {current_user.email}")

        # Save to chat history if topic_id provided and save_history is True
        if request.topic_id and request.save_history:
            try:
                chat_service = get_chat_service()
                # Convert ref_docs to list of dicts for storage
                ref_docs_data = [
                    {
                        "file_id": doc.file_id,
                        "filename": doc.filename,
                        "email": doc.email,
                        "chunk_index": doc.chunk_index
                    }
                    for doc in ref_docs
                ]
                await chat_service.save_conversation(
                    topic_id=request.topic_id,
                    user_id=current_user.user_id,
                    user_message=request.query,
                    assistant_message=answer,
                    agent_name="data_analyst_agent",
                    reference_documents=ref_docs_data
                )
                logger.info(f"Saved conversation to topic {request.topic_id}")
            except Exception as e:
                # Log error but don't fail the request
                logger.error(f"Failed to save chat history: {e}")

        return AgentResponse(
            email=current_user.email,
            query=request.query,
            answer=answer,
            reference_documents=ref_docs,
            agent_name="data_analyst_agent",
            session_id=result_session_id
        )

    except ValueError as e:
        logger.error(f"Agent error: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    except Exception as e:
        logger.error(f"Unexpected error in Data Analyst agent: {type(e).__name__} – {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )

@router.get("/status/{agent_name}", response_model=AgentStatusResponse)
async def get_agent_status(agent_name: str):
    """
    Get status information about a specific agent.

    Args:
        agent_name: Name of the agent

    Returns:
        Agent status information
    """
    agent_service = get_agent_service()
    status = agent_service.get_agent_status(agent_name)

    if "error" in status:
        raise HTTPException(status_code=404, detail=status["error"])

    return AgentStatusResponse(
        name=status["name"],
        registered=status["registered"],
        initialized=status["initialized"],
        class_name=status["class"],
        tools_count=status["tools_count"]
    )


@router.get("/list", response_model=AgentListResponse)
async def list_agents(
    initialized_only: bool = Query(False, description="Show only initialized agents")
):
    """
    List all available agents.

    Args:
        initialized_only: If True, show only initialized agents

    Returns:
        List of agent names
    """
    agent_service = get_agent_service()

    if initialized_only:
        agents = agent_service.list_initialized_agents()
    else:
        agents = agent_service.list_available_agents()

    return AgentListResponse(
        agents=agents,
        count=len(agents)
    )


@router.get("/status", response_model=Dict[str, Any])
async def get_all_agent_status():
    """
    Get status information for all agents.

    Returns:
        Dict mapping agent names to their status
    """
    agent_service = get_agent_service()
    return agent_service.get_all_agent_status()


# Health check endpoint
@router.get("/health")
async def agent_health():
    """
    Health check for agent service.

    Returns:
        Service health status
    """
    agent_service = get_agent_service()

    return {
        "status": "healthy" if agent_service.is_initialized() else "initializing",
        "initialized": agent_service.is_initialized(),
        "available_agents": agent_service.list_available_agents(),
        "initialized_agents": agent_service.list_initialized_agents()
    }
