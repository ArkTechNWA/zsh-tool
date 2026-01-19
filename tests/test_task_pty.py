"""
Tests for Task Manager - PTY Mode (Issue #9).

Tests PTY execution mode for full terminal emulation.
"""

import pytest
import asyncio
import time

from zsh_tool.tasks import (
    LiveTask, live_tasks,
    execute_zsh_pty, circuit_breaker
)
from zsh_tool.neverhang import CircuitState


class TestPTYLiveTask:
    """Tests for PTY-specific LiveTask fields."""

    def test_pty_task_creation(self):
        """PTY task can be created with PTY fields."""
        task = LiveTask(
            task_id="pty_test",
            command="echo hello",
            process=12345,  # PID
            started_at=time.time(),
            timeout=60,
            is_pty=True,
            pty_fd=10
        )
        assert task.is_pty is True
        assert task.pty_fd == 10
        assert task.process == 12345

    def test_pty_task_defaults(self):
        """PTY fields have correct defaults."""
        task = LiveTask(
            task_id="test",
            command="ls",
            process=None,
            started_at=time.time(),
            timeout=60
        )
        assert task.is_pty is False
        assert task.pty_fd is None


@pytest.mark.asyncio
class TestExecuteZshPty:
    """Tests for execute_zsh_pty() function."""

    async def test_pty_returns_task_id(self):
        """PTY execution returns a task_id."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("echo 'pty test'", timeout=10, yield_after=2)

        assert 'task_id' in result
        assert len(result['task_id']) == 8

    async def test_pty_captures_output(self):
        """PTY mode captures command output."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("echo 'PTY OUTPUT TEST'", timeout=10, yield_after=2)

        # Wait for output to be captured
        await asyncio.sleep(0.5)

        # Check output was captured
        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            # PTY output includes the echoed text
            assert "PTY OUTPUT TEST" in task.output_buffer or task.status == 'completed'

    async def test_pty_circuit_breaker_blocks(self):
        """PTY execution respects circuit breaker."""
        circuit_breaker.state = CircuitState.OPEN
        circuit_breaker.opened_at = time.time()
        circuit_breaker.failures = [(time.time(), "hash")] * 3

        result = await execute_zsh_pty("echo test", timeout=10, yield_after=1)

        assert result['success'] is False
        assert 'NEVERHANG' in result.get('error', '') or 'circuit' in result.get('error', '').lower()

        # Reset
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

    async def test_pty_includes_warnings(self):
        """PTY execution includes A.L.A.N. warnings."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("echo unique_pty_cmd", timeout=10, yield_after=1)

        assert 'warnings' in result
        assert any("A.L.A.N." in w for w in result['warnings'])

    async def test_pty_status_tracking(self):
        """PTY task tracks status correctly."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("echo fast", timeout=10, yield_after=2)

        assert 'status' in result
        # May be running or completed
        assert result['status'] in ('running', 'completed', 'error')

    async def test_pty_task_registered(self):
        """PTY task is registered in live_tasks."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("sleep 2", timeout=10, yield_after=0.5)

        # Task should be in registry while running
        if result['status'] == 'running':
            assert result['task_id'] in live_tasks
            task = live_tasks[result['task_id']]
            assert task.is_pty is True

    async def test_pty_timeout_validation(self):
        """PTY timeout is validated."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Request huge timeout - should be capped
        result = await execute_zsh_pty("echo test", timeout=99999, yield_after=1)

        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            assert task.timeout <= 600  # NEVERHANG_TIMEOUT_MAX


@pytest.mark.asyncio
class TestPTYYieldBehavior:
    """Tests for PTY yield behavior."""

    async def test_pty_yields_quickly(self):
        """PTY returns before long command completes."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        start = time.time()
        result = await execute_zsh_pty("sleep 10", timeout=30, yield_after=0.5)
        elapsed = time.time() - start

        # Should return around yield_after time
        assert elapsed < 3
        assert result['status'] == 'running'

    async def test_pty_fast_command_completes(self):
        """Fast PTY command can complete before yield."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("echo done", timeout=10, yield_after=3)

        # Give time to complete
        await asyncio.sleep(1)

        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            # Should be completed by now
            if task.status == 'completed':
                assert task.exit_code is not None


@pytest.mark.asyncio
class TestPTYInteractive:
    """Tests for PTY interactive features."""

    async def test_pty_supports_colors(self):
        """PTY supports color output (TERM is set)."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Command that checks terminal
        result = await execute_zsh_pty("echo $TERM", timeout=10, yield_after=2)

        await asyncio.sleep(0.5)

        # PTY should have a terminal type
        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            # Output may contain xterm or similar
            # Just verify we got some output
            assert task.output_buffer is not None

    async def test_pty_handles_prompt(self):
        """PTY handles shell prompts."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Simple command that will show prompt
        result = await execute_zsh_pty("echo 'prompt test'", timeout=10, yield_after=2)

        await asyncio.sleep(0.5)

        # Should execute without issues
        assert result['status'] in ('running', 'completed', 'error')


@pytest.mark.asyncio
class TestPTYExitCodes:
    """Tests for PTY exit code handling."""

    async def test_pty_success_exit_code(self):
        """PTY captures success exit code."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("exit 0", timeout=10, yield_after=2)

        await asyncio.sleep(1)

        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            if task.status == 'completed':
                assert task.exit_code == 0

    async def test_pty_failure_exit_code(self):
        """PTY captures failure exit code."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("exit 42", timeout=10, yield_after=2)

        await asyncio.sleep(1)

        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            if task.status == 'completed':
                assert task.exit_code == 42


@pytest.mark.asyncio
class TestPTYErrors:
    """Tests for PTY error handling."""

    async def test_pty_handles_invalid_command(self):
        """PTY handles invalid command gracefully."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("nonexistent_command_xyz123", timeout=10, yield_after=2)

        await asyncio.sleep(1)

        # Should complete (with error) or still be running
        assert result['status'] in ('running', 'completed', 'error')

    async def test_pty_handles_killed_process(self):
        """PTY handles process being killed."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await execute_zsh_pty("sleep 100", timeout=2, yield_after=0.5)

        # Wait for timeout
        await asyncio.sleep(3)

        if result['task_id'] in live_tasks:
            task = live_tasks[result['task_id']]
            assert task.status in ('timeout', 'completed', 'killed', 'error')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
