"""The MCP server against the official SDK's client (#84): a real process over stdio.

The ``mcp`` package is a development dependency only; the app and its exe do not contain it."""

from __future__ import annotations

import asyncio
import sys

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def server(config_dir, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "hiro_budget", "--config-dir", str(config_dir), "mcp", *extra],
    )


async def session_for(params, work):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 60)
            return await asyncio.wait_for(work(session), 60)


def run(params, work):
    return asyncio.run(session_for(params, work))


def test_initialize_list_call_write_and_undo(config_dir):
    async def work(session):
        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        assert {"list_categories", "categorize_bookings", "undo_change", "import_files"} <= names
        by_name = {t.name: t for t in tools}
        assert by_name["list_bookings"].annotations.read_only_hint is True
        assert by_name["categorize_bookings"].annotations.destructive_hint is False
        assert by_name["import_files"].annotations.destructive_hint is True

        cats = await session.call_tool("list_categories", {})
        assert not cats.is_error
        keys = [c["key"] for g in cats.structured_content["groups"] for c in g["categories"]]
        assert "lebensmittel.supermarkt" in keys

        added = await session.call_tool(
            "create_manual_booking",
            {
                "account": "Bargeld",
                "booking_date": "2026-03-01",
                "amount": "-9,99",
                "payee": "Kiosk",
            },
        )
        assert not added.is_error, added.content
        (tx_id,) = added.structured_content["ids"]
        done = await session.call_tool(
            "categorize_bookings", {"ids": [tx_id], "category": "lebensmittel.supermarkt"}
        )
        assert not done.is_error

        changes = await session.call_tool("list_changes", {})
        session_id = changes.structured_content["changes"][0]["session"]
        undone = await session.call_tool("undo_change", {"session": session_id})
        assert not undone.is_error, undone.content
        left = await session.call_tool("list_bookings", {"text": "Kiosk"})
        assert left.structured_content["total"] == 0

        bad = await session.call_tool("list_bookings", {"category": "nonsense"})
        assert bad.is_error and "Did you mean" in bad.content[0].text
        return True

    assert run(server(config_dir), work)


def test_read_only_server(config_dir):
    async def work(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert "list_bookings" in names
        assert not names & {"categorize_bookings", "import_files", "undo_change"}
        return True

    assert run(server(config_dir, "--read-only"), work)


def test_the_instructions_reach_the_client(config_dir):
    async def work(session):
        return session.initialize_result.instructions

    text = run(server(config_dir), work)
    assert "undo_change" in text
