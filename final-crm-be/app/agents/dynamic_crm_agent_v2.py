"""
Dynamic CRM Agent V2 (The Speaker)
- Stateless Logic.
- Constructs System Prompt from DB Settings + RAG Context.
- Sends payload to Local Proxy V2.
"""
import logging
import aiohttp
import asyncio
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
        organization_id: str,
        rag_context: str = "",
        category: str = "general",
        name_user: str = "Customer",
        image_url: str = None
    ) -> Dict[str, Any]:
        """
        Generates the AI response with Vision support.
        Returns Dict: { "content": str, "usage": dict, "metadata": dict }
        """
        try:
            # 1. PARSE SETTINGS
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))
            
            # Extract Handoff Config
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

            if handoff.get("enabled"):
                system_prompt += (
                    f"ESCALATION RULES:\n"
                    f"- If the user is angry or asks for a human, apologize and say you are connecting them to an agent.\n"
                    f"- Trigger Keyword: 'HUMAN_HANDOFF'.\n\n"
                )

            if custom_instructions:
                system_prompt += f"MUST FOLLOWING THIS INSTRUCTIONS:\n{custom_instructions}\n\n"

            system_prompt += (
                f"STRICT KNOWLEDGE BOUNDARIES:\n"
                f"1. You are a CLOSED-DOMAIN agent. NO outside knowledge allowed.\n"
                f"2. You MUST answer using ONLY the 'CONTEXT' below.\n"
                f"3. If the CONTEXT is empty or does not contain the answer, say: 'Maaf, saya tidak memiliki informasi mengenai hal tersebut.'\n"
                f"4. NEVER invent, assume, or guess information not in CONTEXT.\n\n"
                f"CONTEXT (SOURCE OF TRUTH):\n"
                f"###\n{rag_context if rag_context else '[NO RELEVANT INFORMATION FOUND]'}\n###\n\n"
                f"FINAL GUIDELINES:\n"
                f"- Address customer as '{name_user}'.\n"
                f"- Always respond to the customer's LAST message only.\n"
                f"- If the last message is an acknowledgment or thank you, respond briefly and ask if they need anything else.\n"
                f"- Do NOT repeat previous answers.\n"
            )
            
            # 4. BUILD MESSAGE CHAIN (TEXT ONLY - Images go in files array)
            messages = [{"role": "system", "content": system_prompt}]
            
            for msg in chat_history:
                role = "assistant" if msg.get("sender_type") == "ai" else "user"
                content_text = msg.get("content") or msg.get("message_content", "") or ""
                
                # Add text content only (images handled separately)
                if content_text:
                    messages.append({"role": role, "content": content_text})
            
            # 5. ADD CURRENT MESSAGE (TEXT ONLY)
            messages.append({"role": "user", "content": customer_message})

            # ✅ 6. BUILD FILES ARRAY FOR IMAGES
            files = []
            if image_url:
                files.append({
                    "type": "image",
                    "url": image_url
                })
                logger.info(f"📸 Adding image to request: {image_url[:50]}...")

            # 7. SEND TO PROXY
            payload = {
                "messages": messages,
                "files": files,
                "category": category,
                "nameUser": name_user,
                "temperature": temperature,
                "organization_id": organization_id
            }

            logger.info(f"🌐 Calling proxy with {len(messages)} messages, {len(files)} files")

            # [FIX] INCREASE TIMEOUT TO 5 MINUTES (300s)
            # This ensures Python waits for the Node.js Queue even under heavy load.
            timeout = aiohttp.ClientTimeout(total=300) 

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.proxy_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Speaker V2 Error {response.status}: {error_text}")
                        return {
                            "content": "Maaf, saya sedang istirahat sebentar.",
                            "metadata": {"error": f"Proxy {response.status}", "is_error": True},
                            "usage": {}
                        }
                    
                    result = await response.json()
                    
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

        except aiohttp.ClientConnectorError:
            logger.error(f"❌ Speaker V2 Offline: Cannot connect to {self.proxy_url}")
            return {
                "content": "Maaf, sistem sedang under maintenance. Mohon coba lagi nanti",
                "metadata": {"error": "Service Unavailable"},
                "usage": {},
                "is_error": True
            }
        
        except asyncio.TimeoutError:
            logger.error(f"❌ Speaker V2 Timeout: Proxy took longer than 300s")
            return {
                "content": "Maaf, respon terlalu lama. Mohon coba lagi.",
                "metadata": {"error": "Timeout"},
                "usage": {},
                "is_error": True
            }

        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}", exc_info=True)
            return {
                "content": "Maaf, sistem sedang sibuk. Mohon coba lagi nanti.",
                "metadata": {"error": str(e), "is_error": True},
                "usage": {}
            }

        
# Singleton
_dynamic_crm_agent_v2 = None
def get_dynamic_crm_agent_v2():
    global _dynamic_crm_agent_v2
    if _dynamic_crm_agent_v2 is None:
        _dynamic_crm_agent_v2 = DynamicCRMAgentV2()
    return _dynamic_crm_agent_v2