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
        """Raw HTTP request to a Palapa AI endpoint."""
        full_url = f"{base_url.rstrip('/')}{endpoint}"

        # Palapa only uses X-API-Key — Bearer is not needed
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        logger.info(f"🌐 [REST-PROXY] OUTBOUND {method} {full_url}")

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = await client.get(full_url, headers=headers)
                else:
                    resp = await client.post(full_url, json=payload or {}, headers=headers)

            logger.info(f"📥 [REST-PROXY] RESPONSE CODE: {resp.status_code}")

            if resp.status_code != 200:
                logger.error(f"❌ [REST-PROXY] ERROR BODY: {resp.text}")
                return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}

            data = resp.json()
            return {"success": True, "data": data}

        except httpx.TimeoutException:
            logger.error(f"⏱️ [REST-PROXY] TIMEOUT calling {full_url}")
            return {"success": False, "error": "Connection timed out"}
        except Exception as e:
            logger.error(f"💥 [REST-PROXY] NETWORK EXCEPTION: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================
    # 2. SERVER DISCOVERY
    # =========================================================
    async def _get_active_servers(self, supabase, agent_id: str) -> List[Dict]:
        """Fetch active MCP servers for an agent from agent_integrations."""
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

            active_servers = []
            for row in res.data:
                config_raw = row.get("config", {})
                config_data = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
                for s in config_data.get("servers", []):
                    active_servers.append({
                        "name": s.get("name"),
                        "url": s.get("url"),
                        "api_key": s.get("apiKey"),
                    })
            return active_servers
        except Exception as e:
            logger.error(f"Failed to fetch servers for agent {agent_id}: {e}")
            return []

    # =========================================================
    # 3. SCHEMA → OPENAI TOOLS
    # =========================================================
    async def get_all_tools_schema(self, supabase, agent_id: str) -> List[Dict[str, Any]]:
        """
        Calls GET /mcp/schema (always fresh — bypasses cache) and maps
        resources + relationships into OpenAI function definitions.

        Real Palapa /mcp/initialize response shape (for reference):
            {
                "resources": [
                    {
                        "name": "orders",
                        "type": "table",
                        "fields": [
                            {"name": "id", "type": "integer", "primary_key": true, "nullable": false},
                            ...
                        ],
                        "foreign_keys": [...],
                        "indexes": [...],
                        "access": "read"
                    }
                ]
            }

        GET /mcp/schema adds a top-level "relationships" array derived from FK metadata.
        It does NOT read from the schema cache — always introspects the DB fresh.
        """
        servers = await self._get_active_servers(supabase, agent_id)
        aggregated_tools = []

        logger.info(f"🔌 [REST-PROXY] Discovering schema for Agent {agent_id}")

        for server in servers:
            url = server.get("url")
            api_key = server.get("api_key")
            server_name = server.get("name", "db").replace(" ", "_").lower()

            # /mcp/schema always returns fresh data + relationships in one call.
            # No need to call /mcp/initialize first — it does not affect /mcp/schema.
            schema_resp = await self._rest_request("GET", url, "/mcp/schema", api_key=api_key)

            if not schema_resp.get("success"):
                logger.error(f"⚠️ [REST-PROXY] Schema fetch failed for {server_name}: {schema_resp.get('error')}")
                continue

            data = schema_resp["data"]

            # Log the real raw output so you can verify the server response
            logger.info(
                f"📦 [REST-PROXY] RAW /mcp/schema from '{server_name}':\n"
                + json.dumps(data, indent=2)
            )

            resources = data.get("resources", [])
            relationships = data.get("relationships", [])

            if not resources:
                logger.warning(f"⚠️ [REST-PROXY] No resources returned for '{server_name}'.")
                continue

            # Build relationship lookup: table_name → [human-readable join hints]
            rel_map: Dict[str, List[str]] = {}
            for rel in relationships:
                frm = rel.get("from_resource")
                to = rel.get("to_resource")
                if not frm or not to:
                    continue
                rel_map.setdefault(frm, []).append(
                    f"Can join to `{to}` via `{frm}.{rel['from_column']} = {to}.{rel['to_column']}`"
                )
                rel_map.setdefault(to, []).append(
                    f"Can join from `{frm}` via `{frm}.{rel['from_column']} = {to}.{rel['to_column']}`"
                )

            for res in resources:
                res_name = res.get("name")
                if not res_name:
                    continue

                pks = [f["name"] for f in res.get("fields", []) if f.get("primary_key")]
                indexed_cols = list({
                    col
                    for idx in res.get("indexes", [])
                    for col in idx.get("columns", [])
                })

                desc_parts = [f"Query the '{res_name}' table from '{server_name}'."]
                if pks:
                    desc_parts.append(f"Primary Key(s): {', '.join(pks)}.")
                if indexed_cols:
                    desc_parts.append(f"Indexed columns (prefer for filters): {', '.join(indexed_cols)}.")
                if res_name in rel_map:
                    desc_parts.append("Relationships: " + " | ".join(rel_map[res_name]) + ".")
                desc_parts.append(
                    "Do NOT use JOINs. Query one table at a time; use 'IN' filters to link results."
                )

                aggregated_tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{server_name}__{res_name}",
                        "description": " ".join(desc_parts),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Columns to return. Omit or leave empty to return all.",
                                },
                                "filters": {
                                    "type": "object",
                                    "description": (
                                        "Filter conditions. Key = column name. "
                                        "Value = {\"op\": \"=\", \"value\": ...}. "
                                        "Operators: =, !=, >, <, >=, <=, LIKE, IN, IS NULL, IS NOT NULL."
                                    ),
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "Rows to return (default 50, max 1000).",
                                },
                                "order_by": {
                                    "type": "object",
                                    "properties": {
                                        "field": {"type": "string"},
                                        "direction": {"type": "string", "enum": ["asc", "desc"]},
                                    },
                                },
                            },
                        },
                    },
                })

        logger.info(f"✅ [REST-PROXY] Mapped {len(aggregated_tools)} tables to OpenAI tools.")
        return aggregated_tools

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
        """Translates an OpenAI tool call into a Palapa POST /mcp/execute request."""
        if "__" not in tool_call_name:
            return {"status": "error", "output": f"Invalid tool name format: '{tool_call_name}'"}

        target_server_name, resource_name = tool_call_name.split("__", 1)
        servers = await self._get_active_servers(supabase, agent_id)

        target_server = next(
            (s for s in servers if s.get("name", "").replace(" ", "_").lower() == target_server_name),
            None,
        )
        if not target_server:
            return {"status": "error", "output": f"Server '{target_server_name}' not found."}

        logger.info(f"🔧 [REST-PROXY] Executing: {target_server_name} → {resource_name}")

        payload: Dict[str, Any] = {
            "resource": resource_name,
            "fields": arguments.get("fields", []),
            "limit": arguments.get("limit", 50),
        }
        if arguments.get("filters"):
            payload["filters"] = arguments["filters"]
        if arguments.get("order_by"):
            payload["order_by"] = arguments["order_by"]

        response = await self._rest_request(
            method="POST",
            base_url=target_server["url"],
            endpoint="/mcp/execute",
            payload=payload,
            api_key=target_server["api_key"],
        )

        if response.get("success"):
            return {"status": "success", "output": json.dumps(response["data"])}
        return {"status": "error", "output": response.get("error", "Unknown error")}

    # =========================================================
    # 5. CONNECTION TEST
    # =========================================================
    async def test_connection(self, url: str, transport: str = "http", api_key: str = None) -> Dict[str, Any]:
        """
        Tests connectivity by calling GET /mcp/resources.
        Accepts 'transport' to satisfy the crm_agents.py caller, even though Palapa is purely HTTP.
        """
        logger.info(f"🧪 [REST-PROXY] Testing connection to {url}")
        response = await self._rest_request("GET", url, "/mcp/resources", api_key=api_key, timeout=5.0)

        if response.get("success"):
            data = response["data"]
            resources = data if isinstance(data, list) else data.get("resources", [])
            logger.info(f"✅ [REST-PROXY] Connected. Found {len(resources)} tables.")
            return {
                "success": True,
                "status": "connected",
                "capabilities": ["resources/read", "tools/list"],
                "tools_count": len(resources),
            }

        logger.error(f"❌ [REST-PROXY] Connection failed: {response.get('error')}")
        return {
            "success": False,
            "status": "error",
            "error": response.get("error", "Invalid Palapa AI endpoint"),
        }


# Singleton
_mcp_service = MCPService()


def get_mcp_service() -> MCPService:
    return _mcp_service
