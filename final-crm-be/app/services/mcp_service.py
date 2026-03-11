import logging
import httpx
import json
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MCPService:

    # =========================================================
    # 1. CORE HTTP CLIENT
    # =========================================================
    async def _rest_request(
        self,
        method: str,
        base_url: str,
        endpoint: str,
        payload: Optional[Dict] = None,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        full_url = f"{base_url.rstrip('/')}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        logger.info(f"🌐 [MCP] {method} {full_url}")

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = await client.get(full_url, headers=headers)
                else:
                    resp = await client.post(full_url, json=payload or {}, headers=headers)

            logger.info(f"📥 [MCP] {resp.status_code}")

            if resp.status_code != 200:
                logger.error(f"❌ [MCP] ERROR: {resp.text}")
                return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}

            return {"success": True, "data": resp.json()}

        except httpx.TimeoutException:
            logger.error(f"⏱️ [MCP] TIMEOUT: {full_url}")
            return {"success": False, "error": "Connection timed out"}
        except Exception as e:
            logger.error(f"💥 [MCP] EXCEPTION: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================
    # 2. SERVER DISCOVERY
    # =========================================================
    async def _get_active_servers(self, supabase, agent_id: str) -> List[Dict]:
        try:
            res = (
                supabase.table("agent_integrations")
                .select("config")
                .eq("agent_id", agent_id)
                .eq("channel", "mcp")
                .eq("enabled", True)
                .execute()
            )
            if not res.data:
                return []

            servers = []
            for row in res.data:
                config = row.get("config", {})
                if isinstance(config, str):
                    config = json.loads(config)
                for s in config.get("servers", []):
                    servers.append({
                        "name": s.get("name"),
                        "url":  s.get("url"),
                        "api_key": s.get("apiKey"),
                    })
            return servers
        except Exception as e:
            logger.error(f"[MCP] Failed to fetch servers for agent {agent_id}: {e}")
            return []

    # =========================================================
    # 3. SCHEMA → OPENAI TOOLS
    # =========================================================
    async def get_all_tools_schema(self, supabase, agent_id: str) -> List[Dict[str, Any]]:
        servers = await self._get_active_servers(supabase, agent_id)
        tools = []

        logger.info(f"🔌 [MCP] Discovering schema for Agent {agent_id}")

        for server in servers:
            server_name = server.get("name", "db").replace(" ", "_").lower()
            schema_resp = await self._rest_request("GET", server["url"], "/mcp/schema", api_key=server["api_key"])

            if not schema_resp.get("success"):
                logger.error(f"[MCP] Schema fetch failed for {server_name}: {schema_resp.get('error')}")
                continue

            data = schema_resp["data"]
            resources = data.get("resources", [])
            operators = data.get("supported_operators", ["=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "IS NULL", "IS NOT NULL"])

            for w in data.get("warnings", []):
                logger.warning(f"[MCP] Schema warning: {w}")

            if not resources:
                logger.warning(f"[MCP] No resources returned for '{server_name}'")
                continue

            for res in resources:
                res_name = res.get("name")
                if not res_name:
                    continue

                # Description comes from Palapa (TABLE_COMMENT or auto-generated)
                description = res.get("description", f"Table '{res_name}'.")

                # Append column info so AI knows exact names and types
                fields_info = []
                for f in res.get("fields", []):
                    raw = f.get("raw_type") or f.get("type", "")
                    pk  = " [PK]" if f.get("primary_key") else ""
                    fields_info.append(f"{f['name']} ({raw}{pk})")
                if fields_info:
                    description += f" Columns: {', '.join(fields_info)}."

                tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{server_name}__{res_name}",
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Columns to return. Leave empty to return all.",
                                },
                                "filters": {
                                    "type": "object",
                                    "description": f"Filter conditions. Key = column name. Value = {{\"op\": \"=\", \"value\": ...}}. Supported operators: {', '.join(operators)}.",
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "Number of rows to return. Default 100, max 1000.",
                                },
                                "order_by": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "field":     {"type": "string"},
                                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                                        },
                                    },
                                    "description": "Sort order. Example: [{\"field\": \"created_at\", \"direction\": \"desc\"}].",
                                },
                            },
                        },
                    },
                })

        logger.info(f"✅ [MCP] Mapped {len(tools)} tables to OpenAI tools.")
        return tools

    # =========================================================
    # 4. TOOL EXECUTION
    # =========================================================
    async def execute_mcp_tool(
        self,
        supabase,
        agent_id: str,
        tool_call_name: str,
        arguments: Dict,
    ) -> Dict[str, Any]:
        if "__" not in tool_call_name:
            return {"status": "error", "output": f"Invalid tool name: '{tool_call_name}'"}

        target_server_name, resource_name = tool_call_name.split("__", 1)
        servers = await self._get_active_servers(supabase, agent_id)

        server = next(
            (s for s in servers if s.get("name", "").replace(" ", "_").lower() == target_server_name),
            None,
        )
        if not server:
            return {"status": "error", "output": f"Server '{target_server_name}' not found."}

        logger.info(f"🔧 [MCP] Execute: {target_server_name} → {resource_name}")

        # Pass arguments as-is — no transformation, no injection
        payload: Dict[str, Any] = {
            "resource": resource_name,
            "fields":   arguments.get("fields", []),
            "limit":    arguments.get("limit", 100),
        }
        if arguments.get("filters"):
            payload["filters"] = arguments["filters"]
        if arguments.get("order_by"):
            payload["order_by"] = arguments["order_by"]

        response = await self._rest_request(
            method="POST",
            base_url=server["url"],
            endpoint="/mcp/execute",
            payload=payload,
            api_key=server["api_key"],
        )

        if response.get("success"):
            return {"status": "success", "output": json.dumps(response["data"])}
        return {"status": "error", "output": response.get("error", "Unknown error")}

    # =========================================================
    # 5. CONNECTION TEST
    # =========================================================
    async def test_connection(self, url: str, transport: str = "http", api_key: str = None) -> Dict[str, Any]:
        response = await self._rest_request("GET", url, "/mcp/resources", api_key=api_key, timeout=5.0)

        if response.get("success"):
            data = response["data"]
            resources = data if isinstance(data, list) else data.get("resources", [])
            logger.info(f"✅ [MCP] Connected. Found {len(resources)} tables.")
            return {
                "success":      True,
                "status":       "connected",
                "capabilities": ["resources/read", "tools/list"],
                "tools_count":  len(resources),
            }

        logger.error(f"❌ [MCP] Connection failed: {response.get('error')}")
        return {
            "success": False,
            "status":  "error",
            "error":   response.get("error", "Invalid Palapa AI endpoint"),
        }


# Singleton
_mcp_service = MCPService()


def get_mcp_service() -> MCPService:
    return _mcp_service