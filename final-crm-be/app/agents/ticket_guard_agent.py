import json
import logging
import os
from typing import Dict, Any

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from app.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

class TicketGuardAgent(BaseAgent):
    """
    Ticket Guard Agent
    
    Specialized agent for analyzing incoming messages to determine if a support ticket 
    should be created. It outputs structured JSON.
    """

    def get_agent_name(self) -> str:
        return "ticket_guard_agent"

    def _load_rules(self) -> Dict[str, Any]:
        """Load rules from config file, with fallback"""
        try:
            # Assuming app/config/ticket_rules.json exists
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, "config", "ticket_rules.json")
            
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load ticket rules: {e}")
        
        # Safe defaults
        return {
            "negative_intents": ["hi", "hello", "test"],
            "positive_intents": ["help", "error", "problem"],
            "priority_keywords": {"urgent": ["urgent"], "high": ["billing"]}
        }

    def create_agent(self) -> LlmAgent:
        rules = self._load_rules()
        
        instruction = f"""
        You are the Ticket Guard AI. Your ONLY job is to classify if a message needs a support ticket.

        **CONFIGURATION:**
        - Ignore (No Ticket): {json.dumps(rules.get('negative_intents', []))}
        - Create Ticket: {json.dumps(rules.get('positive_intents', []))}
        - Priority Keywords: {json.dumps(rules.get('priority_keywords', {}))}

        **LOGIC:**
        1. If message is greeting/spam/vague -> should_create_ticket = false.
        2. If message has actionable intent -> should_create_ticket = true.
        3. Determine priority based on keywords (default: medium).
        4. Categorize the issue (e.g., billing, technical, inquiry).

        **OUTPUT FORMAT:**
        Return ONLY a raw JSON object (no markdown formatting).
        {{
            "should_create_ticket": boolean,
            "reason": "short explanation",
            "suggested_priority": "low" | "medium" | "high" | "urgent",
            "suggested_category": "string",
            "auto_reply_hint": "string (what to say if rejected)"
        }}
        """

        # Use a lightweight model for speed/cost (e.g., gpt-4o-mini or gpt-3.5-turbo)
        agent = LlmAgent(
            name=self.get_agent_name(),
            model=LiteLlm(model="openai/gpt-4o-mini"), 
            instruction=instruction
        )
        
        logger.info(f"Initialized {self.get_agent_name()}")
        return agent