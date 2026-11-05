"""``hiro-budget mcp``: the MCP server on stdin and stdout."""

from __future__ import annotations

import argparse
import sys

from hiro_budget import plugin_api


class McpFrontend:
    command = "mcp"
    help = "MCP server over stdio for LLM agents (the Hiro agent, ...)"
    source = "mcp"  # changes are journaled as made by "mcp"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--read-only",
            action="store_true",
            help="offer only the tools that do not change anything (a safe first try)",
        )

    def run(self, args: argparse.Namespace, operations: plugin_api.Operations) -> int:
        from .server import McpServer, log

        operations.read_only = args.read_only

        # The protocol owns stdout: whatever else prints (a library, a stray print) goes to
        # stderr, so it cannot corrupt the stream.
        protocol_out = sys.stdout.buffer
        sys.stdout = sys.stderr
        log.info("session %s%s", operations.session_id, " (read only)" if args.read_only else "")
        try:
            McpServer(operations).serve(sys.stdin.buffer, protocol_out)
        except KeyboardInterrupt:
            pass
        return 0
