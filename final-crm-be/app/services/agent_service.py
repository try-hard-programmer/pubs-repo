"""
Agent Service for Managing Google ADK Agents

Provides centralized service layer for agent lifecycle management,
initialization, and execution.
"""

import logging
from typing import Optional, Dict, Any, List, TYPE_CHECKING
from app.agents import AgentRegistry
from app.agents.analysis_agent import AnalysisAgent

if TYPE_CHECKING:
    from app.agents.rag_agent import RAGAgent

logger = logging.getLogger(__name__)


class AgentService:
    """
    Service for managing all agents in the application.

    Features:
    - Centralized agent registration
    - Lifecycle management (initialization, shutdown)
    - Agent execution
    - Agent metadata and status
    """

    def __init__(self):
        """Initialize agent service"""
        self._initialized = False

    async def initialize_agents(self) -> None:
        """
        Initialize all agents during application startup.

        This method:
        1. Registers all available agents
        2. Initializes all registered agents
        3. Makes agents ready for use

        Should be called once during FastAPI lifespan startup.
        """
        if self._initialized:
            logger.warning("Agents already initialized")
            return

        logger.info("Starting agent initialization...")

        # Register all agents
        self._register_agents()

        # Initialize all registered agents
        await AgentRegistry.initialize_all()

        self._initialized = True
        logger.info("Agent service initialization complete")
        
    def _register_agents(self) -> None:
            """Register all available agents."""
            from app.agents.rag_agent import RAGAgent
            from app.agents.analysis_agent import AnalysisAgent
            # [NEW] Import Ticket Guard
            from app.agents.ticket_guard_agent import TicketGuardAgent 

            AgentRegistry.register(name="rag_agent", agent_class=RAGAgent)
            AgentRegistry.register(name="analysis_agent", agent_class=AnalysisAgent)
            
            # [NEW] Register Ticket Guard
            AgentRegistry.register(name="ticket_guard_agent", agent_class=TicketGuardAgent)

            logger.info(f"Registered {len(AgentRegistry.list_agents())} agents")
    
    async def run_agent(
        self,
        agent_name: str,
        user_id: str,
        query: str,
        **kwargs
    ) -> Any:
        """
        Run a specific agent with a query.

        Args:
            agent_name: Name of the agent to run
            user_id: User identifier
            query: User's query
            **kwargs: Additional arguments passed to agent.run()

        Returns:
            Agent's response

        Raises:
            ValueError: If agent not found or not initialized
        """
        agent = AgentRegistry.get_or_create(agent_name)

        if agent is None:
            available = AgentRegistry.list_agents()
            raise ValueError(
                f"Agent '{agent_name}' not found. "
                f"Available agents: {available}"
            )

        if not agent.is_initialized():
            await agent.initialize()
            raise ValueError(
                f"Agent '{agent_name}' not initialized. "
                "Call initialize_agents() first."
            )

        logger.info(f"Running agent '{agent_name}' for user '{user_id}'")

        result = await agent.run(
            user_id=user_id,
            query=query,
            **kwargs
        )

        logger.info(f"Agent '{agent_name}' completed execution")

        return result

    async def run_agent_analyst(
        self,
        agent_name: str,
        user_id: str,
        query: str,
        email:str,
        **kwargs
    ) -> Any:
        """
        Run a specific agent with a query.

        Args:
            agent_name: Name of the agent to run
            user_id: User identifier
            query: User's query
            **kwargs: Additional arguments passed to agent.run()

        Returns:
            Agent's response

        Raises:
            ValueError: If agent not found or not initialized
        """
        agent = AgentRegistry.get_or_create(agent_name)

        if agent is None:
            available = AgentRegistry.list_agents()
            raise ValueError(
                f"Agent '{agent_name}' not found. "
                f"Available agents: {available}"
            )

        if not agent.is_initialized():
            await agent.initialize()
            raise ValueError(
                f"Agent '{agent_name}' not initialized. "
                "Call initialize_agents() first."
            )

        logger.info(f"Running agent '{agent_name}' for user '{user_id}'")

        result = await agent.run(
            user_id=user_id,
            query=query,
            email=email,
            **kwargs
        )

        logger.info(f"Agent '{agent_name}' completed execution")

        return result

    def get_agent_status(self, agent_name: str) -> Dict[str, Any]:
        """
        Get status information about a specific agent.

        Args:
            agent_name: Name of the agent

        Returns:
            Dict with agent status information
        """
        return AgentRegistry.get_agent_info(agent_name)

    def list_available_agents(self) -> List[str]:
        """
        List all available agent names.

        Returns:
            List of agent names
        """
        return AgentRegistry.list_agents()

    def list_initialized_agents(self) -> List[str]:
        """
        List names of initialized agents.

        Returns:
            List of initialized agent names
        """
        return AgentRegistry.list_initialized_agents()

    def get_all_agent_status(self) -> Dict[str, Dict]:
        """
        Get status information for all agents.

        Returns:
            Dict mapping agent names to their status info
        """
        return AgentRegistry.get_all_agent_info()

    def is_initialized(self) -> bool:
        """
        Check if service is initialized.

        Returns:
            bool: True if initialized
        """
        return self._initialized


# Global singleton instance
_agent_service: Optional[AgentService] = None


def get_agent_service() -> AgentService:
    """
    Get the global agent service instance.

    Returns:
        AgentService: Singleton instance
    """
    global _agent_service
    if _agent_service is None:
        _agent_service = AgentService()
    return _agent_service
