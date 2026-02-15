"""
Live Task Manager for zsh-tool.

Yield-based execution with oversight (Issue #1).
Manages command execution, output buffering, and task lifecycle.
"""

import asyncio
import fcntl
import os
import pty
import select
import struct
import termios
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import json as _json
import tempfile as _tempfile

from .config import (
    ALAN_DB_PATH,
    EXEC_BINARY_PATH,
    NEVERHANG_TIMEOUT_DEFAULT,
    NEVERHANG_TIMEOUT_MAX,
    TRUNCATE_OUTPUT_AT,
    YIELD_AFTER_DEFAULT,
)
from .neverhang import CircuitBreaker
from .alan import ALAN, _wrap_for_pipestatus, _extract_pipestatus


def _pipestatus_success(pipestatus: list[int] | None) -> bool:
    """Check if overall exit is success (last segment is 0)."""
    if not pipestatus:
        return True
    return pipestatus[-1] == 0


# Global instances
alan = ALAN(ALAN_DB_PATH)
circuit_breaker = CircuitBreaker()


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
    pipestatus: Optional[list[int]] = None  # Raw exit codes per pipe segment
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

        # Extract pipestatus from output (Issue #20, #27)
        clean_output, pipestatus = _extract_pipestatus(task.output_buffer)
        task.output_buffer = clean_output  # Strip marker from visible output

        # Store raw pipestatus
        fallback_code = task.process.returncode if task.process.returncode is not None else 0
        task.pipestatus = pipestatus if pipestatus else [fallback_code]

        # Process completed
        if task.status == "running":
            task.status = "completed"
            if _pipestatus_success(task.pipestatus):
                circuit_breaker.record_success()
            # Note: we don't record failure to circuit breaker for normal command failures

        # Record in A.L.A.N. - use last pipestatus value (overall exit)
        duration_ms = int((time.time() - task.started_at) * 1000)
        primary_exit = task.pipestatus[-1]
        alan.record(
            task.command,
            primary_exit,
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


async def _rust_output_collector(task: LiveTask, meta_path: str):
    """Background coroutine that collects output from the Rust executor.

    The Rust binary handles fd 3 sideband, timeout, and process management.
    We just read its stdout (which is clean command output) and parse the
    meta-file it writes on exit.
    """
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    task.process.stdout.read(4096),
                    timeout=0.1
                )
                if chunk:
                    task.output_buffer += chunk.decode('utf-8', errors='replace')
                elif task.process.returncode is not None:
                    break
                else:
                    await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                if task.process.returncode is not None:
                    break
                await asyncio.sleep(0.01)
                continue

        # Ensure process has finished
        await task.process.wait()

        # Read metadata from meta-file (no in-band markers!)
        pipestatus = None
        timed_out = False
        try:
            with open(meta_path, 'r') as f:
                meta = _json.load(f)
            pipestatus = meta.get('pipestatus')
            timed_out = meta.get('timed_out', False)
        except (FileNotFoundError, _json.JSONDecodeError):
            pass
        finally:
            try:
                os.unlink(meta_path)
            except OSError:
                pass

        if timed_out:
            task.status = "timeout"
            circuit_breaker.record_timeout(alan._hash_command(task.command))

        fallback_code = task.process.returncode if task.process.returncode is not None else 0
        task.pipestatus = pipestatus if pipestatus else [fallback_code]

        if task.status == "running":
            task.status = "completed"
            if _pipestatus_success(task.pipestatus):
                circuit_breaker.record_success()

        # ALAN core recording handled by Rust executor (--db flag).
        # Only SSH recording and manopt triggering remain in Python.
        # TODO(phase3): port SSH recording and manopt to Rust.
        duration_ms = int((time.time() - task.started_at) * 1000)
        primary_exit = task.pipestatus[-1]
        alan._record_ssh_if_applicable(
            task.command, primary_exit, duration_ms, task.output_buffer[:500]
        )
        alan._trigger_manopt_if_applicable(task.command, primary_exit)
        alan.maybe_prune()

    except Exception as e:
        task.status = "error"
        task.error = str(e)


async def _pty_output_collector(task: LiveTask):
    """Background coroutine that collects output from a PTY."""
    fallback_code = 0  # Track process exit for fallback
    try:
        while True:
            # Check if process is still running
            try:
                pid_result = os.waitpid(task.process, os.WNOHANG)
                if pid_result[0] != 0:
                    # Process exited - capture for fallback
                    fallback_code = os.WEXITSTATUS(pid_result[1]) if os.WIFEXITED(pid_result[1]) else -1
                    task.status = "completed"
                    break
            except ChildProcessError:
                # Process already reaped
                task.status = "completed"
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

        # Extract pipestatus from output (Issue #20, #27)
        clean_output, pipestatus = _extract_pipestatus(task.output_buffer)
        task.output_buffer = clean_output  # Strip marker from visible output

        # Store raw pipestatus
        task.pipestatus = pipestatus if pipestatus else [fallback_code]

        # Record success to circuit breaker if overall exit is 0
        if task.status == "completed" and _pipestatus_success(task.pipestatus):
            circuit_breaker.record_success()

        # Record in A.L.A.N. - use last pipestatus value (overall exit)
        duration_ms = int((time.time() - task.started_at) * 1000)
        primary_exit = task.pipestatus[-1]
        alan.record(
            task.command,
            primary_exit,
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
    insights = alan.get_insights(command, timeout)  # list[tuple[str, str]]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        insights.append(("warning", circuit_message))

    # Create task ID
    task_id = str(uuid.uuid4())[:8]

    # Use Rust executor with --pty if available
    if EXEC_BINARY_PATH:
        meta_path = _tempfile.mktemp(prefix='zsh-meta-', suffix='.json')
        exec_args = [
            EXEC_BINARY_PATH,
            "--meta", meta_path,
            "--timeout", str(timeout),
            "--pty",
            "--db", str(ALAN_DB_PATH),
            "--session-id", alan.session_id,
            "--", command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *exec_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task = LiveTask(
            task_id=task_id,
            command=command,
            process=proc,
            started_at=time.time(),
            timeout=timeout,
            is_pty=True,
        )
        live_tasks[task_id] = task
        asyncio.create_task(_rust_output_collector(task, meta_path))
        await asyncio.sleep(yield_after)
        return _build_task_response(task, insights)

    # Legacy: direct PTY fork (fallback when Rust binary not available)
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
    return _build_task_response(task, insights)


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
    insights = alan.get_insights(command, timeout)  # list[tuple[str, str]]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        insights.append(("warning", circuit_message))

    # Create task
    task_id = str(uuid.uuid4())[:8]

    # Use Rust executor if available
    if EXEC_BINARY_PATH:
        meta_path = _tempfile.mktemp(prefix='zsh-meta-', suffix='.json')
        exec_args = [
            EXEC_BINARY_PATH,
            "--meta", meta_path,
            "--timeout", str(timeout),
            "--db", str(ALAN_DB_PATH),
            "--session-id", alan.session_id,
            "--", command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *exec_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task = LiveTask(
            task_id=task_id,
            command=command,
            process=proc,
            started_at=time.time(),
            timeout=timeout
        )
        live_tasks[task_id] = task
        asyncio.create_task(_rust_output_collector(task, meta_path))
        await asyncio.sleep(yield_after)
        return _build_task_response(task, insights)

    # Legacy: direct zsh subprocess (fallback when Rust binary not available)
    wrapped_command = _wrap_for_pipestatus(command)

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

    asyncio.create_task(_output_collector(task))
    await asyncio.sleep(yield_after)
    return _build_task_response(task, insights)


def _build_task_response(task: LiveTask, insights: list[tuple[str, str]] | None = None) -> dict:
    """Build response dict from task state."""
    from .config import PIPESTATUS_MARKER
    from .alan import get_post_insights

    # Get new output since last read
    new_output = task.output_buffer[task.output_read_pos:]
    task.output_read_pos = len(task.output_buffer)

    # Strip pipestatus marker from output to prevent leaking (Issue #29)
    # The marker may be in output_buffer before _output_collector finishes extraction
    if PIPESTATUS_MARKER in new_output:
        lines = new_output.split('\n')
        new_output = '\n'.join(line for line in lines if PIPESTATUS_MARKER not in line)

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
        result['success'] = _pipestatus_success(task.pipestatus)
        result['pipestatus'] = task.pipestatus
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

    # Build insight dict: merge pre-execution and post-execution insights
    all_insights = list(insights or [])
    if task.status == "completed" and task.pipestatus:
        post = get_post_insights(task.command, task.pipestatus, new_output)
        all_insights.extend(post)

    if all_insights:
        grouped: dict[str, list[str]] = {}
        for level, msg in all_insights:
            grouped.setdefault(level, []).append(msg)
        result['insights'] = grouped

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
