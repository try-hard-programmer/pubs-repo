"""
Dynamic CRM Agent V2 (The Speaker)
"""
import logging
import aiohttp
import asyncio
import json
from typing import List, Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)


class DynamicCRMAgentV2:
    def __init__(self):
        # Ensure URL ends with /chat
        base = settings.PROXY_BASE_URL.rstrip('/')
        self.proxy_url = f"{base}/chat"
        
        logger.info(f"🔊 Speaker V2 initialized → {self.proxy_url}")

    def _parse_json(self, data: Any) -> Dict:
        """Helper to safely parse JSON strings or dicts from Supabase"""
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                return json.loads(data)
            except:
                return {}
        return {}

    def _build_system_prompt(
            self,
            persona: Dict,
            advanced: Dict,
            rag_context: str,
            name_user: str,
            has_current_image: bool
        ) -> str:
            """Lean system prompt - optimized for token efficiency"""
            
            # === EXTRACT SETTINGS ===
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "auto")
            custom_instructions = persona.get("customInstructions", "").strip()
            handoff = advanced.get("handoffTriggers", {})
            use_custom_override = len(custom_instructions) > 10
            
            # === CORE IDENTITY (Compact) ===
            prompt = f"""You are {name}. Tone: {tone}. User: {name_user}. Language: mirror user's language, default {language}.

        """

            # === HANDOFF ===
            if handoff.get("enabled"):
                prompt += """If user angry/wants human → empathize + say 'HUMAN_HANDOFF'

        """

            # === KNOWLEDGE BASE ===
            if rag_context and rag_context.strip():
                prompt += f"""KNOWLEDGE:
        {rag_context}
        ---

        """

            # === IMAGE ===
            if has_current_image:
                prompt += """User sent image. Analyze it for context.

        """

            # === LAYER 4: RULES ===
            if use_custom_override:
                prompt += f"""INSTRUCTIONS:
        {custom_instructions}
        """
                logger.info(f"📝 CUSTOM mode ({len(custom_instructions)} chars)")
                
            else:
                prompt += """RULES:
        - Answer from KNOWLEDGE when available
        - Multiple items? Answer each. Unknown items? Say so briefly.
        - Gratitude only? Short reply, stop.
        - No info? Be honest, suggest contact support.
        - Don't invent. Don't over-explain. Be natural.
        """
                logger.info("🛡️ SMART DEFAULT mode")

            return prompt

    def _build_messages(
        self,
        system_prompt: str,
        chat_history: List[Dict[str, Any]],
        customer_message: str
    ) -> List[Dict[str, str]]:
        """
        Build message chain for LLM.
        
        IMPORTANT: Only TEXT in messages. Images handled separately in files array.
        History images are EXCLUDED to prevent confusion.
        """
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add history (TEXT ONLY - no images from history)
        for msg in chat_history:
            role = "assistant" if msg.get("sender_type") == "ai" else "user"
            content_text = msg.get("content") or msg.get("message_content", "") or ""
            
            if content_text.strip():
                messages.append({"role": role, "content": content_text})
        
        # Add current message (TEXT ONLY)
        messages.append({"role": "user", "content": customer_message})
        
        return messages

    def _build_files_array(self, image_url: Optional[str]) -> List[Dict[str, str]]:
        """
        Build files array for vision.
        
        IMPORTANT: Only CURRENT message image. History images excluded.
        This prevents LLM confusion about which image to analyze.
        """
        files = []
        
        if image_url:
            files.append({
                "type": "image",
                "url": image_url
            })
            logger.info(f"📸 Current image attached: {image_url[:60]}...")
        else:
            logger.debug("📸 No image in current message")
        
        return files

    async def analyze_image(self, image_url: str, prompt: str, organization_id: str) -> str:
        """
        Helper for the Manager's Vision Interceptor to analyze a specific image.
        Used to extract text/errors from images BEFORE RAG runs.
        """
        try:
            payload = {
                "messages": [{"role": "user", "content": prompt}],
                "files": [{"type": "image", "url": image_url}],
                "organization_id": organization_id,
                "temperature": 0.1 # Keep it precise for OCR/Reading
            }
            
            # We use the same proxy URL
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.proxy_url, json=payload) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        # Extract content safely
                        content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                        return content
            return ""
            
        except Exception as e:
            logger.error(f"❌ Vision Interceptor Error: {e}")
            return ""
        
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
        image_urls: List[str] = None # [FIX] Changed to List to match Manager
    ) -> Dict[str, Any]:
        """
        Generate AI response with:
        - Smart two-layer prompting
        - Vision support (current image only)
        - Anti-hallucination protection
        
        Returns:
            Dict with keys: content, usage, metadata
        """
        try:
            # === 1. PARSE SETTINGS ===
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))
            
            # Temperature mapping
            temp_setting = advanced.get("temperature", "balanced")
            temp_map = {"precise": 0.1, "balanced": 0.5, "creative": 0.8}
            temperature = temp_map.get(temp_setting, 0.5)

            # [FIX] Determine if we have images (List check)
            has_current_image = bool(image_urls and len(image_urls) > 0)

            # === 2. BUILD SYSTEM PROMPT ===
            system_prompt = self._build_system_prompt(
                persona=persona,
                advanced=advanced,
                rag_context=rag_context,
                name_user=name_user,
                # [FIX] Logic updated for list
                has_current_image=has_current_image 
            )

            # === 3. BUILD MESSAGE CHAIN (Text Only) ===
            messages = self._build_messages(
                system_prompt=system_prompt,
                chat_history=chat_history,
                customer_message=customer_message
            )

            # === 4. BUILD FILES ARRAY (Current Image Only) ===
            # [FIX] Passing list to builder
            files = self._build_files_array(image_urls)

            # === 5. BUILD PAYLOAD ===
            payload = {
                "messages": messages,
                "files": files,
                "category": category,
                "nameUser": name_user,
                "temperature": temperature,
                "organization_id": organization_id
                # NOTE: No "model" field - proxy handles model selection
            }

            logger.info(f"🌐 Calling proxy: {len(messages)} messages, {len(files)} files, temp={temperature}")

            # === 6. CALL PROXY ===
            # 5 minute timeout for heavy load scenarios
            timeout = aiohttp.ClientTimeout(total=300)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.proxy_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Proxy Error {response.status}: {error_text}")
                        return {
                            "content": "Sorry, the system is currently busy. Please try again in a moment.",
                            "metadata": {"error": f"Proxy {response.status}", "is_error": True},
                            "usage": {}
                        }
                    
                    result = await response.json()
                    
                    # Parse response content
                    content = ""
                    try:
                        content = result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError):
                        content = result.get("reply") or result.get("content") or ""
                    
                    if not content:
                        content = "Sorry, I cannot process this request."
                        logger.warning("⚠️ Empty response from proxy")

                    return {
                        "content": content,
                        "metadata": result.get("metadata", {}),
                        "usage": result.get("usage", {})
                    }

        except aiohttp.ClientConnectorError:
            logger.error(f"❌ Cannot connect to proxy: {self.proxy_url}")
            return {
                "content": "Sorry, the system is under maintenance. Please try again later.",
                "metadata": {"error": "Service Unavailable", "is_error": True},
                "usage": {}
            }

        except asyncio.TimeoutError:
            logger.error("❌ Proxy timeout (>300s)")
            return {
                "content": "Sorry, the response took too long. Please try again.",
                "metadata": {"error": "Timeout", "is_error": True},
                "usage": {}
            }

        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}", exc_info=True)
            return {
                "content": "Sorry, a system error occurred. Please try again later.",
                "metadata": {"error": str(e), "is_error": True},
                "usage": {}
            }
        

# === SINGLETON ===
_dynamic_crm_agent_v2 = None

def get_dynamic_crm_agent_v2() -> DynamicCRMAgentV2:
    global _dynamic_crm_agent_v2
    if _dynamic_crm_agent_v2 is None:
        _dynamic_crm_agent_v2 = DynamicCRMAgentV2()
    return _dynamic_crm_agent_v2