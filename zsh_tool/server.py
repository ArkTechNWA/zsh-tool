#!/usr/bin/env python3
"""
Zsh Tool MCP Server
===================
Full-parity zsh execution with NEVERHANG circuit breaker and A.L.A.N. learning.

For Johnny5. For us.
"""

import anyio
import asyncio
import shutil

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .config import (
    NEVERHANG_TIMEOUT_DEFAULT,
    NEVERHANG_TIMEOUT_MAX,
    YIELD_AFTER_DEFAULT,
)
from .neverhang import CircuitState
from .tasks import (
    alan,
    circuit_breaker,
    live_tasks,
    execute_zsh_pty,
    execute_zsh_yielding,
    poll_task,
    send_to_task,
    kill_task,
    list_tasks,
)


# =============================================================================
# MCP Server (using official SDK)
# =============================================================================

server = Server("zsh-tool")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="zsh",
            description="Execute a zsh command with yield-based oversight. Returns after yield_after seconds with partial output if still running. Use zsh_poll to continue collecting output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The zsh command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Max execution time in seconds (default: {NEVERHANG_TIMEOUT_DEFAULT}, max: {NEVERHANG_TIMEOUT_MAX})"
                    },
                    "yield_after": {
                        "type": "number",
                        "description": f"Return control after this many seconds if still running (default: {YIELD_AFTER_DEFAULT})"
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what this command does"
                    },
                    "pty": {
                        "type": "boolean",
                        "description": "Use PTY (pseudo-terminal) mode for full terminal emulation. Enables proper handling of interactive prompts, colors, and programs that require a TTY."
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="zsh_poll",
            description="Get more output from a running task. Call repeatedly until status is not 'running'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned from zsh command"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="zsh_send",
            description="Send input to a running task's stdin. Use for interactive commands that need input.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID of the running command"
                    },
                    "input": {
                        "type": "string",
                        "description": "Text to send to stdin (newline added automatically)"
                    }
                },
                "required": ["task_id", "input"]
            }
        ),
        Tool(
            name="zsh_kill",
            description="Kill a running task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to kill"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="zsh_tasks",
            description="List all active tasks with their status.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_health",
            description="Get health status of zsh-tool including NEVERHANG and A.L.A.N. status",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_alan_stats",
            description="Get A.L.A.N. learning database statistics",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_alan_query",
            description="Query A.L.A.N. for insights about a command pattern",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to query pattern stats for"
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="zsh_neverhang_status",
            description="Get NEVERHANG circuit breaker status",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_neverhang_reset",
            description="Reset NEVERHANG circuit breaker to closed state",
            inputSchema={"type": "object", "properties": {}}
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    import json

    # Protect against MCP abort - wrap entire handler
    try:
        return await _handle_tool_call(name, arguments)
    except asyncio.CancelledError:
        # MCP aborted - return graceful error instead of propagating
        return [TextContent(
            type="text",
            text=json.dumps({
                'success': False,
                'error': 'MCP call was cancelled',
                'hint': 'Use zsh_tasks to check for running tasks'
            }, indent=2)
        )]


async def _handle_tool_call(name: str, arguments: dict) -> list[TextContent]:
    """Internal tool call handler."""
    import json

    if name == "zsh":
        use_pty = arguments.get("pty", False)
        if use_pty:
            result = await execute_zsh_pty(
                command=arguments["command"],
                timeout=arguments.get("timeout", NEVERHANG_TIMEOUT_DEFAULT),
                yield_after=arguments.get("yield_after", YIELD_AFTER_DEFAULT),
                description=arguments.get("description")
            )
        else:
            result = await execute_zsh_yielding(
                command=arguments["command"],
                timeout=arguments.get("timeout", NEVERHANG_TIMEOUT_DEFAULT),
                yield_after=arguments.get("yield_after", YIELD_AFTER_DEFAULT),
                description=arguments.get("description")
            )
        return _format_task_output(result)
    elif name == "zsh_poll":
        result = await poll_task(arguments["task_id"])
        return _format_task_output(result)
    elif name == "zsh_send":
        result = await send_to_task(arguments["task_id"], arguments["input"])
    elif name == "zsh_kill":
        result = await kill_task(arguments["task_id"])
    elif name == "zsh_tasks":
        result = list_tasks()
    elif name == "zsh_health":
        result = {
            'status': 'healthy',
            'neverhang': circuit_breaker.get_status(),
            'alan': alan.get_stats(),
            'active_tasks': len(live_tasks)
        }
    elif name == "zsh_alan_stats":
        result = alan.get_stats()
    elif name == "zsh_alan_query":
        result = alan.get_pattern_stats(arguments["command"])
    elif name == "zsh_neverhang_status":
        result = circuit_breaker.get_status()
    elif name == "zsh_neverhang_reset":
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []
        circuit_breaker.opened_at = None
        result = {'success': True, 'message': 'Circuit breaker reset to CLOSED state'}
    else:
        result = {'error': f'Unknown tool: {name}'}

    return [TextContent(
        type="text",
        text=json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    )]


def _format_task_output(result: dict) -> list[TextContent]:
    """Format task-based execution output cleanly."""
    parts = []

    # Output first - clean
    output = result.get('output', '')
    if output:
        parts.append(output.rstrip('\n'))

    # Error message if present
    error = result.get('error')
    if error:
        parts.append(f"[error] {error}")

    # Status line
    status = result.get('status', 'unknown')
    task_id = result.get('task_id', '')
    elapsed = result.get('elapsed_seconds', 0)

    if status == "running":
        has_stdin = result.get('has_stdin', False)
        parts.append(f"[RUNNING task_id={task_id} elapsed={elapsed}s stdin={'yes' if has_stdin else 'no'}]")
        parts.append("Use zsh_poll to continue, zsh_send to input, zsh_kill to stop.")
    elif status == "completed":
        exit_codes = result.get('exit_codes', '[unknown:-1]')
        parts.append(f"[COMPLETED task_id={task_id} elapsed={elapsed}s exit={exit_codes}]")
    elif status == "timeout":
        parts.append(f"[TIMEOUT task_id={task_id} elapsed={elapsed}s]")
    elif status == "killed":
        parts.append(f"[KILLED task_id={task_id} elapsed={elapsed}s]")
    elif status == "error":
        parts.append(f"[ERROR task_id={task_id} elapsed={elapsed}s]")

    if result.get('warnings'):
        parts.append(f"[warnings: {result['warnings']}]")

    return [TextContent(type="text", text='\n'.join(parts) if parts else "(no output)")]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except* anyio.ClosedResourceError:
        # Graceful shutdown when stdin closes (normal for MCP stdio transport)
        pass
    except* Exception as eg:
        # Log unexpected errors but don't crash
        import sys
        print(f"zsh-tool: unexpected error: {eg}", file=sys.stderr)


def run():
    """Entry point for CLI."""
    # Check zsh availability before starting
    if not shutil.which('zsh'):
        import sys
        print("zsh-tool: zsh not found in PATH. This tool requires zsh to function.", file=sys.stderr)
        print("zsh-tool: Install zsh or use a different shell tool.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main())


if __name__ == '__main__':
    run()
