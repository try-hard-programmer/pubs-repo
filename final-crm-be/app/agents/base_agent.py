"""
Base Agent Class for Google ADK Agents

Provides common functionality and abstractions for all agents in the system.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Abstract base class for all Google ADK agents.

    Provides common functionality for:
    - Agent lifecycle management
    - Session management
    - Message execution
    - State management

    Subclasses must implement:
    - create_agent(): Define agent configuration
    - get_agent_name(): Return unique agent name
    """

    def __init__(self):
        """Initialize base agent components"""
        self.agent: Optional[LlmAgent] = None
        self.session_service: InMemorySessionService = InMemorySessionService()
        self.runner: Optional[Runner] = None
        self._initialized: bool = False

    @abstractmethod
    def create_agent(self) -> LlmAgent:
        """
        Create and configure the agent.

        Must be implemented by subclasses to define:
        - Agent name
        - Model configuration
        - Instructions
        - Tools
        - Output configuration

        Returns:
            LlmAgent: Configured agent instance
        """
        pass

    @abstractmethod
    def get_agent_name(self) -> str:
        """
        Get unique agent name.

        Returns:
            str: Unique identifier for this agent
        """
        pass

    async def initialize(self) -> None:
        """
        Initialize agent, session service, and runner.

        Should be called once during application startup or
        before first use of the agent.
        """
        if self._initialized:
            logger.warning(f"Agent {self.get_agent_name()} already initialized")
            return

        try:
            # Create agent instance
            self.agent = self.create_agent()

            # Initialize runner
            self.runner = Runner(
                agent=self.agent,
                session_service=self.session_service,
                app_name="app"
            )

            self._initialized = True
            logger.info(f"Agent {self.get_agent_name()} initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize agent {self.get_agent_name()}: {e}")
            raise

    async def run(
        self,
        user_id: str,
        query: str,
        session_state: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None
    ) -> tuple[str, str]:
        """
        Run the agent with a user query.

        Args:
            user_id: Unique user identifier
            query: User's query text
            session_state: Optional initial session state
            session_id: Optional existing session ID (creates new if None)

        Returns:
            tuple: (response_text, session_id)

        Raises:
            RuntimeError: If agent not initialized
            Exception: If execution fails
        """
        if not self._initialized:
            raise RuntimeError(
                f"Agent {self.get_agent_name()} not initialized. "
                "Call initialize() first."
            )

        try:
            # Create or get session
            if session_id is None:
                session = await self.session_service.create_session(
                    app_name="app",
                    user_id=user_id,
                    state=session_state or {}
                )
                session_id = session.id

            # Create user message
            user_msg = types.Content(
                role="user",
                parts=[types.Part(text=query)]
            )

            # Run agent and collect response
            final_text = None

            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_msg
            ):
                try:
                    if hasattr(event, "is_final_response") and event.is_final_response():
                        if (
                            getattr(event, "content", None) and
                            getattr(event.content, "parts", None)
                        ):
                            parts = event.content.parts
                            if parts and getattr(parts[0], "text", None):
                                final_text = parts[0].text
                                break
                except Exception as e:
                    logger.error(
                        f"Error processing event in {self.get_agent_name()}: {e}"
                    )
                    continue

            return final_text or "Maaf, tidak ada respons.", session_id

        except Exception as e:
            logger.error(f"Error running agent {self.get_agent_name()}: {e}")
            raise

    async def get_session_state(
        self,
        user_id: str,
        session_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get session state for a user session.

        Args:
            user_id: User identifier
            session_id: Session ID

        Returns:
            Dict with session state or None if not found
        """
        try:
            session = await self.session_service.get_session(
                app_name="app",
                user_id=user_id,
                session_id=session_id
            )
            return session.state if session else None
        except Exception as e:
            logger.error(f"Error getting session state: {e}")
            return None

    async def create_session(
        self,
        user_id: str,
        initial_state: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Create a new session for a user.

        Args:
            user_id: Unique user identifier
            initial_state: Optional initial session state

        Returns:
            str: Session ID
        """
        session = await self.session_service.create_session(
            app_name="app",
            user_id=user_id,
            state=initial_state or {}
        )
        return session.id

    def is_initialized(self) -> bool:
        """Check if agent is initialized"""
        return self._initialized

    def get_tools(self) -> List[Any]:
        """
        Get list of tools used by this agent.

        Returns:
            List of tool functions
        """
        if self.agent:
            return self.agent.tools or []
        return []
