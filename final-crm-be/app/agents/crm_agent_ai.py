"""
CRM AI Agent
Basic customer service AI agent using Google ADK for handling customer inquiries
"""
import logging
from typing import List, Dict, Any, Optional
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class CRMAgent(BaseAgent):
    """
    Basic CRM AI agent for customer service.

    Features:
    - Multi-turn conversation support (chat history aware)
    - Multilingual (responds in same language as customer)
    - Professional and helpful tone
    - No special tools (basic LLM only)

    Use Case:
    - Auto-respond to customer inquiries on WhatsApp/Telegram/Email
    - Handle general customer service questions
    - Provide helpful and polite responses
    """

    def get_agent_name(self) -> str:
        """Get unique agent name"""
        return "crm_agent"

    def create_agent(self) -> LlmAgent:
        """
        Create and configure the CRM AI agent.

        Returns:
            LlmAgent configured for customer service
        """
        instruction = """You are a helpful customer service AI assistant.

Your role:
- Answer customer questions professionally and politely
- Be concise, clear, and helpful in your responses
- If you don't know something, admit it honestly
- Maintain a friendly but professional tone
- Focus on solving customer problems

Language Guidelines:
- ALWAYS respond in the same language as the customer
- Indonesian question → Indonesian answer
- English question → English answer
- Maintain natural, conversational language

Response Style:
- Be warm and empathetic
- Keep responses concise (2-3 sentences ideal)
- Avoid overly formal or robotic language
- If clarification needed, ask specific questions

Important:
- Do not make promises you cannot keep
- Do not provide information you're unsure about
- Stay within your role as a customer service assistant
"""

        agent = LlmAgent(
            name=self.get_agent_name(),
            model=LiteLlm(model="openai/gpt-3.5-turbo"),
            instruction=instruction,
        )

        logger.info("CRM AI agent created successfully")
        return agent

    async def process_message(
        self,
        chat_id: str,
        customer_message: str,
        chat_history: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Process customer message with optional chat history context.

        Args:
            chat_id: Chat UUID (used as session ID)
            customer_message: Latest message from customer
            chat_history: Previous messages in format:
                [{"role": "user"|"assistant", "content": "..."}]

        Returns:
            AI response text

        Example:
            agent = CRMAgent()
            await agent.initialize()

            response = await agent.process_message(
                chat_id="chat-uuid-123",
                customer_message="Halo, saya mau tanya tentang produk",
                chat_history=[
                    {"role": "user", "content": "Apakah toko buka hari ini?"},
                    {"role": "assistant", "content": "Ya, toko buka dari jam 9 pagi."}
                ]
            )
        """
        try:
            logger.info(f"🤖 Processing message for chat: {chat_id}")

            # Initialize chat history if not provided
            if chat_history is None:
                chat_history = []

            # Build conversation context
            conversation = []
            for msg in chat_history:
                conversation.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

            # Add current message
            conversation.append({
                "role": "user",
                "content": customer_message
            })

            # Prepare session state with conversation history
            session_state = {
                "conversation_history": conversation,
                "chat_id": chat_id
            }

            # Run agent with context
            # Note: run() returns tuple (response_text, session_id)
            response_text, session_id = await self.run(
                user_id=chat_id,
                query=customer_message,
                session_state=session_state
            )

            # Extract response
            response = response_text if response_text else ""

            if not response:
                # Fallback response if agent fails
                logger.warning(f"Empty response from agent for chat {chat_id}")
                response = self._get_fallback_response()

            logger.info(
                f"✅ CRM Agent response for chat {chat_id}: "
                f"{response[:100]}{'...' if len(response) > 100 else ''}"
            )

            return response

        except Exception as e:
            logger.error(f"❌ Error in CRM agent processing for chat {chat_id}: {e}")
            return self._get_error_response()

    def _get_fallback_response(self) -> str:
        """Get fallback response when agent fails to generate response"""
        return (
            "I apologize, but I'm having trouble generating a response. "
            "Could you please rephrase your question?"
        )

    def _get_error_response(self) -> str:
        """Get error response when agent encounters an error"""
        return (
            "I apologize, but I'm experiencing technical difficulties. "
            "Please try again in a moment, or contact our support team for immediate assistance."
        )


# Singleton instance for reuse
_crm_agent: Optional[CRMAgent] = None


async def get_crm_agent() -> CRMAgent:
    """
    Get or create singleton CRM agent instance.

    This ensures the agent is initialized only once and reused
    across multiple requests for better performance.

    Returns:
        Initialized CRMAgent instance

    Example:
        agent = await get_crm_agent()
        response = await agent.process_message(...)
    """
    global _crm_agent

    if _crm_agent is None:
        logger.info("Initializing CRM AI agent singleton...")
        _crm_agent = CRMAgent()
        await _crm_agent.initialize()
        logger.info("✅ CRM AI agent singleton initialized")

    return _crm_agent
