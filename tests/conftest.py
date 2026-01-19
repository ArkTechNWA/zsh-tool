"""
Pytest configuration and fixtures.

Handles async test cleanup to prevent resource warnings.
"""

import pytest
import asyncio
import os
import signal
from pathlib import Path


def _cleanup_tasks_sync():
    """Synchronous cleanup of live tasks.

    For PTY mode: kill process and close file descriptor.
    For subprocess mode: kill process and close all transports properly.
    """
    from zsh_tool.tasks import live_tasks

    for task_id, task in list(live_tasks.items()):
        try:
            if task.is_pty:
                # PTY mode - task.process is a PID
                if isinstance(task.process, int):
                    try:
                        os.kill(task.process, signal.SIGKILL)
                        os.waitpid(task.process, os.WNOHANG)
                    except (ProcessLookupError, ChildProcessError, OSError):
                        pass
                    if task.pty_fd is not None:
                        try:
                            os.close(task.pty_fd)
                        except OSError:
                            pass
            else:
                # Subprocess mode - task.process is asyncio.subprocess.Process
                proc = task.process
                if proc is not None:
                    # Close all stream transports to prevent resource warnings
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            try:
                                # Close the transport if available
                                if hasattr(stream, '_transport') and stream._transport is not None:
                                    stream._transport.close()
                                elif hasattr(stream, 'close'):
                                    stream.close()
                            except Exception:
                                pass
                    # Kill the process
                    if hasattr(proc, 'kill'):
                        try:
                            proc.kill()
                        except (ProcessLookupError, Exception):
                            pass
                    # Close the main transport to prevent resource warnings
                    if hasattr(proc, '_transport') and proc._transport is not None:
                        try:
                            proc._transport.close()
                        except Exception:
                            pass
        except Exception:
            pass

    live_tasks.clear()


@pytest.fixture(autouse=True)
def cleanup_live_tasks_sync(request):
    """Clean up any lingering live tasks after each test (sync version)."""
    yield
    _cleanup_tasks_sync()


@pytest.fixture
def event_loop_policy():
    """Use default event loop policy."""
    return asyncio.DefaultEventLoopPolicy()
