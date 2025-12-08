"""
Google ADK Agents Package

This package contains all AI agents built with Google Agent Development Kit (ADK).
Agents can be used standalone or orchestrated by the main agent.
"""

from .agent_registry import AgentRegistry

__all__ = [
    "AgentRegistry",
]
