"""JSON-schema-native Strands tool adapter.

A decorated **kwargs function has a different Pydantic signature from an MCP
schema. AgentTool's stream contract avoids that second, incompatible validator.
The SDK event wrapper is also used by its own MCPAgentTool implementation.
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from jsonschema import Draft202012Validator
from strands.types.tools import AgentTool
from strands.types._events import ToolResultEvent


class SchemaMcpTool(AgentTool):
    def __init__(self, session: Any, descriptor: Any) -> None:
        super().__init__()
        self.session = session
        self.descriptor = descriptor
        schema = copy.deepcopy(descriptor.input_schema)
        # Provider schemas are data, not authority to fetch remote references.
        def check_refs(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                        raise ValueError("Remote schema references are not supported")
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)
        check_refs(schema)
        Draft202012Validator.check_schema(schema)
        self._schema = schema
        self._validator = Draft202012Validator(schema)

    @property
    def tool_name(self) -> str:
        return self.descriptor.name

    @property
    def tool_type(self) -> str:
        return "mcp"

    @property
    def tool_spec(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (self.descriptor.description + " Read-only external evidence through the current user's GitHub connection.")[:1600],
            "inputSchema": {"json": copy.deepcopy(self._schema)},
        }

    def __call__(self, **arguments: Any) -> dict[str, Any]:
        try:
            normalized = self.session._normalize_invocation_arguments(self.descriptor, arguments)
            error = next(self._validator.iter_errors(normalized), None)
            if error is not None:
                # Never include instance values in validation failures or traces.
                raise ValueError(f"GitHub MCP input does not match the advertised schema ({error.validator})")
        except (TypeError, ValueError) as exc:
            self.session.record_input_failure(self.tool_name, exc)
            raise
        return self.session.call(self.tool_name, normalized)

    async def stream(self, tool_use: dict[str, Any], invocation_state: dict[str, Any], **kwargs: Any):
        with self.session._lock:
            self.session._sdk_stream_attempts += 1
        tool_use_id = str(tool_use.get("toolUseId", "unknown"))
        try:
            arguments = tool_use.get("input", {})
            if not isinstance(arguments, dict):
                error = ValueError("GitHub MCP input must be an object")
                self.session.record_input_failure(self.tool_name, error)
                raise error
            result = await asyncio.to_thread(self, **arguments)
            payload = {"toolUseId": tool_use_id, "status": "success", "content": [{"text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            from archbro.backend.mcp.agent_tools import _safe_error
            payload = {"toolUseId": tool_use_id, "status": "error", "content": [{"text": _safe_error(exc)}]}
        yield ToolResultEvent(payload)
