import logging
import httpx
import asyncio
import json
from typing import Dict, Any, List, Optional
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

class MCPService:
    
    def __init__(self):
        # Cache for active MCP client sessions
        self._sessions: Dict[str, ClientSession] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
    
    # =========================================================
    # 1. SESSION MANAGEMENT
    # =========================================================
    async def _get_or_create_session(
        self, 
        url: str,
        transport: str,
        api_key: Optional[str] = None
    ) -> Optional[ClientSession]:
        """
        Get existing session or create new one using official MCP SDK.
        """
        session_key = f"{transport}:{url}"
        
        # Return cached session if exists
        if session_key in self._sessions:
            return self._sessions[session_key]
        
        # Ensure lock exists
        if session_key not in self._session_locks:
            self._session_locks[session_key] = asyncio.Lock()
        
        async with self._session_locks[session_key]:
            # Double-check after acquiring lock
            if session_key in self._sessions:
                return self._sessions[session_key]
            
            try:
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                
                if transport == "sse":
                    # Use official MCP SSE client
                    read, write = await sse_client(url, headers=headers).__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    
                    # Initialize the session
                    await session.initialize()
                    
                    self._sessions[session_key] = session
                    logger.info(f"✅ MCP SSE Session created: {url}")
                    return session
                    
                elif transport == "http":
                    # For HTTP, we use direct httpx calls (no persistent session needed)
                    # Return None to signal direct HTTP mode
                    return None
                    
            except Exception as e:
                logger.error(f"Failed to create MCP session for {url}: {e}")
                return None
        
        return None
    
    async def _close_session(self, url: str, transport: str):
        """Close and remove a cached session."""
        session_key = f"{transport}:{url}"
        if session_key in self._sessions:
            try:
                await self._sessions[session_key].__aexit__(None, None, None)
            except:
                pass
            del self._sessions[session_key]
    
    # =========================================================
    # 2. TRANSPORT HELPERS
    # =========================================================
    async def _http_request(
        self,
        url: str,
        method: str,
        params: Optional[Dict] = None,
        api_key: Optional[str] = None,
        timeout: float = 10.0
    ) -> Dict[str, Any]:
        """Direct HTTP JSON-RPC request (for HTTP transport)."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Syntra-CRM-Backend/1.0"
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": 1
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                
                if resp.status_code != 200:
                    logger.error(f"HTTP Error {resp.status_code}: {resp.text}")
                    return {"error": f"HTTP {resp.status_code}", "success": False}

                data = resp.json()
                
                if "error" in data:
                    logger.error(f"JSON-RPC Error: {data['error']}")
                    return {"error": data["error"], "success": False}

                return {"result": data.get("result"), "success": True}

        except Exception as e:
            logger.error(f"HTTP Request Error: {e}")
            return {"error": str(e), "success": False}
    
    async def _sse_call_tool(
        self,
        session: ClientSession,
        tool_name: str,
        arguments: Dict
    ) -> Dict[str, Any]:
        """Call tool using MCP SDK session."""
        try:
            result = await session.call_tool(tool_name, arguments)
            
            # Extract text content
            output_parts = []
            for content in result.content:
                if hasattr(content, 'text'):
                    output_parts.append(content.text)
            
            return {
                "success": True,
                "result": {
                    "content": [{"type": "text", "text": "\n".join(output_parts)}]
                }
            }
        except Exception as e:
            logger.error(f"SSE tool call error: {e}")
            return {"error": str(e), "success": False}

    # =========================================================
    # 3. SERVER DISCOVERY
    # =========================================================
    async def _get_active_servers(self, supabase, agent_id: str) -> List[Dict]:
        """Fetch MCP servers from agent_integrations table."""
        try:
            res = supabase.table("agent_integrations")\
                .select("config")\
                .eq("agent_id", agent_id)\
                .eq("channel", "mcp")\
                .eq("enabled", True)\
                .execute()
            
            if not res.data:
                return []

            active_servers = []
            
            for row in res.data:
                config_raw = row.get("config")
                
                if isinstance(config_raw, str):
                    try:
                        config_data = json.loads(config_raw)
                    except:
                        continue
                else:
                    config_data = config_raw or {}

                servers = config_data.get("servers", [])
                for s in servers:
                    active_servers.append({
                        "name": s.get("name"),
                        "url": s.get("url"),
                        "api_key": s.get("apiKey"),
                        "transport": s.get("transport", "http")
                    })

            return active_servers

        except Exception as e:
            logger.error(f"Failed to fetch MCP servers for agent {agent_id}: {e}")
            return []

    # =========================================================
    # 4. TOOL AGGREGATION
    # =========================================================
    async def get_all_tools_schema(self, supabase, agent_id: str) -> List[Dict[str, Any]]:
        """Get all tools from active MCP servers and convert to OpenAI schema."""
        servers = await self._get_active_servers(supabase, agent_id)
        aggregated_tools = []

        logger.info(f"🔌 MCP: Discovering tools from {len(servers)} servers for Agent {agent_id}")

        for server in servers:
            url = server.get("url")
            api_key = server.get("api_key")
            transport = server.get("transport", "http")
            server_name = server.get("name", "mcp").replace(" ", "_").lower()
            
            try:
                if transport == "sse":
                    # Use MCP SDK session
                    session = await self._get_or_create_session(url, transport, api_key)
                    if not session:
                        logger.error(f"Failed to create session for {server_name}")
                        continue
                    
                    # List tools using SDK
                    tools_result = await session.list_tools()
                    mcp_tools = tools_result.tools if hasattr(tools_result, 'tools') else []
                    
                    # Convert to OpenAI schema
                    for tool in mcp_tools:
                        namespaced_name = f"{server_name}__{tool.name}"
                        
                        openai_tool = {
                            "type": "function",
                            "function": {
                                "name": namespaced_name,
                                "description": tool.description or "",
                                "parameters": tool.inputSchema if hasattr(tool, 'inputSchema') else {}
                            }
                        }
                        aggregated_tools.append(openai_tool)
                
                else:  # HTTP transport
                    response = await self._http_request(url, "tools/list", api_key=api_key)
                    
                    if response.get("success") and response.get("result"):
                        mcp_tools = response["result"].get("tools", [])
                        
                        for tool in mcp_tools:
                            namespaced_name = f"{server_name}__{tool['name']}"
                            
                            openai_tool = {
                                "type": "function",
                                "function": {
                                    "name": namespaced_name,
                                    "description": tool.get("description", ""),
                                    "parameters": tool.get("inputSchema", {})
                                }
                            }
                            aggregated_tools.append(openai_tool)
            
            except Exception as e:
                logger.error(f"Error listing tools from {server_name}: {e}")
                continue

        return aggregated_tools

    # =========================================================
    # 5. TOOL EXECUTION
    # =========================================================
    async def execute_mcp_tool(
        self, 
        supabase, 
        agent_id: str, 
        tool_call_name: str, 
        arguments: Dict
    ) -> Dict[str, Any]:
        """Execute MCP tool."""
        try:
            if "__" not in tool_call_name:
                return {"error": f"Invalid tool format: {tool_call_name}"}
            
            target_server_name, real_tool_name = tool_call_name.split("__", 1)
            
            servers = await self._get_active_servers(supabase, agent_id)
            
            target_server = None
            for s in servers:
                s_name = s.get("name", "").replace(" ", "_").lower()
                if s_name == target_server_name:
                    target_server = s
                    break
            
            if not target_server:
                return {"error": f"MCP Server '{target_server_name}' not found"}

            transport = target_server.get("transport", "http")
            
            logger.info(f"🚀 MCP: Executing {real_tool_name} on {target_server_name}...")
            
            if transport == "sse":
                # Use MCP SDK
                session = await self._get_or_create_session(
                    target_server["url"],
                    transport,
                    target_server["api_key"]
                )
                
                if not session:
                    return {"error": "Failed to create MCP session"}
                
                response = await self._sse_call_tool(session, real_tool_name, arguments)
                
            else:  # HTTP
                response = await self._http_request(
                    url=target_server["url"],
                    method="tools/call",
                    params={
                        "name": real_tool_name,
                        "arguments": arguments
                    },
                    api_key=target_server["api_key"],
                    timeout=30.0
                )

            if response.get("success"):
                result_content = response["result"].get("content", [])
                output_str = "\n".join([c.get("text", "") for c in result_content if c.get("type") == "text"])
                return {"status": "success", "output": output_str}
            else:
                return {"status": "error", "output": response.get("error")}

        except Exception as e:
            logger.error(f"MCP Execution Failed: {e}")
            return {"status": "error", "output": str(e)}

    # =========================================================
    # 6. RESOURCE FETCHING
    # =========================================================
    async def fetch_active_resources(self, supabase, agent_id: str) -> str:
        """Fetch resources from connected MCP servers."""
        servers = await self._get_active_servers(supabase, agent_id)
        context_str = []

        for server in servers:
            url = server.get("url")
            api_key = server.get("api_key")
            transport = server.get("transport", "http")
            server_name = server.get("name")

            try:
                if transport == "sse":
                    # Use MCP SDK
                    session = await self._get_or_create_session(url, transport, api_key)
                    if not session:
                        continue
                    
                    # List resources
                    resources_result = await session.list_resources()
                    resources = resources_result.resources if hasattr(resources_result, 'resources') else []
                    
                    # Read first 3
                    for res in resources[:3]:
                        read_result = await session.read_resource(res.uri)
                        for content in read_result.contents:
                            if hasattr(content, 'text'):
                                context_str.append(f"--- MCP Resource ({server_name}): {res.name} ---\n{content.text[:1000]}")
                
                else:  # HTTP
                    # List resources
                    list_resp = await self._http_request(url, "resources/list", api_key=api_key)
                    
                    if list_resp.get("success"):
                        resources = list_resp["result"].get("resources", [])
                        
                        for res in resources[:3]: 
                            uri = res.get("uri")
                            read_resp = await self._http_request(
                                url, "resources/read", 
                                params={"uri": uri}, 
                                api_key=api_key
                            )
                            
                            if read_resp.get("success"):
                                contents = read_resp["result"].get("contents", [])
                                for c in contents:
                                    context_str.append(f"--- MCP Resource ({server_name}): {res.get('name')} ---\n{c.get('text', '')[:1000]}")
            
            except Exception as e:
                logger.error(f"Error fetching resources from {server_name}: {e}")
                continue

        return "\n\n".join(context_str)

    # =========================================================
    # 7. CONNECTION TESTER
    # =========================================================
    async def test_connection(self, url: str, transport: str, api_key: str = None) -> Dict[str, Any]:
        """Test MCP server connection."""
        try:
            if transport.lower() == "sse":
                # Use MCP SDK to test SSE connection
                session = await self._get_or_create_session(url, "sse", api_key)
                
                if session:
                    # Test by listing tools
                    tools_result = await session.list_tools()
                    caps = ["tools"]
                    
                    # Check if resources are supported
                    try:
                        await session.list_resources()
                        caps.append("resources")
                    except:
                        pass
                    
                    return {
                        "success": True,
                        "status": "connected",
                        "capabilities": caps
                    }
                else:
                    return {"success": False, "status": "error", "error": "Failed to establish session"}
            
            else:  # HTTP
                res = await self._http_request(url, "initialize", params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "Syntra CRM", "version": "1.0"}
                }, api_key=api_key, timeout=5.0)
                
                if res.get("success"):
                    result = res.get("result") or {}
                    caps = list(result.get("capabilities", {}).keys())
                    
                    return {"success": True, "status": "connected", "capabilities": caps}
                else:
                    return {"success": False, "status": "error", "error": res.get("error")}
        
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return {"success": False, "status": "error", "error": str(e)}
    
    # =========================================================
    # 8. CLEANUP
    # =========================================================
    async def close_all_sessions(self):
        """Close all active MCP sessions."""
        for session_key in list(self._sessions.keys()):
            transport = session_key.split(":", 1)[0]
            url = session_key.split(":", 1)[1]
            await self._close_session(url, transport)

# Singleton
_mcp_service = MCPService()
def get_mcp_service(): return _mcp_service