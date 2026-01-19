#!/usr/bin/env python3
"""
Zsh Tool MCP Server
===================
Full-parity zsh execution with NEVERHANG circuit breaker and A.L.A.N. learning.

For Johnny5. For us.
"""

import anyio
import asyncio
import fcntl
import os
import pty
import select
import shutil
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .config import (
    ALAN_DB_PATH,
    NEVERHANG_TIMEOUT_DEFAULT,
    NEVERHANG_TIMEOUT_MAX,
    TRUNCATE_OUTPUT_AT,
    YIELD_AFTER_DEFAULT,
)
from .neverhang import CircuitState, CircuitBreaker
from .alan import ALAN, _wrap_for_pipestatus, _extract_pipestatus


# Global instances
alan = ALAN(ALAN_DB_PATH)
circuit_breaker = CircuitBreaker()

# =============================================================================
# Live Task Manager (Issue #1: Yield-based execution with oversight)
# =============================================================================

@dataclass
class LiveTask:
    """A running command with live output buffering."""
    task_id: str
    command: str
    process: Any  # asyncio.subprocess.Process or PID for PTY
    started_at: float
    timeout: int
    output_buffer: str = ""
    output_read_pos: int = 0  # How much output has been returned to caller
    status: str = "running"  # running, completed, timeout, killed, error
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # PTY mode fields
    is_pty: bool = False
    pty_fd: Optional[int] = None  # Master PTY file descriptor


# Active tasks registry
live_tasks: dict[str, LiveTask] = {}


def _cleanup_task(task_id: str):
    """Clean up task resources and remove from registry."""
    if task_id in live_tasks:
        task = live_tasks[task_id]
        if task.is_pty:
            # Close PTY file descriptor
            if task.pty_fd is not None:
                try:
                    os.close(task.pty_fd)
                except Exception:
                    pass
        else:
            # Close stdin if still open
            if task.process and task.process.stdin and not task.process.stdin.is_closing():
                try:
                    task.process.stdin.close()
                except Exception:
                    pass
        # Remove from registry to prevent memory leak
        del live_tasks[task_id]


async def _output_collector(task: LiveTask):
    """Background coroutine that collects output from a running process."""
    try:
        while True:
            # Read available output (non-blocking style via small reads)
            try:
                chunk = await asyncio.wait_for(
                    task.process.stdout.read(4096),
                    timeout=0.1
                )
                if chunk:
                    task.output_buffer += chunk.decode('utf-8', errors='replace')
                elif task.process.returncode is not None:
                    # Process finished
                    break
                else:
                    # Empty read but process still running - yield to prevent spin
                    await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                # No data available right now, check if process done
                if task.process.returncode is not None:
                    break
                # Check overall timeout
                elapsed = time.time() - task.started_at
                if elapsed > task.timeout:
                    task.status = "timeout"
                    task.process.kill()
                    await task.process.wait()
                    circuit_breaker.record_timeout(alan._hash_command(task.command))
                    break
                # Yield before continuing to prevent event loop starvation
                await asyncio.sleep(0.01)
                continue

        # Process completed
        if task.status == "running":
            task.status = "completed"
            task.exit_code = task.process.returncode
            circuit_breaker.record_success()

        # Extract pipestatus from output (Issue #20)
        clean_output, pipestatus = _extract_pipestatus(task.output_buffer)
        task.output_buffer = clean_output  # Strip marker from visible output

        # Record in A.L.A.N.
        duration_ms = int((time.time() - task.started_at) * 1000)
        alan.record(
            task.command,
            task.exit_code if task.exit_code is not None else -1,
            duration_ms,
            task.status == "timeout",
            clean_output[:500],
            "",
            pipestatus=pipestatus
        )
        alan.maybe_prune()

    except Exception as e:
        task.status = "error"
        task.error = str(e)


async def _pty_output_collector(task: LiveTask):
    """Background coroutine that collects output from a PTY."""
    try:
        while True:
            # Check if process is still running
            try:
                pid_result = os.waitpid(task.process, os.WNOHANG)
                if pid_result[0] != 0:
                    # Process exited
                    task.exit_code = os.WEXITSTATUS(pid_result[1]) if os.WIFEXITED(pid_result[1]) else -1
                    task.status = "completed"
                    circuit_breaker.record_success()
                    break
            except ChildProcessError:
                # Process already reaped
                task.status = "completed"
                task.exit_code = 0
                break

            # Check timeout
            elapsed = time.time() - task.started_at
            if elapsed > task.timeout:
                task.status = "timeout"
                try:
                    os.kill(task.process, 9)  # SIGKILL
                except ProcessLookupError:
                    pass
                circuit_breaker.record_timeout(alan._hash_command(task.command))
                break

            # Read available output from PTY (non-blocking)
            try:
                # Use select to check if data available
                readable, _, _ = select.select([task.pty_fd], [], [], 0.1)
                if readable:
                    chunk = os.read(task.pty_fd, 4096)
                    if chunk:
                        task.output_buffer += chunk.decode('utf-8', errors='replace')
            except (OSError, IOError):
                # PTY closed or error
                break

            # Small yield to not hog CPU
            await asyncio.sleep(0.05)

        # Read any remaining output
        try:
            while True:
                readable, _, _ = select.select([task.pty_fd], [], [], 0.1)
                if not readable:
                    break
                chunk = os.read(task.pty_fd, 4096)
                if not chunk:
                    break
                task.output_buffer += chunk.decode('utf-8', errors='replace')
        except (OSError, IOError):
            pass

        # Extract pipestatus from output (Issue #20)
        clean_output, pipestatus = _extract_pipestatus(task.output_buffer)
        task.output_buffer = clean_output  # Strip marker from visible output

        # Record in A.L.A.N.
        duration_ms = int((time.time() - task.started_at) * 1000)
        alan.record(
            task.command,
            task.exit_code if task.exit_code is not None else -1,
            duration_ms,
            task.status == "timeout",
            clean_output[:500],
            "",
            pipestatus=pipestatus
        )
        alan.maybe_prune()

    except Exception as e:
        task.status = "error"
        task.error = str(e)


async def execute_zsh_pty(
    command: str,
    timeout: int = NEVERHANG_TIMEOUT_DEFAULT,
    yield_after: float = YIELD_AFTER_DEFAULT,
    description: Optional[str] = None
) -> dict:
    """Execute command in a PTY for full terminal emulation."""

    # Validate timeout
    timeout = min(timeout, NEVERHANG_TIMEOUT_MAX)

    # Check A.L.A.N. 2.0 for insights
    insights = alan.get_insights(command, timeout)
    warnings = [f"A.L.A.N.: {i}" for i in insights]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        warnings.append(circuit_message)

    # Create task ID
    task_id = str(uuid.uuid4())[:8]

    # Fork with PTY
    pid, master_fd = pty.fork()

    if pid == 0:
        # Child process - exec zsh with command wrapped for pipestatus capture
        wrapped_command = _wrap_for_pipestatus(command)
        os.execvp('/bin/zsh', ['/bin/zsh', '-c', wrapped_command])
        # If exec fails, exit
        os._exit(1)

    # Parent process - set up non-blocking read
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # Set terminal size (80x24 default)
    try:
        winsize = struct.pack('HHHH', 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    task = LiveTask(
        task_id=task_id,
        command=command,
        process=pid,  # PID instead of Process object
        started_at=time.time(),
        timeout=timeout,
        is_pty=True,
        pty_fd=master_fd
    )
    live_tasks[task_id] = task

    # Start background output collector
    asyncio.create_task(_pty_output_collector(task))

    # Wait for yield_after seconds then return (process continues in background)
    await asyncio.sleep(yield_after)

    # Check status and return
    return _build_task_response(task, warnings)


async def execute_zsh_yielding(
    command: str,
    timeout: int = NEVERHANG_TIMEOUT_DEFAULT,
    yield_after: float = YIELD_AFTER_DEFAULT,
    description: Optional[str] = None
) -> dict:
    """Execute with yield - returns partial output after yield_after seconds if still running."""

    # Validate timeout
    timeout = min(timeout, NEVERHANG_TIMEOUT_MAX)

    # Check A.L.A.N. 2.0 for insights
    insights = alan.get_insights(command, timeout)
    warnings = [f"A.L.A.N.: {i}" for i in insights]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        warnings.append(circuit_message)

    # Create task
    task_id = str(uuid.uuid4())[:8]

    # Wrap command for pipestatus capture
    wrapped_command = _wrap_for_pipestatus(command)

    # Start process with stdin pipe for interactive input
    proc = await asyncio.create_subprocess_shell(
        wrapped_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
        executable='/bin/zsh'
    )

    task = LiveTask(
        task_id=task_id,
        command=command,
        process=proc,
        started_at=time.time(),
        timeout=timeout
    )
    live_tasks[task_id] = task

    # Start background output collector
    asyncio.create_task(_output_collector(task))

    # Wait for yield_after seconds then return (process continues in background)
    await asyncio.sleep(yield_after)

    # Check status and return
    return _build_task_response(task, warnings)


def _build_task_response(task: LiveTask, warnings: list = None) -> dict:
    """Build response dict from task state."""
    # Get new output since last read
    new_output = task.output_buffer[task.output_read_pos:]
    task.output_read_pos = len(task.output_buffer)

    # Truncate if needed
    if len(new_output) > TRUNCATE_OUTPUT_AT:
        new_output = new_output[:TRUNCATE_OUTPUT_AT] + f"\n[TRUNCATED - {len(new_output)} chars total]"

    elapsed = time.time() - task.started_at

    result = {
        'task_id': task.task_id,
        'status': task.status,
        'output': new_output,
        'elapsed_seconds': round(elapsed, 1),
    }

    if task.status == "running":
        result['has_stdin'] = task.is_pty or (hasattr(task.process, 'stdin') and task.process.stdin is not None)
        result['is_pty'] = task.is_pty
        result['message'] = f"Command running ({elapsed:.1f}s). Use zsh_poll to get more output, zsh_send to send input."
    elif task.status == "completed":
        result['success'] = task.exit_code == 0
        result['exit_code'] = task.exit_code
        _cleanup_task(task.task_id)
    elif task.status == "timeout":
        result['success'] = False
        result['error'] = f"Command timed out after {task.timeout}s"
        _cleanup_task(task.task_id)
    elif task.status == "killed":
        result['success'] = False
        result['error'] = "Command was killed"
        _cleanup_task(task.task_id)
    elif task.status == "error":
        result['success'] = False
        result['error'] = task.error
        _cleanup_task(task.task_id)

    if warnings:
        result['warnings'] = warnings

    return result


async def poll_task(task_id: str) -> dict:
    """Get current output from a running task."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]
    return _build_task_response(task)


async def send_to_task(task_id: str, input_text: str) -> dict:
    """Send input to a task's stdin (pipe or PTY)."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]

    if task.status != "running":
        return {'error': f'Task not running (status: {task.status})', 'success': False}

    try:
        data = input_text if input_text.endswith('\n') else input_text + '\n'

        if task.is_pty:
            # Write to PTY master
            os.write(task.pty_fd, data.encode('utf-8'))
        else:
            # Write to process stdin pipe
            if not task.process.stdin:
                return {'error': 'No stdin available for this task', 'success': False}
            task.process.stdin.write(data.encode('utf-8'))
            await task.process.stdin.drain()

        return {'success': True, 'message': f'Sent {len(input_text)} chars to task {task_id}'}
    except Exception as e:
        return {'error': f'Failed to send input: {e}', 'success': False}


async def kill_task(task_id: str) -> dict:
    """Kill a running task."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]

    if task.status != "running":
        return {'error': f'Task not running (status: {task.status})', 'success': False}

    try:
        if task.is_pty:
            # Kill PTY process by PID
            os.kill(task.process, 9)  # SIGKILL
            # Non-blocking reap - don't wait for zombie
            try:
                os.waitpid(task.process, os.WNOHANG)
            except ChildProcessError:
                pass
        else:
            task.process.kill()
            # Don't block forever waiting for process to die
            try:
                await asyncio.wait_for(task.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # Process didn't die cleanly, but we tried

        task.status = "killed"
        _cleanup_task(task_id)
        return {'success': True, 'message': f'Task {task_id} killed'}
    except Exception as e:
        return {'error': f'Failed to kill task: {e}', 'success': False}


def list_tasks() -> dict:
    """List all active tasks."""
    tasks = []
    for tid, task in live_tasks.items():
        tasks.append({
            'task_id': tid,
            'command': task.command[:50] + ('...' if len(task.command) > 50 else ''),
            'status': task.status,
            'elapsed_seconds': round(time.time() - task.started_at, 1),
            'output_bytes': len(task.output_buffer)
        })
    return {'tasks': tasks, 'count': len(tasks)}


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
        exit_code = result.get('exit_code', 0)
        if exit_code == 0:
            parts.append(f"[COMPLETED task_id={task_id} elapsed={elapsed}s exit=0]")
        else:
            parts.append(f"[COMPLETED task_id={task_id} elapsed={elapsed}s exit={exit_code}]")
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
