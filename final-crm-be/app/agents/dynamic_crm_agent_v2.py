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

    def _sanitize_text_results(self, text: str) -> str:
        """
        Forcefully cleans AI output to match WhatsApp formatting.
        1. Converts **Bold** to *Bold*
        2. Removes # Headers
        3. Flattens Links
        """
        import re
        
        if not text: return ""

        # 1. Convert Markdown Bold (**text**) to WhatsApp Bold (*text*)
        text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

        # 2. Convert Headers (### Title) to Bold (*Title*)
        text = re.sub(r'(?m)^#{1,6}\s+(.*)', r'*\1*', text)

        # 3. Convert [Link Name](URL) to "Link Name: URL"
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

        return text.strip()
    
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
            has_current_image: bool,
        ) -> str:
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "english")
            custom_instructions = persona.get("customInstructions", "").strip()
            handoff = advanced.get("handoffTriggers", {})
            lang_instruction = f"Reply ONLY in {language}."

            prompt = f"""You are {name}. Tone: {tone}. User: {name_user}. LANGUAGE RULE: {lang_instruction}
            ## CORE BEHAVIOR
            - ONLY answer based on the KNOWLEDGE BASE provided below.
            - If answer NOT in knowledge base, refuse politely.
            - NEVER make up facts.
            - Ignore previous history unless necessary.
            # NAME POLICY
            - User: "{name_user}".
            - **General Rule:** Do NOT use the name in normal technical explanations. It sounds robotic.
            - **Exceptions (Allowed):** 1. If the user is **Angry** (to calm them down).
              2. If the user says **"Thanks/Makasih/Arigatou"** (e.g., "Sama-sama, {name_user}!" is okay).
            ## UNIVERSAL FALLBACK PROTOCOL (STRICT & SMART)
            If the exact answer to the user's question is not available in the Knowledge Base:
            1. **NO GUESSING:** Never assume or infer information that isn’t clearly available.  
            (Example: If the user asks about "Plan A" but only "Plan B" is listed, do not explain Plan B as if it were the answer.)
            2. **HONEST RESPONSE:** Use a natural, human-friendly explanation, such as:  
            "Sorry, I don’t have the exact details for that right now.
            3. **HELPFUL DIRECTION (MANDATORY):**
            - Review the available information in the **Knowledge Base**.
            - Share any related **titles**, **topics**, or **error codes** that are available.
            - Present them as options the user can choose from.  
            - *Example:*  
                "What I can help with right now are these related topics: [list of available titles/codes]."
            4. **NEXT STEP:** If none of the listed items match the user’s issue, gently suggest reaching out to the support team for further assistance.
            """
            
            if handoff.get("enabled"):
                keywords = handoff.get("keywords", [])
                triggers = "is angry OR wants human"
                if keywords:
                    kw_str = " / ".join([f'"{k}"' for k in keywords])
                    triggers += f" OR types {kw_str}"
                prompt += f"""HANDOFF RULE: If user {triggers} → empathize + say 'HUMAN_HANDOFF'"""

            if rag_context and rag_context.strip():
                prompt += f"""## KNOWLEDGE BASE
            IMPORTANT: Use ONLY this info.
            ---
            {rag_context}
            ---
            """
            else:
                prompt += """## KNOWLEDGE BASE
                No knowledge base provided. Answer general greetings only.
            """

            if has_current_image:
                prompt += """## VISION UPDATE
            User sent an image. Extract codes/text and search the Knowledge Base for matches.
            """

            if len(custom_instructions) > 10:
                prompt += f"""INSTRUCTIONS:
            {custom_instructions}
            """
            else:
                prompt += f"""INSTRUCTIONS:
            1. **TONE & VOICE (ANTI-ROBOT MODE):**
               - **BANNED PHRASES (DO NOT USE):** * "Saya mengerti"
                 * "Tentu"
                 * "Mohon maaf atas ketidaknyamanan"
                 * "Kami memahami"
               - **REQUIRED:** You MUST start with a natural reaction word.
                 (Examples: "Wah, RC 68 ya?", "Oh, error itu...", "Hmm, sepertinya...", "Oke, mari kita cek...", "Siap, untuk masalah ini...")
               - **Style:** Talk like a Senior Engineer on WhatsApp. Direct, helpful, and slightly casual.

            2. **STRICT VISUAL STRUCTURE:**
               * **The Hook:** A short, casual reaction (using the examples above).
               * **The Explanation:** ONE sentence explaining the cause simply.
               * **The Solution:**
                   - Step 1 (Bullet Point)
                   - Step 2
                   - Step 3
               * **The Closing:** A short, friendly closing (e.g., "Kabari ya kalau masih gagal!").

            3. **FORMATTING:**
               - **BOLD** all Error Codes and Button Names.
               - Keep paragraphs short.

            4. **REWRITE RULE:**
               - Do not copy-paste. Explain it like you are talking to a friend.
            """
            return prompt
    
    def _build_messages(
        self,
        system_prompt: str,
        chat_history: List[Dict[str, Any]],
        customer_message: str,
        image_urls: List[str] = None 
    ) -> List[Dict[str, Any]]:
        """
        Build message chain (Multimodal support)
        """
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add history (TEXT ONLY)
        for msg in chat_history:
            role = "assistant" if msg.get("sender_type") == "ai" else "user"
            content_text = msg.get("content") or msg.get("message_content", "") or ""
            
            if content_text.strip():
                messages.append({"role": role, "content": content_text})
        
        # Add current message
        if image_urls and len(image_urls) > 0:
            content_payload = [
                {"type": "text", "text": customer_message}
            ]
            for url in image_urls:
                content_payload.append({
                    "type": "image_url",
                    "image_url": {"url": url}
                })
            messages.append({"role": "user", "content": content_payload})
            logger.info(f"📸 Attached {len(image_urls)} images inline.")
        else:
            messages.append({"role": "user", "content": customer_message})
        
        return messages
    
    def _build_files_array(self, image_urls: Optional[List[str]]) -> List[Dict[str, str]]:
        """
        Build files array for vision.
        """
        files = []
        
        if image_urls:
            for url in image_urls:
                files.append({
                    "type": "image",
                    "url": url
                })
            logger.info(f"📸 Attached {len(image_urls)} images.")
        
        return files

    async def analyze_image(self, image_url: str, prompt: str, organization_id: str) -> str:
        """Helper for Vision Interceptor"""
        try:
            payload = {
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]}
                ],
                "organization_id": organization_id,
                "temperature": 0.1 
            }
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.proxy_url, json=payload) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        return res.get("choices", [{}])[0].get("message", {}).get("content", "")
            return ""
        except Exception: return ""

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
        image_urls: List[str] = None,
        ticket_categories: List[str] = None,
        ticket_id: str = ""
    ) -> Dict[str, Any]:
        """
        Generate AI response with robust error handling (The 3 Safety Blocks)
        """
        try:
            # === 1. PARSE SETTINGS ===
            persona = self._parse_json(agent_settings.get("persona_config", {}))
            advanced = self._parse_json(agent_settings.get("advanced_config", {}))
            
            temp_map = {"consistent": 0.3, "balanced": 0.7, "creative": 1}
            temperature = temp_map.get(advanced.get("temperature", "balanced").lower(), 0.7)

            # === 2. BUILD SYSTEM PROMPT ===
            # [CRITICAL FIX] Removed 'ticket_categories' from this call
            system_prompt = self._build_system_prompt(
                persona=persona,
                advanced=advanced,
                rag_context=rag_context,
                name_user=name_user,
                has_current_image=bool(image_urls),
            )

            # === 3. BUILD MESSAGES ===
            messages = self._build_messages(
                system_prompt=system_prompt,
                chat_history=chat_history,
                customer_message=customer_message,
                image_urls=image_urls
            )

            # === 4. BUILD PAYLOAD ===
            payload = {
                "messages": messages,
                "files": [], 
                "category": category,
                "nameUser": name_user,
                "temperature": temperature,
                "organization_id": organization_id,
                "ticket_categories": ticket_categories or [],
                "ticket_id":ticket_id
            }
            # logger.info(f"{json.dumps(payload, indent=4)} <<<<<<<<<<")
            # === 5. CALL PROXY (WITH 3 ERROR BLOCKS) ===
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
                    
                    content = ""
                    try:
                        content = result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError):
                        content = result.get("reply") or result.get("content") or ""
                    
                    if not content:
                        content = "Mohon Maaf ya, kali ini kami belum bisa menjawab, silahkan ditanyakan kembali 😊."
                        logger.warning("⚠️ Empty response from proxy")

                    # [NEW] APPLY THE CLEANER HERE
                    clean_content = self._sanitize_text_results(content)

                    return {
                        "content": clean_content, 
                        "metadata": result.get("metadata", {}),
                        "usage": result.get("usage", {})
                    }

        # [ERROR BLOCK 1] Connection Failed (Offline)
        except aiohttp.ClientConnectorError:
            logger.error(f"❌ Cannot connect to proxy: {self.proxy_url}")
            return {
                "content": "Maaf ya, kali ini kami belum bisa menjawab, silahkan coba lagi.",
                "metadata": {"error": "Service Unavailable", "is_error": True},
                "usage": {}
            }

        # [ERROR BLOCK 2] Timeout (Too Slow)
        except asyncio.TimeoutError:
            logger.error("❌ Proxy timeout (>300s)")
            return {
                "content": "Maaf ya, kali ini kami belum bisa menjawab, silahkan coba lagi",
                "metadata": {"error": "Timeout", "is_error": True},
                "usage": {}
            }

        # [ERROR BLOCK 3] Catch-All (Logic Bugs / Crashes)
        except Exception as e:
            logger.error(f"❌ Speaker V2 Exception: {e}", exc_info=True)
            return {
                "content": "Maaf ya, kali ini kami belum bisa menjawab, silahkan coba lagi",
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