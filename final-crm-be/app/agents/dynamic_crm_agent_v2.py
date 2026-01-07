"""
Dynamic CRM Agent V2 (The Speaker)
- Stateless Logic.
- Constructs System Prompt from DB Settings + RAG Context.
- Sends payload to Local Proxy V2.
"""
import logging
import aiohttp
import json
from typing import List, Dict, Any
from app.config import settings

logger = logging.getLogger(__name__)

class DynamicCRMAgentV2:
    def __init__(self):
        self.proxy_url = f"{settings.PROXY_BASE_URL.rstrip('/')}/chat"

    def _parse_json(self, data: Any) -> Dict:
        if isinstance(data, dict): return data
        if isinstance(data, str):
            try: return json.loads(data)
            except: return {}
        return {}

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
        try:
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))

            # A. Persona Settings
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "indonesia")
            custom_instructions = persona.get("customInstructions", "")

            # B. Advanced Settings
            # Map text temperature to float
            temp_setting = advanced.get("temperature", "balanced")
            temp_map = {"precise": 0.1, "balanced": 0.5, "creative": 0.8}
            temperature = temp_map.get(temp_setting, 0.5)

            # --- BUILD SYSTEM PROMPT ---
            system_prompt = (
                f"IDENTITY:\n"
                f"You are {name}.\n"
                f"Tone: {tone}.\n"
                f"Language: {language}.\n\n"
            )

            if custom_instructions:
                system_prompt += (
                    f"OPERATIONAL INSTRUCTIONS:\n"
                    f"{custom_instructions}\n\n"
                )

            # Strict Safety Layer
            system_prompt += (
                f"STRICT KNOWLEDGE BOUNDARIES:\n"
                f"1. You are a CLOSED-DOMAIN agent. NO outside knowledge allowed.\n"
                f"2. You MUST answer using ONLY the 'CONTEXT' below.\n"
                f"3. If the answer is NOT in the CONTEXT, politely refuse. Do NOT hallucinate.\n\n"
                f"CONTEXT (SOURCE OF TRUTH):\n"
                f"###\n{rag_context}\n###\n\n"
                f"FINAL GUIDELINES:\n"
                f"- Address customer as '{name_user}'.\n"
                f"- Do NOT mention 'RAG' or 'training data'.\n"
            )

            # --- BUILD MESSAGE CHAIN ---
            messages = [{"role": "system", "content": system_prompt}]
            
            # History already limited by Service, but good to check emptiness
            for msg in chat_history:
                role = "assistant" if msg.get("sender_type") == "ai" else "user"
                content = msg.get("content") or msg.get("message_content", "")
                if content:
                    messages.append({"role": role, "content": content})
            
            messages.append({"role": "user", "content": customer_message})

            # --- SEND TO PROXY ---
            payload = {
                "messages": messages,
                "category": category,
                "nameUser": name_user,
                "temperature": temperature
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.proxy_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    ssl=False 
                ) as response:
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Speaker V2 Error {response.status}: {error_text}")
                        return "Maaf, saya sedang istirahat sebentar."
                    
                    result = await response.json()
                    
                    try:
                        return result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError):
                        return result.get("reply") or result.get("content") or "Error parsing response."

        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}")
            return "Maaf, sistem sedang sibuk. Mohon coba lagi nanti."

# Singleton
_dynamic_crm_agent_v2 = None
def get_dynamic_crm_agent_v2():
    global _dynamic_crm_agent_v2
    if _dynamic_crm_agent_v2 is None:
        _dynamic_crm_agent_v2 = DynamicCRMAgentV2()
    return _dynamic_crm_agent_v2