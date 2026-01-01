"""
Dynamic CRM AI Agent
Constructs AI personality and context dynamically per request.
Sends CUSTOM PAYLOAD (category, nameUser) to Proxy for routing.
"""
import logging
import json
import aiohttp
from typing import List, Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)

class DynamicCRMAgent:
    """
    Stateless agent that builds its persona at runtime and routes via Proxy.
    """

    def __init__(self):
        # Target the Proxy URL directly
        self.proxy_url = settings.PROXY_BASE_URL 
        self.api_key = settings.PLATFORM_KEY or settings.OPENAI_API_KEY

    async def process_message(
        self,
        chat_id: str,
        customer_message: str,
        chat_history: List[Dict[str, Any]],
        agent_settings: Dict[str, Any],
        rag_context: str = "",
        category: str = "general",
        name_user: str = "Customer"
    ) -> str:
        """
        Process message with dynamic persona and RAG context.
        Sends custom payload structure to proxy.
        """
        try:
            # 1. Parse Settings (Handle Double Serialization)
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))

            # 2. Extract Configs
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "id")
            custom_instructions = persona.get("customInstructions", "")
            
            # 3. Build Dynamic System Prompt
            system_instruction = f"""
You are a professional customer support agent, and you have cusomer has name {name}.

CORE SETTINGS:
- Tone: {tone}name
- Language Preference: {language} (Response MUST match this language code)
- Role: Customer Service

CUSTOM INSTRUCTIONS:
{custom_instructions}
name
KNOWLEDGE BASE CONTEXT:
{rag_context}

GUIDELINES:
- Answer based on the Context provided. 
- If the Context doesn't answer the question, apologize and ask for more details.
- Do NOT make up facts.
"""

            # 4. Apply History Limit & Build Messages
            history_limit = int(advanced.get("historyLimit", 10))
            limited_history = chat_history[-history_limit:] if chat_history else []

            messages = []
            # Add System Prompt
            messages.append({"role": "system", "content": system_instruction})
            
            # Add History
            for msg in limited_history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            
            # Add Current User Message
            messages.append({"role": "user", "content": customer_message})

            # 5. Construct CUSTOM Payload
            # [UPDATED] Removed "model" field to let Proxy handle it based on category
            payload = {
                "category": category,
                "nameUser": name_user,
                "messages": messages,
                "stream": False
            }

            logger.info(f"🚀 Sending to Proxy: User={name_user}, Cat={category}, Msgs={len(messages)}")

            # 6. Send Request via AIOHTTP
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                
                async with session.post(self.proxy_url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Proxy Error {response.status}: {error_text}")
                        return "Maaf, sistem sedang sibuk. Mohon coba lagi nanti."
                    
                    result = await response.json()
                    
                    # Handle OpenAI-compatible response format
                    try:
                        ai_content = result["choices"][0]["message"]["content"]
                        return ai_content
                    except (KeyError, IndexError):
                        logger.error(f"❌ Unexpected Proxy Response Format: {result}")
                        return "Maaf, terjadi kesalahan format respons."

        except Exception as e:
            logger.error(f"❌ Dynamic Agent Error: {e}")
            return "Maaf, terjadi kesalahan pada sistem kami."

    def _parse_json(self, data: Any) -> Dict:
        """Helper to handle double-serialized JSON strings from DB"""
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return {}
        return {}

# Singleton
_dynamic_agent = None

def get_dynamic_crm_agent():
    global _dynamic_agent
    if _dynamic_agent is None:
        _dynamic_agent = DynamicCRMAgent()
    return _dynamic_agent