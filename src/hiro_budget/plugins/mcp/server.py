"""The MCP protocol (JSON-RPC 2.0, one JSON object per line, UTF-8) over two byte streams.

Handled one request at a time. ``tools/call`` never raises: a mistake of the caller is a
tool result with ``isError``, a bug is logged and also reported that way, and the server
keeps running. Only ``plugin_api`` of the app is used.
"""

from __future__ import annotations

import json
import logging
from typing import Any, BinaryIO

from hiro_budget import plugin_api

log = logging.getLogger("hiro_budget.mcp")

# newest first; structured results (outputSchema, structuredContent) exist from 2025-06-18
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
STRUCTURED_FROM = "2025-06-18"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

INSTRUCTIONS = (
    "Hiro Budget: a family budget on the user's computer. Amounts are euros as strings with "
    "a dot, e.g. '-12.34' (negative = money out); arguments also accept '-12,34'. Months are "
    "YYYY-MM, dates YYYY-MM-DD. Categories are addressed by key 'group.category' or exact "
    "name: call list_categories before creating one. Bookings have numeric ids: find them "
    "with list_bookings (category 'uncategorized' lists what needs work). Every change "
    "except import_files and train is journaled and can be taken back with undo_change "
    "(one change, or all of this session with its session id from list_changes). A backup "
    "is made before the first change."
)

# changes that give the same result when repeated (MCP idempotentHint)
IDEMPOTENT = frozenset({"categorize_bookings", "assign_budget", "set_booking_note"})


def tool_annotations(op: plugin_api.OperationInfo) -> dict[str, Any]:
    """What a client needs to decide whether to ask the user: reads are read only;
    undoable changes are not destructive; the rest (import, train, undo) is."""
    note: dict[str, Any] = {"title": op.summary, "openWorldHint": False}
    if op.read_only:
        return {**note, "readOnlyHint": True}
    note["readOnlyHint"] = False
    note["destructiveHint"] = not op.undoable
    if op.name in IDEMPOTENT:
        note["idempotentHint"] = True
    return note


def describe(op: plugin_api.OperationInfo) -> str:
    text = f"{op.summary}. {op.description}"
    if not op.read_only:
        text += " Can be undone with undo_change." if op.undoable else " Cannot be undone."
    return text


class McpServer:
    def __init__(self, operations: plugin_api.Operations) -> None:
        self.operations = operations
        self.version = SUPPORTED_VERSIONS[0]  # until the client says
        self._tools: dict[str, plugin_api.OperationInfo] | None = None

    @property
    def tools(self) -> dict[str, plugin_api.OperationInfo]:
        if self._tools is None:
            self._tools = {op.name: op for op in self.operations.list()}
        return self._tools

    @property
    def structured(self) -> bool:
        return self.version >= STRUCTURED_FROM

    # -- the loop ----------------------------------------------------------------------

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            reply = self.handle_line(line)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode())
                stdout.write(b"\n")
                stdout.flush()

    def handle_line(self, line: bytes) -> Any:
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:  # includes bad UTF-8
            return _error(None, PARSE_ERROR, "Parse error")
        if isinstance(message, list):  # a batch (up to 2025-03-26)
            replies = [r for m in message if (r := self.handle(m)) is not None]
            return replies or None
        return self.handle(message)

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "id" in message and "method" not in message:
                return None  # a response to something we never asked
            return _error(_id(message), INVALID_REQUEST, "Invalid Request")
        method, params = message["method"], message.get("params") or {}
        is_request = "id" in message
        try:
            result = self.dispatch(method, params if isinstance(params, dict) else {})
        except _RpcError as e:
            return _error(message["id"], e.code, e.message) if is_request else None
        except Exception:
            log.exception("%s failed", method)
            return _error(message.get("id"), -32603, "Internal error") if is_request else None
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        match method:
            case "initialize":
                return self.initialize(params)
            case "ping":
                return {}
            case "tools/list":
                return {"tools": [self.tool(op) for op in self.tools.values()]}
            case "tools/call":
                return self.call_tool(params)
            case _ if method.startswith("notifications/"):
                return {}  # initialized, cancelled, ...: nothing to do
            case _:
                raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    # -- methods -----------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        wanted = params.get("protocolVersion")
        self.version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        log.info("client %s, protocol %s", params.get("clientInfo"), self.version)
        return {
            "protocolVersion": self.version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hiro-budget", "version": plugin_api.app_version()},
            "instructions": INSTRUCTIONS,
        }

    def tool(self, op: plugin_api.OperationInfo) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "name": op.name,
            "title": op.summary,
            "description": describe(op),
            "inputSchema": op.input_schema,
            "annotations": tool_annotations(op),
        }
        if self.structured:
            tool["outputSchema"] = op.output_schema
        return tool

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in self.tools:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        result = self.operations.call(name, arguments or {})
        reply: dict[str, Any] = {
            "content": [{"type": "text", "text": result.text}],
            "isError": result.error is not None,
        }
        if self.structured and result.error is None:
            reply["structuredContent"] = result.data
        return reply


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _id(message: Any) -> Any:
    return message.get("id") if isinstance(message, dict) else None


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
