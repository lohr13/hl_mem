"""Official MCP SDK transport runtime for HL-Mem."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, Sequence

import anyio
import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from hl_mem import __version__
from hl_mem.config_loader import load_settings
from hl_mem.errors import HlMemError
from hl_mem.mcp.server import McpMemoryServer, get_tool_schemas


class MemoryToolBackend(Protocol):
    """Synchronous business boundary consumed by the MCP runtime."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def _sdk_tools() -> list[types.Tool]:
    """Build SDK tool objects from the existing canonical JSON Schemas."""
    return [
        types.Tool(
            name=schema["name"],
            description=schema.get("description"),
            input_schema=schema["inputSchema"],
            output_schema=schema.get("outputSchema"),
        )
        for schema in get_tool_schemas()
    ]


def create_mcp_server(memory: MemoryToolBackend) -> Server[dict[str, Any]]:
    """Adapt the synchronous HL-Mem tool contract to an MCP 2.x Server."""

    async def list_tools(
        _context: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_sdk_tools())

    async def call_tool(
        _context: ServerRequestContext[dict[str, Any]],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        try:
            result = await anyio.to_thread.run_sync(memory.call_tool, params.name, params.arguments or {})
        except HlMemError as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error))],
                is_error=True,
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, sort_keys=True),
                )
            ],
            structured_content=result,
        )

    return Server(
        "hl-mem",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(memory: MemoryToolBackend) -> None:
    """Serve HL-Mem over MCP's stdio transport until the peer disconnects."""
    server = create_mcp_server(memory)
    async with stdio_server() as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


def main(argv: Sequence[str] | None = None) -> None:
    """Load one Settings snapshot and start the MCP stdio runtime."""
    parser = argparse.ArgumentParser(prog="hl-mem-mcp")
    parser.add_argument("--version", action="version", version=f"hl_mem MCP {__version__}")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)

    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=str(args.db))
    memory = McpMemoryServer(settings)
    try:
        anyio.run(run_stdio, memory)
    finally:
        memory.database.close()
