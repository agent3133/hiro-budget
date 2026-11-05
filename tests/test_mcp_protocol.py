"""The MCP server (#84), in process: JSON lines in, JSON lines out."""

from __future__ import annotations

import io
import json
import sys

import pytest

from hiro_budget import plugin_api
from hiro_budget.app import App
from hiro_budget.ops import OpContext, registry
from hiro_budget.plugins.mcp.server import McpServer, tool_annotations


@pytest.fixture
def operations(app) -> plugin_api.Operations:
    return plugin_api.Operations(OpContext(App.from_config_dir(app.settings.config_dir), "mcp"))


def talk(operations, *messages, version="2025-06-18", raw: list[bytes] | None = None):
    """Send the messages (dicts, or raw lines) after an initialize; the replies by id."""
    lines = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}},
        },
        *messages,
    ]
    data = b"".join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in lines)
    out = io.BytesIO()
    server = McpServer(operations)
    server.serve(io.BytesIO(data), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    return {r.get("id"): r for r in replies if isinstance(r, dict)}, replies


def patch_run(monkeypatch, op_name, run):
    """Replace what one operation does (the registry is left as it is)."""
    from dataclasses import replace

    from hiro_budget import ops

    original_find = ops.find
    monkeypatch.setattr(
        ops,
        "find",
        lambda name: (
            replace(original_find(name), run=run) if name == op_name else original_find(name)
        ),
    )


def request(i, method, **params):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params}


def test_initialize_negotiates_the_version(operations):
    for asked, answered in (
        ("2025-06-18", "2025-06-18"),
        ("2025-03-26", "2025-03-26"),
        ("2024-11-05", "2024-11-05"),
        ("2099-01-01", "2025-06-18"),  # unknown: the newest we have
    ):
        replies, _ = talk(operations, version=asked)
        result = replies[0]["result"]
        assert result["protocolVersion"] == answered
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == "hiro-budget"
        assert "undo_change" in result["instructions"] and "'-12.34'" in result["instructions"]


def test_tools_list_has_every_operation(operations):
    replies, _ = talk(operations, request(1, "tools/list"))
    tools = replies[1]["result"]["tools"]
    assert [t["name"] for t in tools] == [o.name for o in registry()]
    for tool in tools:
        assert tool["description"] and tool["title"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(tool)
        assert "$ref" not in json.dumps(tool["outputSchema"])
        assert "cli" not in json.dumps(schema)  # no command line hints


def test_old_protocol_versions_get_no_output_schema(operations):
    replies, _ = talk(operations, request(1, "tools/list"), version="2025-03-26")
    assert all("outputSchema" not in t for t in replies[1]["result"]["tools"])
    out, _ = talk(
        operations,
        request(2, "tools/call", name="list_categories", arguments={}),
        version="2025-03-26",
    )
    assert "structuredContent" not in out[2]["result"]


def test_annotations_per_tool(operations):
    replies, _ = talk(operations, request(1, "tools/list"))
    note = {t["name"]: t["annotations"] for t in replies[1]["result"]["tools"]}
    for name in ("list_bookings", "get_budget", "report_monthly", "list_changes"):
        assert note[name]["readOnlyHint"] is True and "destructiveHint" not in note[name]
    for name in ("categorize_bookings", "assign_budget", "delete_manual_booking", "add_rule"):
        assert note[name]["readOnlyHint"] is False and note[name]["destructiveHint"] is False
    for name in ("categorize_bookings", "assign_budget", "set_booking_note"):
        assert note[name]["idempotentHint"] is True
    assert "idempotentHint" not in note["split_booking"]
    for name in ("import_files", "train", "undo_change"):
        assert note[name]["readOnlyHint"] is False and note[name]["destructiveHint"] is True
    assert all(n["openWorldHint"] is False for n in note.values())


def test_a_read_call_returns_text_and_structured_content(operations):
    replies, _ = talk(operations, request(1, "tools/call", name="list_categories", arguments={}))
    result = replies[1]["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text" and "wohnen" in result["content"][0]["text"]
    keys = [c["key"] for g in result["structuredContent"]["groups"] for c in g["categories"]]
    assert "lebensmittel.supermarkt" in keys


def test_a_write_and_its_undo(operations):
    replies, _ = talk(
        operations,
        request(
            1,
            "tools/call",
            name="create_manual_booking",
            arguments={
                "account": "Bargeld",
                "booking_date": "2026-03-01",
                "amount": "-9,99",
                "payee": "Kiosk",
            },
        ),
        request(2, "tools/call", name="list_changes", arguments={"limit": 5}),
    )
    assert replies[1]["result"]["isError"] is False
    changes = replies[2]["result"]["structuredContent"]["changes"]
    assert changes[0]["source"] == "mcp" and changes[0]["undoable"]
    session = changes[0]["session"]
    replies, _ = talk(
        operations,
        request(1, "tools/call", name="undo_change", arguments={"session": session}),
        request(2, "tools/call", name="list_bookings", arguments={"text": "Kiosk"}),
    )
    assert replies[1]["result"]["isError"] is False
    assert replies[2]["result"]["structuredContent"]["total"] == 0


def test_mistakes_of_the_caller_are_tool_errors(operations):
    replies, _ = talk(
        operations,
        request(1, "tools/call", name="list_bookings", arguments={"limit": 0}),
        request(2, "tools/call", name="list_bookings", arguments={"category": "nonsense"}),
        request(3, "tools/call", name="get_booking", arguments={"id": "x"}),
    )
    for i in (1, 2, 3):
        result = replies[i]["result"]
        assert result["isError"] is True and result["content"][0]["text"]
        assert "structuredContent" not in result
    assert "Did you mean" in replies[2]["result"]["content"][0]["text"]
    assert "invalid arguments" in replies[1]["result"]["content"][0]["text"]


def test_protocol_errors(operations):
    replies, all_ = talk(
        operations,
        request(1, "nope/method"),
        request(2, "tools/call", name="no_such_tool", arguments={}),
        request(3, "tools/call", name="list_bookings", arguments=[1]),
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        b"this is not json",
        b"\xff\xfe not utf-8",
        {"jsonrpc": "2.0", "id": 5},  # a response to nothing we asked: no reply
        {"jsonrpc": "2.0", "params": {}},  # neither a request nor a response
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no reply
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "method": "unknown/notification"},  # unknown, but no id: silent
    )
    assert replies[1]["error"]["code"] == -32601
    assert (
        replies[2]["error"]["code"] == -32602 and "Unknown tool" in replies[2]["error"]["message"]
    )
    assert replies[3]["error"]["code"] == -32602
    assert replies[4]["result"] == {}
    unanswered = [r["error"]["code"] for r in all_ if r.get("id") is None]
    assert unanswered == [-32700, -32700, -32600]  # two bad lines, one invalid request
    assert 5 not in replies
    assert len(all_) == 1 + 4 + 3  # initialize, four requests, three errors without an id


def test_a_batch_is_answered_as_a_batch(operations):
    _, all_ = talk(
        operations,
        [request(1, "ping"), request(2, "ping"), {"jsonrpc": "2.0", "method": "notifications/x"}],
    )
    batch = all_[1]
    assert [r["id"] for r in batch] == [1, 2]


def test_a_bug_in_an_operation_does_not_stop_the_server(operations, monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("kaboom")

    patch_run(monkeypatch, "list_categories", boom)
    replies, _ = talk(
        operations,
        request(1, "tools/call", name="list_categories", arguments={}),
        request(2, "ping"),
    )
    result = replies[1]["result"]
    assert result["isError"] is True and "kaboom" in result["content"][0]["text"]
    assert replies[2]["result"] == {}  # still serving


def test_a_stray_print_does_not_reach_the_protocol_stream(operations, monkeypatch, capsys):
    """The frontend points stdout at stderr; whatever prints meanwhile stays out of the stream."""
    from hiro_budget import ops
    from hiro_budget.plugins.mcp.frontend import McpFrontend

    original = ops.find("list_categories").run

    def noisy(ctx, args):
        print("a stray print")
        return original(ctx, args)

    patch_run(monkeypatch, "list_categories", noisy)
    protocol = io.BytesIO()
    stdout = io.TextIOWrapper(protocol, write_through=True)  # kept: closing it closes protocol
    stdin = io.TextIOWrapper(
        io.BytesIO(
            json.dumps(request(1, "tools/call", name="list_categories", arguments={})).encode()
            + b"\n"
        )
    )
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stdin", stdin)

    class Args:
        read_only = False

    McpFrontend().run(Args(), operations)
    out = capsys.readouterr()
    assert "a stray print" in out.err  # went to stderr
    lines = protocol.getvalue().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == 1


def test_a_bug_is_written_to_the_log_file(app, monkeypatch):
    """``hiro-budget mcp`` logs to the app's log file, traceback included: the stderr of a
    server started by an agent is often not kept (#87)."""
    from hiro_budget import logfile
    from hiro_budget.__main__ import main

    def boom(ctx, args):
        raise RuntimeError("kaboom in the log")

    patch_run(monkeypatch, "list_categories", boom)
    line = json.dumps(request(1, "tools/call", name="list_categories", arguments={}))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(line.encode() + b"\n")))
    stdout = io.TextIOWrapper(io.BytesIO(), write_through=True)  # kept: closing it closes it
    monkeypatch.setattr(sys, "stdout", stdout)
    try:
        assert main(["--config-dir", str(app.settings.config_dir), "mcp"]) == 0
        path = logfile.log_path()
    finally:
        logfile.close_log_file()
    assert path == (app.settings.data_dir / logfile.LOG_FOLDER / logfile.LOG_FILE).resolve()
    text = path.read_text(encoding="utf-8")
    assert "operation list_categories failed" in text
    assert "Traceback" in text and "kaboom in the log" in text


def test_read_only_server_offers_and_runs_reads_only(app):
    ops_ = plugin_api.Operations(
        OpContext(App.from_config_dir(app.settings.config_dir), "mcp"), read_only=True
    )
    replies, _ = talk(
        ops_,
        request(1, "tools/list"),
        request(2, "tools/call", name="set_booking_note", arguments={"id": 1, "text": "x"}),
    )
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert "list_bookings" in names and "categorize_bookings" not in names
    assert replies[2]["error"]["code"] == -32602  # not offered: unknown tool


def test_annotation_rules_directly():
    def info(name, read_only, undoable):
        return plugin_api.OperationInfo(name, "s", "d", {}, {}, read_only, undoable)

    assert tool_annotations(info("x", True, False))["readOnlyHint"] is True
    assert tool_annotations(info("x", False, True))["destructiveHint"] is False
    assert tool_annotations(info("x", False, False))["destructiveHint"] is True
