import logging
import aiohttp
import asyncio
import json
from typing import List, Dict, Any, Optional
from app.config import settings
from app.services.mcp_service import get_mcp_service

logger = logging.getLogger(__name__)

class DynamicCRMAgentV2:
    def __init__(self):
        # Ensure URL ends with /chat
        base = settings.PROXY_BASE_URL.rstrip('/')
        self.proxy_url = f"{base}/chat"
        self.mcp_service = get_mcp_service()  

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
    
    async def reformulate_query(self, user_message: str, vision_context: str, organization_id: str) -> str:
        """Fast LLM call to extract a clean search query from messy user input"""
        combined = user_message.strip()
                
        # [TRACE] Flag if this looks like a no-content image
        if vision_context:
            lower_ctx = vision_context.lower()
            is_no_content = any(p in lower_ctx for p in [
                "visual content only", "no text detected", "no readable text",
                "[no_text_detected]", "no spoken words", "instrumental",
                "error processing image", "error processing audio"
            ])
            if is_no_content:
                logger.warning(f"📊 [TRACE:QUERY] ⚠️ IRRELEVANT IMAGE DETECTED — vision has no useful content, but RAG will still run (no gate yet)")
            
            combined = f"{combined}\n[Image Analysis: {vision_context}]"
        
        # Skip if already clean enough
        if not combined or len(combined) < 3:
            return combined

        payload = {
            "messages": [
                {"role": "system", "content": (
                    "Extract a concise search query from the user's message. "
                    "If there's image analysis, prioritize extracting codes, product names, or key terms from it. "
                    "IMPORTANT: Output in the SAME LANGUAGE as the user's message. "
                    "Output ONLY the search query, nothing else. Max 15 words."
                )},
                {"role": "user", "content": combined}
            ],
            "organization_id": organization_id,
            "temperature": 0.0,
            "max_tokens": 50
        }
        
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.proxy_url, json=payload) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        query = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                        if query and len(query) > 2:
                            logger.info(f"📊 [TRACE:QUERY] Reformulated: '{user_message[:10]}...' → '{query}'")
                            return query
        except Exception as e:
            logger.warning(f"⚠️ Reformulation failed, using raw: {e}")
        
        # Fallback: concat raw
        logger.info(f"📊 [TRACE:QUERY] Using raw (no reformulation): '{combined[:10]}...'")
        return combined

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
            external_tools: List[Dict] = None,  
        ) -> str:
            name = persona.get("name", "Support Agent")
            tone = persona.get("tone", "friendly")
            language = persona.get("language", "english")
            custom_instructions = persona.get("customInstructions", "").strip()
            handoff = advanced.get("handoffTriggers", {})
            lang_instruction = f"Reply ONLY in {language}."            

            use_custom = len(custom_instructions) > 10

            # ── SWITCH: custom instructions vs default persona ──────────────────────
            match use_custom:
                case True:
                    prompt = f"""
        {custom_instructions}
        """
                case False:
                    prompt = f"""
                    You are {name}. Tone: {tone}. User: {name_user}. LANGUAGE RULE: {lang_instruction}
                    ## CORE INSTRUCTION
                    Please answer the user's questions based on the provided **KNOWLEDGE BASE**. 
                    
                    **Guidelines:**
                    1. Use the information in the Knowledge Base to provide accurate answers.
                    2. If the answer is not found in the Knowledge Base, politely inform the user that you don't have that information.
                    3. Keep the tone natural, helpful, and friendly.
                    """
            # ────────────────────────────────────────────────────────────────────────

            if handoff.get("enabled"):
                keywords = handoff.get("keywords", [])
                triggers = "is angry OR wants human"
                if keywords:
                    kw_str = " / ".join([f'"{k}"' for k in keywords])
                    triggers += f" OR types {kw_str}"
                prompt += f"""HANDOFF RULE: If user {triggers} → empathize + say 'Please connect to your engineer!' with keeping using the same {language}"""

            if external_tools and len(external_tools) > 0:
                tool_descriptions = []
                for tool in external_tools:
                    tool_name = tool['function']['name'].split('__')[-1].replace('_', ' ')
                    tool_desc = tool['function'].get('description', '')
                    tool_descriptions.append(f"• {tool_name}: {tool_desc}")
                
                prompt += f"""
            ## AVAILABLE TOOLS
            You have access to these tools to help users:
            {chr(10).join(tool_descriptions)}
            """

            if not use_custom:
                if rag_context and rag_context.strip():
                    prompt += f"""## KNOWLEDGE BASE
                IMPORTANT: Use this info to answer questions.
                ---
                {rag_context}
                ---
                """
                elif external_tools and len(external_tools) > 0:
                    prompt += """## KNOWLEDGE BASE
                No static knowledge base. Use the AVAILABLE TOOLS above to help users with reservations and bookings.
                """
                else:
                    logger.info(f"📊 [TRACE:PROMPT] No RAG context — agent will use greeting-only mode")
                    prompt += """## KNOWLEDGE BASE
                No knowledge base provided. Answer general greetings only.
                """
            else:
                if rag_context and rag_context.strip():
                    prompt += f"""## KNOWLEDGE BASE
                ---
                {rag_context}
                ---
                """

            if has_current_image:
                logger.info(f"📊 [TRACE:PROMPT] Image flag is ON — LLM told to extract codes from image")
                prompt += """## VISION UPDATE
            User sent an image. Extract codes/text and search the Knowledge Base for matches.
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
        
        # Add history
        for msg in chat_history:
            # FIX: Trust the 'role' if it exists. Only check 'sender_type' as a fallback.
            if "role" in msg:
                role = msg["role"]
            else:
                # Fallback for legacy objects
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
        ticket_id: str = "",
        external_tools: List[Dict] = None,
        supabase: Any = None
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
            system_prompt = self._build_system_prompt(
                persona=persona,
                advanced=advanced,
                rag_context=rag_context,
                name_user=name_user,
                has_current_image=bool(image_urls),
                external_tools=external_tools,  
            )

            # === 3. BUILD MESSAGES ===
            messages = self._build_messages(
                system_prompt=system_prompt,
                chat_history=chat_history,
                customer_message=customer_message,
                image_urls=image_urls
            )
            
            # === 4. EXECUTION LOOP (MCP SUPPORT) ===
            current_turn = 0
            max_turns = 5 
            final_usage = {"total_tokens": 0}
            timeout = aiohttp.ClientTimeout(total=300)

            while current_turn < max_turns:
                current_turn += 1

                # Build Payload
                payload = {
                    "messages": messages,
                    "files": [], 
                    "category": category,
                    "nameUser": name_user,
                    "temperature": temperature,
                    "organization_id": organization_id,
                    "ticket_categories": ticket_categories or [],
                    "ticket_id": ticket_id
                }

                # Inject Tools
                if external_tools:
                    payload["tools"] = external_tools
                    payload["tool_choice"] = "auto"

                # logger.info(f"🚀 AI Payload (Turn {current_turn}):\n{json.dumps(payload, indent=2, default=str)}")
                
                # === 5. CALL PROXY ===
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
                                "usage": final_usage
                            }
                        
                        result = await response.json()
                        
                        # Handle varied proxy response structures
                        choice = result.get("choices", [{}])[0]
                        message = choice.get("message", {})
                        
                        # Accumulate usage
                        u = result.get("usage", {})
                        final_usage["total_tokens"] += u.get("total_tokens", 0)

                        # === 6. HANDLE TOOL CALLS ===
                        tool_calls = message.get("tool_calls")
                        
                        if tool_calls:
                            # A. Append AI's intent to history
                            messages.append(message) 
                            
                            # B. Execute Tools
                            for tool in tool_calls:
                                func_name = tool["function"]["name"]
                                func_args = json.loads(tool["function"]["arguments"])
                                call_id = tool["id"]
                                                                
                                # Execute via MCP Service
                                tool_result = await self.mcp_service.execute_mcp_tool(
                                    supabase=supabase,
                                    agent_id=agent_settings.get("agent_id"),
                                    tool_call_name=func_name,
                                    arguments=func_args
                                )
                                
                                tool_output = tool_result.get("output", "Error executing tool")
                                
                                # C. Append Result to history
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "name": func_name,
                                    "content": tool_output
                                })

                            # D. Loop again!
                            continue 
                        
                        # === 7. FINAL TEXT RESPONSE ===
                        content = ""
                        try:
                            content = message["content"]
                        except (KeyError, IndexError):
                            content = result.get("reply") or result.get("content") or ""
                        
                        if not content:
                            content = "Mohon Maaf ya, kali ini kami belum bisa menjawab, silahkan ditanyakan kembali 😊."
                            logger.warning("⚠️ Empty response from proxy")

                        # Apply Cleaner
                        clean_content = self._sanitize_text_results(content)

                        return {
                            "content": clean_content, 
                            "metadata": result.get("metadata", {}),
                            "usage": final_usage
                        }

            return {"content": "Loop limit reached.", "metadata": {"is_error": True}, "usage": final_usage}

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