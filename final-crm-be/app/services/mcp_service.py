import logging
import httpx
import asyncio
from typing import Dict, Any

logger = logging.getLogger(__name__)

class MCPService:
    async def test_connection(self, url: str, transport: str, api_key: str = None) -> Dict[str, Any]:
        """
        Tests connection to an MCP server via HTTP/SSE.
        Enforces a strict 5-second timeout.
        """
        try:
            # 1. HTTP Transport Logic
            if transport.lower() == "http":
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Syntra-CRM-Backend/1.0"
                }
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                # JSON-RPC 2.0 Initialize Payload
                payload = {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "Syntra CRM", "version": "1.0"}
                    },
                    "id": 1
                }

                async with httpx.AsyncClient(timeout=5.0) as client:
                    start_time = asyncio.get_event_loop().time()
                    resp = await client.post(url, json=payload, headers=headers)
                    latency = (asyncio.get_event_loop().time() - start_time) * 1000

                    if resp.status_code == 200:
                        data = resp.json()
                        result = data.get("result", {})
                        server_caps = result.get("capabilities", {})
                        caps_list = list(server_caps.keys())

                        return {
                            "success": True,
                            "status": "connected",
                            "capabilities": caps_list,
                            "latency_ms": int(latency)
                        }
                    else:
                        return {
                            "success": False,
                            "status": "error",
                            "error": f"HTTP {resp.status_code}: {resp.text[:100]}"
                        }

            # 2. SSE Transport Logic (Basic Handshake)
            elif transport.lower() == "sse":
                async with httpx.AsyncClient(timeout=5.0) as client:
                     start_time = asyncio.get_event_loop().time()
                     # Connect to SSE endpoint to check if it opens
                     async with client.stream("GET", url) as response:
                        latency = (asyncio.get_event_loop().time() - start_time) * 1000
                        if response.status_code == 200:
                            return {
                                "success": True, 
                                "status": "connected",
                                "capabilities": ["sse/stream"],
                                "latency_ms": int(latency)
                            }
                        else:
                            return {"success": False, "status": "error", "error": f"SSE Failed: {response.status_code}"}

            else:
                return {"success": False, "status": "error", "error": "Unsupported transport type"}

        except httpx.TimeoutException:
            return {"success": False, "status": "timeout", "error": "Connection timed out (5s limit)"}
        except Exception as e:
            logger.error(f"MCP Test Error: {e}")
            return {"success": False, "status": "error", "error": str(e)}

# Singleton
_mcp_service = MCPService()
def get_mcp_service(): return _mcp_service