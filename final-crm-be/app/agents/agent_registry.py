"""
Agent Registry for Managing Multiple Google ADK Agents

Provides a centralized registry for all agents in the system,
enabling dynamic agent lookup and management.
"""

import logging
from typing import Dict, Type, Optional, List
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    Singleton registry for managing all agents in the system.

    Features:
    - Register agents by name
    - Retrieve agent instances
    - List available agents
    - Initialize all agents
    - Type-safe agent management
    """

    _agents: Dict[str, BaseAgent] = {}
    _agent_classes: Dict[str, Type[BaseAgent]] = {}
    _initialized: bool = False

    @classmethod
    def register(
        cls,
        name: str,
        agent_class: Type[BaseAgent],
        auto_initialize: bool = False
    ) -> None:
        """
        Register an agent class in the registry.

        Args:
            name: Unique agent identifier
            agent_class: Agent class (must inherit from BaseAgent)
            auto_initialize: Whether to initialize immediately

        Raises:
            ValueError: If name already registered or invalid agent class
        """
        if name in cls._agent_classes:
            logger.warning(f"Agent '{name}' already registered. Overwriting...")

        if not issubclass(agent_class, BaseAgent):
            raise ValueError(
                f"Agent class must inherit from BaseAgent, got {agent_class}"
            )

        cls._agent_classes[name] = agent_class
        logger.info(f"Registered agent class: {name}")

        if auto_initialize:
            cls._initialize_agent(name)

    @classmethod
    def _initialize_agent(cls, name: str) -> BaseAgent:
        """
        Initialize a single agent instance.

        Args:
            name: Agent name

        Returns:
            BaseAgent: Initialized agent instance

        Raises:
            KeyError: If agent not registered
        """
        if name not in cls._agent_classes:
            raise KeyError(f"Agent '{name}' not registered")

        if name in cls._agents:
            return cls._agents[name]

        agent_class = cls._agent_classes[name]
        agent_instance = agent_class()

        cls._agents[name] = agent_instance
        logger.info(f"Created agent instance: {name}")

        return agent_instance

    @classmethod
    async def initialize_all(cls) -> None:
        """
        Initialize all registered agents.

        This should be called during application startup.
        """
        if cls._initialized:
            logger.warning("Agents already initialized")
            return

        logger.info(f"Initializing {len(cls._agent_classes)} agents...")

        for name in cls._agent_classes.keys():
            try:
                agent = cls._initialize_agent(name)
                await agent.initialize()
                logger.info(f"Successfully initialized agent: {name}")
            except Exception as e:
                logger.error(f"Failed to initialize agent '{name}': {e}")
                # Continue initializing other agents
                continue

        cls._initialized = True
        logger.info("All agents initialized")

    @classmethod
    def get(cls, name: str) -> Optional[BaseAgent]:
        """
        Get an agent instance by name.

        Args:
            name: Agent name

        Returns:
            BaseAgent instance or None if not found

        Note:
            Agent must be initialized before use.
            Call initialize_all() during startup.
        """
        if name in cls._agents:
            return cls._agents[name]
        if name in cls._agent_classes:
            return cls._initialize_agent(name) 
        return None

    @classmethod
    def get_or_create(cls, name: str) -> BaseAgent:
        """
        Get existing agent or create new instance.

        Args:
            name: Agent name

        Returns:
            BaseAgent: Agent instance

        Raises:
            KeyError: If agent not registered
        """
        if name in cls._agents:
            return cls._agents[name]

        return cls._initialize_agent(name)

    @classmethod
    def list_agents(cls) -> List[str]:
        """
        List all registered agent names.

        Returns:
            List of agent names
        """
        return list(cls._agent_classes.keys())

    @classmethod
    def list_initialized_agents(cls) -> List[str]:
        """
        List names of initialized agents.

        Returns:
            List of initialized agent names
        """
        return list(cls._agents.keys())

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """
        Check if agent is registered.

        Args:
            name: Agent name

        Returns:
            bool: True if registered
        """
        return name in cls._agent_classes

    @classmethod
    def is_initialized(cls, name: str) -> bool:
        """
        Check if specific agent is initialized.

        Args:
            name: Agent name

        Returns:
            bool: True if initialized
        """
        agent = cls._agents.get(name)
        return agent is not None and agent.is_initialized()

    @classmethod
    def clear(cls) -> None:
        """
        Clear all registered agents.

        Warning: This will remove all agent instances.
        Use only for testing or cleanup.
        """
        cls._agents.clear()
        cls._agent_classes.clear()
        cls._initialized = False
        logger.info("Agent registry cleared")

    @classmethod
    def get_agent_info(cls, name: str) -> Dict[str, any]:
        """
        Get information about an agent.

        Args:
            name: Agent name

        Returns:
            Dict with agent information
        """
        if name not in cls._agent_classes:
            return {"error": f"Agent '{name}' not registered"}

        agent = cls._agents.get(name)

        return {
            "name": name,
            "registered": True,
            "initialized": agent is not None and agent.is_initialized(),
            "class": cls._agent_classes[name].__name__,
            "tools_count": len(agent.get_tools()) if agent else 0
        }

    @classmethod
    def get_all_agent_info(cls) -> Dict[str, Dict]:
        """
        Get information about all registered agents.

        Returns:
            Dict mapping agent names to their info
        """
        return {
            name: cls.get_agent_info(name)
            for name in cls._agent_classes.keys()
        }
