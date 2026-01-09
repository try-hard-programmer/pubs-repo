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
        # Ensure URL ends with /chat
        base = settings.PROXY_BASE_URL.rstrip('/')
        self.proxy_url = f"{base}/chat"

    def _parse_json(self, data: Any) -> Dict:
        """Helper to safely parse JSON strings or dicts from Supabase"""
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
        organization_id: str,     # <--- MATCHING ARGUMENT for V2
        rag_context: str = "",
        category: str = "general",
        name_user: str = "Customer"
    ) -> Dict[str, Any]:
        """
        Generates the AI response. 
        Returns Dict: { "content": str, "usage": dict, "metadata": dict }
        """
        try:
            # 1. PARSE SETTINGS
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))
            
            # Extract Handoff Config (Escalation is AI's job)
            handoff = advanced.get("handoffTriggers", {})

            # 2. EXTRACT VARIABLES
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "indonesia")
            custom_instructions = persona.get("customInstructions", "")
            
            # Temperature Mapping
            temp_setting = advanced.get("temperature", "balanced")
            temp_map = {"precise": 0.1, "balanced": 0.5, "creative": 0.8}
            temperature = temp_map.get(temp_setting, 0.5)

            # 3. BUILD SYSTEM PROMPT
            system_prompt = (
                f"IDENTITY:\n"
                f"You are {name}.\n"
                f"Tone: {tone}.\n"
                f"Language: {language}.\n\n"
            )

            # INJECT HANDOFF RULES
            if handoff.get("enabled"):
                system_prompt += (
                    f"ESCALATION RULES:\n"
                    f"- If the user is angry or asks for a human, apologize and say you are connecting them to an agent.\n"
                    f"- Trigger Keyword: 'HUMAN_HANDOFF'.\n\n"
                )

            # Custom Instructions
            if custom_instructions:
                system_prompt += (
                    f"OPERATIONAL INSTRUCTIONS:\n"
                    f"{custom_instructions}\n\n"
                )

            # RAG / Knowledge Boundaries
            system_prompt += (
                f"STRICT KNOWLEDGE BOUNDARIES:\n"
                f"1. You are a CLOSED-DOMAIN agent. NO outside knowledge allowed.\n"
                f"2. You MUST answer using ONLY the 'CONTEXT' below.\n"
                f"3. If the answer is NOT in the CONTEXT, politely refuse.\n\n"
                f"CONTEXT (SOURCE OF TRUTH):\n"
                f"###\n{rag_context}\n###\n\n"
                f"FINAL GUIDELINES:\n"
                f"- Address customer as '{name_user}'.\n"
            )

            # 4. BUILD MESSAGE CHAIN
            messages = [{"role": "system", "content": system_prompt}]
            
            for msg in chat_history:
                # Map DB roles to LLM roles
                role = "assistant" if msg.get("sender_type") == "ai" else "user"
                content = msg.get("content") or msg.get("message_content", "")
                if content:
                    messages.append({"role": role, "content": content})
            
            messages.append({"role": "user", "content": customer_message})

            # 5. SEND TO PROXY
            payload = {
                "messages": messages,
                "category": category,
                "nameUser": name_user,
                "temperature": temperature,
                "organization_id": organization_id # <--- Sending this to Proxy for Logging
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
                        # Return clean error dict, don't crash
                        return {
                            "content": "Maaf, saya sedang istirahat sebentar.",
                            "metadata": {"error": f"Proxy {response.status}"},
                            "usage": {}
                        }
                    
                    result = await response.json()
                    
                    # Handle diverse proxy responses (OpenAI vs Simple)
                    content = ""
                    try:
                        content = result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError):
                        content = result.get("reply") or result.get("content") or "Error parsing response."

                    return {
                        "content": content,
                        "metadata": result.get("metadata", {}),
                        "usage": result.get("usage", {})
                    }

        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}")
            # Fallback return
            return {
                "content": "Maaf, sistem sedang sibuk. Mohon coba lagi nanti.",
                "metadata": {"error": str(e)},
                "usage": {}
            }

# Singleton
_dynamic_crm_agent_v2 = None
def get_dynamic_crm_agent_v2():
    global _dynamic_crm_agent_v2
    if _dynamic_crm_agent_v2 is None:
        _dynamic_crm_agent_v2 = DynamicCRMAgentV2()
    return _dynamic_crm_agent_v2