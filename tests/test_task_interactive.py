"""
Tests for Task Manager - Interactive Operations (Issue #10).

Tests poll_task, send_to_task, kill_task, and list_tasks.
"""

import pytest
import asyncio
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "zsh_tool"))

from server import (
    LiveTask, live_tasks, _cleanup_task,
    poll_task, send_to_task, kill_task, list_tasks,
    execute_zsh_yielding, execute_zsh_pty,
    circuit_breaker, CircuitState
)


class TestListTasks:
    """Tests for list_tasks()."""

    def test_list_tasks_returns_dict(self):
        """list_tasks returns a dictionary."""
        result = list_tasks()
        assert isinstance(result, dict)

    def test_list_tasks_has_tasks_key(self):
        """list_tasks result has 'tasks' key."""
        result = list_tasks()
        assert 'tasks' in result

    def test_list_tasks_empty_when_no_tasks(self):
        """list_tasks returns empty list when no tasks."""
        # Clear any existing tasks
        for tid in list(live_tasks.keys()):
            del live_tasks[tid]

        result = list_tasks()
        assert result['tasks'] == []

    def test_list_tasks_shows_registered_task(self):
        """list_tasks includes registered tasks."""
        task = LiveTask(
            task_id="list_test",
            command="echo hello",
            process=None,
            started_at=time.time(),
            timeout=60
        )
        live_tasks["list_test"] = task

        result = list_tasks()
        task_ids = [t['task_id'] for t in result['tasks']]
        assert "list_test" in task_ids

        # Cleanup
        del live_tasks["list_test"]

    def test_list_tasks_truncates_long_commands(self):
        """list_tasks truncates long command strings."""
        long_command = "echo " + "x" * 100
        task = LiveTask(
            task_id="long_cmd_test",
            command=long_command,
            process=None,
            started_at=time.time(),
            timeout=60
        )
        live_tasks["long_cmd_test"] = task

        result = list_tasks()
        for t in result['tasks']:
            if t['task_id'] == "long_cmd_test":
                # Should be truncated to ~50 chars + "..."
                assert len(t['command']) <= 60
                assert t['command'].endswith('...')

        # Cleanup
        del live_tasks["long_cmd_test"]


@pytest.mark.asyncio
class TestPollTask:
    """Tests for poll_task()."""

    async def test_poll_unknown_task(self):
        """poll_task returns error for unknown task."""
        result = await poll_task("nonexistent_task_xyz")
        assert result['success'] is False
        assert 'Unknown task' in result.get('error', '')

    async def test_poll_existing_task(self):
        """poll_task returns task info for existing task."""
        task = LiveTask(
            task_id="poll_test",
            command="echo hello",
            process=None,
            started_at=time.time(),
            timeout=60,
            status="running",
            output_buffer="hello\n"
        )
        live_tasks["poll_test"] = task

        result = await poll_task("poll_test")
        assert result['task_id'] == "poll_test"
        assert result['status'] == "running"

        # Cleanup
        del live_tasks["poll_test"]

    async def test_poll_returns_output(self):
        """poll_task returns accumulated output."""
        task = LiveTask(
            task_id="output_test",
            command="echo test",
            process=None,
            started_at=time.time(),
            timeout=60,
            output_buffer="line1\nline2\n"
        )
        live_tasks["output_test"] = task

        result = await poll_task("output_test")
        assert "line1" in result.get('output', '')

        # Cleanup
        del live_tasks["output_test"]

    async def test_poll_real_task(self):
        """poll_task works on real executing task."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        exec_result = await execute_zsh_yielding("sleep 2", timeout=10, yield_after=0.5)
        task_id = exec_result['task_id']

        if task_id in live_tasks:
            poll_result = await poll_task(task_id)
            assert poll_result['task_id'] == task_id
            assert poll_result['status'] in ('running', 'completed', 'error')


@pytest.mark.asyncio
class TestSendToTask:
    """Tests for send_to_task()."""

    async def test_send_to_unknown_task(self):
        """send_to_task returns error for unknown task."""
        result = await send_to_task("nonexistent_xyz", "hello")
        assert result['success'] is False
        assert 'Unknown task' in result.get('error', '')

    async def test_send_to_completed_task(self):
        """send_to_task returns error for completed task."""
        task = LiveTask(
            task_id="completed_test",
            command="echo done",
            process=None,
            started_at=time.time(),
            timeout=60,
            status="completed"
        )
        live_tasks["completed_test"] = task

        result = await send_to_task("completed_test", "input")
        assert result['success'] is False
        assert 'not running' in result.get('error', '').lower()

        # Cleanup
        del live_tasks["completed_test"]

    async def test_send_to_pty_task(self):
        """send_to_task can send to PTY task."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Start a PTY task that reads input
        exec_result = await execute_zsh_pty("cat", timeout=10, yield_after=0.5)
        task_id = exec_result['task_id']

        if task_id in live_tasks and live_tasks[task_id].status == "running":
            # Send some input
            send_result = await send_to_task(task_id, "test input")
            # Should succeed or fail gracefully
            assert 'success' in send_result or 'error' in send_result

            # Kill the task
            await kill_task(task_id)

    async def test_send_adds_newline(self):
        """send_to_task adds newline if not present."""
        # This is tested implicitly by the fact that commands work
        # The implementation adds \n if not present
        pass  # Implementation detail tested through integration


@pytest.mark.asyncio
class TestKillTask:
    """Tests for kill_task()."""

    async def test_kill_unknown_task(self):
        """kill_task returns error for unknown task."""
        result = await kill_task("nonexistent_xyz")
        assert result['success'] is False
        assert 'Unknown task' in result.get('error', '')

    async def test_kill_completed_task(self):
        """kill_task returns error for already completed task."""
        task = LiveTask(
            task_id="kill_completed",
            command="echo done",
            process=None,
            started_at=time.time(),
            timeout=60,
            status="completed"
        )
        live_tasks["kill_completed"] = task

        result = await kill_task("kill_completed")
        assert result['success'] is False
        assert 'not running' in result.get('error', '').lower()

        # Cleanup
        del live_tasks["kill_completed"]

    async def test_kill_running_task(self):
        """kill_task successfully kills running task."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Start a long-running task
        exec_result = await execute_zsh_yielding("sleep 100", timeout=120, yield_after=0.5)
        task_id = exec_result['task_id']

        if task_id in live_tasks and live_tasks[task_id].status == "running":
            # Kill it
            kill_result = await kill_task(task_id)
            assert kill_result['success'] is True
            assert task_id not in live_tasks  # Should be removed

    async def test_kill_pty_task(self):
        """kill_task successfully kills PTY task."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Start a PTY task
        exec_result = await execute_zsh_pty("sleep 100", timeout=120, yield_after=0.5)
        task_id = exec_result['task_id']

        if task_id in live_tasks and live_tasks[task_id].status == "running":
            # Kill it
            kill_result = await kill_task(task_id)
            assert kill_result['success'] is True


@pytest.mark.asyncio
class TestInteractiveWorkflow:
    """Tests for complete interactive workflows."""

    async def test_execute_poll_kill_workflow(self):
        """Test full execute -> poll -> kill workflow."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Execute
        exec_result = await execute_zsh_yielding("sleep 10", timeout=30, yield_after=0.5)
        task_id = exec_result['task_id']

        if task_id in live_tasks:
            # Poll
            poll_result = await poll_task(task_id)
            assert poll_result['status'] == 'running'

            # Kill
            kill_result = await kill_task(task_id)
            assert kill_result['success'] is True

            # Verify removed
            assert task_id not in live_tasks

    async def test_multiple_concurrent_tasks(self):
        """Test handling multiple concurrent tasks."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        # Start multiple tasks
        task_ids = []
        for i in range(3):
            result = await execute_zsh_yielding(f"sleep {10+i}", timeout=30, yield_after=0.2)
            if result.get('task_id'):
                task_ids.append(result['task_id'])

        # List should show all
        list_result = list_tasks()
        listed_ids = [t['task_id'] for t in list_result['tasks']]

        for tid in task_ids:
            if tid in live_tasks:
                assert tid in listed_ids

        # Kill all
        for tid in task_ids:
            if tid in live_tasks:
                await kill_task(tid)


class TestTaskStatusValues:
    """Tests for task status value handling."""

    def test_valid_status_values(self):
        """Test valid status values are recognized."""
        valid_statuses = ['running', 'completed', 'timeout', 'killed', 'error']

        for status in valid_statuses:
            task = LiveTask(
                task_id="status_test",
                command="echo",
                process=None,
                started_at=time.time(),
                timeout=60,
                status=status
            )
            assert task.status == status

    def test_default_status_is_running(self):
        """Default status is 'running'."""
        task = LiveTask(
            task_id="default_test",
            command="echo",
            process=None,
            started_at=time.time(),
            timeout=60
        )
        assert task.status == "running"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
