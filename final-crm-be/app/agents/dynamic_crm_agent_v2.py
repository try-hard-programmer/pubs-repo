"""
Dynamic CRM Agent V2
The "Speaker" for Team V2.
- Targets http://localhost:6657/v2/chat/completions
- Sends: messages, category, nameUser
- REMOVED: model, auth headers
- SSL: Disabled
"""
import logging
import json
import aiohttp
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class DynamicCRMAgentV2:
    def __init__(self):
        # [HARDCODED] V2 Chat Endpoint
        self.proxy_url = "http://localhost:6657/v2/chat/completions"

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
        Generates response using V2 Proxy.
        """
        try:
            # 1. Prepare System Prompt (Persona)
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "id")
            
            system_prompt = (
                f"You are {name}. Tone: {tone}. Language: {language}.\n"
                f"Context from knowledge base:\n{rag_context}\n\n"
                f"Answer the customer ({name_user}) helpfuly."
            )

            # 2. Build Messages
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add history (last 5)
            for msg in chat_history[-5:]:
                role = "assistant" if msg.get("sender_type") == "ai" else "user"
                content = msg.get("message_content", "")
                if content:
                    messages.append({"role": role, "content": content})
            
            # Add current message
            messages.append({"role": "user", "content": customer_message})

            # 3. Construct Payload
            payload = {
                "messages": messages,
                "category": category,   # [KEPT]
                "nameUser": name_user,  # [KEPT]
                "temperature": 0.7      # Optional
                # [REMOVED] "model" 
            }

            # 4. Send to Proxy V2 (No SSL, No Auth)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.proxy_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    ssl=False # [CRITICAL] Ignore SSL for localhost
                ) as response:
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Speaker V2 Error {response.status}: {error_text}")
                        return "Maaf, saya sedang istirahat sebentar. (Proxy Error)"
                    
                    result = await response.json()
                    
                    # Handle OpenAI format response
                    try:
                        return result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError):
                        # Fallback if proxy returns simple text
                        return result.get("reply") or result.get("content") or "Error parsing response."

        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}")
            return "Maaf, terjadi kesalahan koneksi."

    def _parse_json(self, data: Any) -> Dict:
        if isinstance(data, dict): return data
        if isinstance(data, str):
            try: return json.loads(data)
            except: return {}
        return {}

# Singleton
_agent_v2 = None
def get_dynamic_crm_agent_v2():
    global _agent_v2
    if _agent_v2 is None:
        _agent_v2 = DynamicCRMAgentV2()
    return _agent_v2