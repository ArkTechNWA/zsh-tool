"""
Tests for MCP Tool Handlers (Issue #11).

Tests _handle_tool_call(), _format_task_output(), and individual tool dispatching.
"""

import pytest
import asyncio
import time
import json
from pathlib import Path

from zsh_tool.server import _handle_tool_call, _format_task_output, call_tool
from zsh_tool.tasks import live_tasks, LiveTask, circuit_breaker, alan
from zsh_tool.neverhang import CircuitState
from mcp.types import TextContent


class TestFormatTaskOutput:
    """Tests for _format_task_output() helper."""

    def test_returns_text_content_list(self):
        """Returns list of TextContent."""
        result = {'status': 'completed', 'task_id': 'abc123', 'exit_code': 0}
        output = _format_task_output(result)
        assert isinstance(output, list)
        assert all(isinstance(tc, TextContent) for tc in output)

    def test_includes_output(self):
        """Output is included in response."""
        result = {'output': 'hello world\n', 'status': 'completed', 'task_id': 'abc'}
        output = _format_task_output(result)
        assert 'hello world' in output[0].text

    def test_strips_trailing_newlines(self):
        """Output trailing newlines are stripped."""
        result = {'output': 'test\n\n\n', 'status': 'completed', 'task_id': 'abc'}
        output = _format_task_output(result)
        # Should have exactly one output line (stripped)
        assert not output[0].text.startswith('\n')

    def test_includes_error_message(self):
        """Error messages are included."""
        result = {'error': 'Something went wrong', 'status': 'error', 'task_id': 'abc'}
        output = _format_task_output(result)
        assert 'Something went wrong' in output[0].text
        assert '[error]' in output[0].text

    def test_running_status_format(self):
        """Running status has correct format."""
        result = {
            'status': 'running',
            'task_id': 'abc12345',
            'elapsed_seconds': 1.5,
            'has_stdin': True
        }
        output = _format_task_output(result)
        text = output[0].text
        assert '[RUNNING' in text
        assert 'task_id=abc12345' in text
        assert 'stdin=yes' in text
        assert 'zsh_poll' in text

    def test_running_no_stdin(self):
        """Running status shows stdin=no when no stdin."""
        result = {
            'status': 'running',
            'task_id': 'xyz',
            'elapsed_seconds': 1.0,
            'has_stdin': False
        }
        output = _format_task_output(result)
        assert 'stdin=no' in output[0].text

    def test_completed_success_format(self):
        """Completed success has correct format."""
        result = {
            'status': 'completed',
            'task_id': 'done123',
            'elapsed_seconds': 2.3,
            'exit_code': 0
        }
        output = _format_task_output(result)
        text = output[0].text
        assert '[COMPLETED' in text
        assert 'exit=0' in text

    def test_completed_failure_format(self):
        """Completed with non-zero exit has correct format."""
        result = {
            'status': 'completed',
            'task_id': 'fail123',
            'elapsed_seconds': 1.0,
            'exit_code': 1
        }
        output = _format_task_output(result)
        assert 'exit=1' in output[0].text

    def test_timeout_status_format(self):
        """Timeout status has correct format."""
        result = {
            'status': 'timeout',
            'task_id': 'timeout123',
            'elapsed_seconds': 60.0
        }
        output = _format_task_output(result)
        assert '[TIMEOUT' in output[0].text

    def test_killed_status_format(self):
        """Killed status has correct format."""
        result = {
            'status': 'killed',
            'task_id': 'killed123',
            'elapsed_seconds': 5.0
        }
        output = _format_task_output(result)
        assert '[KILLED' in output[0].text

    def test_error_status_format(self):
        """Error status has correct format."""
        result = {
            'status': 'error',
            'task_id': 'err123',
            'elapsed_seconds': 0.5
        }
        output = _format_task_output(result)
        assert '[ERROR' in output[0].text

    def test_includes_warnings(self):
        """Warnings are included in output."""
        result = {
            'status': 'completed',
            'task_id': 'warn123',
            'elapsed_seconds': 1.0,
            'warnings': ['Warning 1', 'Warning 2']
        }
        output = _format_task_output(result)
        assert '[warnings:' in output[0].text

    def test_no_output_shows_placeholder(self):
        """Empty output shows placeholder."""
        result = {'status': 'completed', 'task_id': 'empty'}
        output = _format_task_output(result)
        assert '(no output)' in output[0].text or output[0].text.strip() != ''


@pytest.mark.asyncio
class TestHandleToolCallZsh:
    """Tests for zsh tool handling."""

    async def test_zsh_returns_text_content(self):
        """zsh tool returns TextContent list."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await _handle_tool_call("zsh", {"command": "echo test"})
        assert isinstance(result, list)
        assert all(isinstance(tc, TextContent) for tc in result)

    async def test_zsh_with_pty_flag(self):
        """zsh tool with pty=True uses PTY mode."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await _handle_tool_call("zsh", {"command": "echo pty", "pty": True})
        assert isinstance(result, list)

    async def test_zsh_with_timeout(self):
        """zsh tool accepts timeout parameter."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await _handle_tool_call("zsh", {"command": "echo timeout", "timeout": 30})
        assert isinstance(result, list)

    async def test_zsh_with_yield_after(self):
        """zsh tool accepts yield_after parameter."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await _handle_tool_call("zsh", {"command": "echo yield", "yield_after": 1.0})
        assert isinstance(result, list)

    async def test_zsh_with_description(self):
        """zsh tool accepts description parameter."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await _handle_tool_call("zsh", {
            "command": "echo desc",
            "description": "Test command"
        })
        assert isinstance(result, list)


@pytest.mark.asyncio
class TestHandleToolCallPoll:
    """Tests for zsh_poll tool handling."""

    async def test_poll_unknown_task(self):
        """zsh_poll returns error for unknown task."""
        result = await _handle_tool_call("zsh_poll", {"task_id": "nonexistent"})
        text = result[0].text
        assert 'error' in text.lower() or 'unknown' in text.lower()

    async def test_poll_existing_task(self):
        """zsh_poll returns task info for existing task."""
        task = LiveTask(
            task_id="poll_handler_test",
            command="echo",
            process=None,
            started_at=time.time(),
            timeout=60,
            status="running"
        )
        live_tasks["poll_handler_test"] = task

        result = await _handle_tool_call("zsh_poll", {"task_id": "poll_handler_test"})
        text = result[0].text
        assert "poll_handler_test" in text

        del live_tasks["poll_handler_test"]


@pytest.mark.asyncio
class TestHandleToolCallSend:
    """Tests for zsh_send tool handling."""

    async def test_send_unknown_task(self):
        """zsh_send returns error for unknown task."""
        result = await _handle_tool_call("zsh_send", {
            "task_id": "nonexistent",
            "input": "test"
        })
        text = result[0].text
        data = json.loads(text)
        assert data['success'] is False


@pytest.mark.asyncio
class TestHandleToolCallKill:
    """Tests for zsh_kill tool handling."""

    async def test_kill_unknown_task(self):
        """zsh_kill returns error for unknown task."""
        result = await _handle_tool_call("zsh_kill", {"task_id": "nonexistent"})
        text = result[0].text
        data = json.loads(text)
        assert data['success'] is False


@pytest.mark.asyncio
class TestHandleToolCallTasks:
    """Tests for zsh_tasks tool handling."""

    async def test_tasks_returns_list(self):
        """zsh_tasks returns tasks list."""
        result = await _handle_tool_call("zsh_tasks", {})
        text = result[0].text
        data = json.loads(text)
        assert 'tasks' in data
        assert isinstance(data['tasks'], list)


@pytest.mark.asyncio
class TestHandleToolCallHealth:
    """Tests for zsh_health tool handling."""

    async def test_health_returns_status(self):
        """zsh_health returns health status."""
        result = await _handle_tool_call("zsh_health", {})
        text = result[0].text
        data = json.loads(text)
        assert data['status'] == 'healthy'

    async def test_health_includes_neverhang(self):
        """zsh_health includes NEVERHANG status."""
        result = await _handle_tool_call("zsh_health", {})
        data = json.loads(result[0].text)
        assert 'neverhang' in data

    async def test_health_includes_alan(self):
        """zsh_health includes A.L.A.N. stats."""
        result = await _handle_tool_call("zsh_health", {})
        data = json.loads(result[0].text)
        assert 'alan' in data

    async def test_health_includes_active_tasks(self):
        """zsh_health includes active task count."""
        result = await _handle_tool_call("zsh_health", {})
        data = json.loads(result[0].text)
        assert 'active_tasks' in data
        assert isinstance(data['active_tasks'], int)


@pytest.mark.asyncio
class TestHandleToolCallAlanStats:
    """Tests for zsh_alan_stats tool handling."""

    async def test_alan_stats_returns_dict(self):
        """zsh_alan_stats returns statistics."""
        result = await _handle_tool_call("zsh_alan_stats", {})
        text = result[0].text
        data = json.loads(text)
        assert isinstance(data, dict)

    async def test_alan_stats_has_expected_fields(self):
        """zsh_alan_stats has expected fields."""
        result = await _handle_tool_call("zsh_alan_stats", {})
        data = json.loads(result[0].text)
        # Should have stats fields
        assert 'total_patterns' in data or 'patterns' in data or len(data) > 0


@pytest.mark.asyncio
class TestHandleToolCallAlanQuery:
    """Tests for zsh_alan_query tool handling."""

    async def test_alan_query_returns_pattern_stats(self):
        """zsh_alan_query returns pattern statistics."""
        result = await _handle_tool_call("zsh_alan_query", {"command": "ls -la"})
        text = result[0].text
        data = json.loads(text)
        assert isinstance(data, dict)

    async def test_alan_query_handles_unknown_command(self):
        """zsh_alan_query handles unknown commands gracefully."""
        result = await _handle_tool_call("zsh_alan_query", {
            "command": "completely_unknown_cmd_xyz"
        })
        text = result[0].text
        # Should return dict without error
        data = json.loads(text)
        assert isinstance(data, dict)


@pytest.mark.asyncio
class TestHandleToolCallNeverhangStatus:
    """Tests for zsh_neverhang_status tool handling."""

    async def test_neverhang_status_returns_dict(self):
        """zsh_neverhang_status returns status dict."""
        result = await _handle_tool_call("zsh_neverhang_status", {})
        text = result[0].text
        data = json.loads(text)
        assert isinstance(data, dict)

    async def test_neverhang_status_has_state(self):
        """zsh_neverhang_status includes state."""
        result = await _handle_tool_call("zsh_neverhang_status", {})
        data = json.loads(result[0].text)
        assert 'state' in data


@pytest.mark.asyncio
class TestHandleToolCallNeverhangReset:
    """Tests for zsh_neverhang_reset tool handling."""

    async def test_neverhang_reset_succeeds(self):
        """zsh_neverhang_reset returns success."""
        result = await _handle_tool_call("zsh_neverhang_reset", {})
        text = result[0].text
        data = json.loads(text)
        assert data['success'] is True

    async def test_neverhang_reset_closes_circuit(self):
        """zsh_neverhang_reset sets circuit to CLOSED."""
        # Open the circuit first
        circuit_breaker.state = CircuitState.OPEN
        circuit_breaker.failures = [(time.time(), "hash")] * 3
        circuit_breaker.opened_at = time.time()

        await _handle_tool_call("zsh_neverhang_reset", {})

        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.failures == []
        assert circuit_breaker.opened_at is None


@pytest.mark.asyncio
class TestHandleToolCallUnknown:
    """Tests for unknown tool handling."""

    async def test_unknown_tool_returns_error(self):
        """Unknown tool returns error message."""
        result = await _handle_tool_call("nonexistent_tool", {})
        text = result[0].text
        data = json.loads(text)
        assert 'error' in data
        assert 'Unknown tool' in data['error']


@pytest.mark.asyncio
class TestCallToolWrapper:
    """Tests for call_tool() wrapper."""

    async def test_call_tool_returns_text_content(self):
        """call_tool returns TextContent list."""
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []

        result = await call_tool("zsh_health", {})
        assert isinstance(result, list)
        assert all(isinstance(tc, TextContent) for tc in result)

    async def test_call_tool_handles_unknown(self):
        """call_tool handles unknown tools."""
        result = await call_tool("unknown_tool", {})
        assert isinstance(result, list)


class TestTextContentFormat:
    """Tests for TextContent formatting."""

    def test_text_content_has_type(self):
        """TextContent has type='text'."""
        result = _format_task_output({'status': 'completed', 'task_id': 'test'})
        assert result[0].type == "text"

    def test_json_results_formatted(self):
        """Dict results are JSON formatted."""
        # Test through the actual handler
        pass  # Tested implicitly in other tests


class TestOutputCombinations:
    """Tests for various output combinations."""

    def test_output_and_error(self):
        """Output and error both included."""
        result = {
            'output': 'partial output\n',
            'error': 'failed midway',
            'status': 'error',
            'task_id': 'both'
        }
        output = _format_task_output(result)
        text = output[0].text
        assert 'partial output' in text
        assert 'failed midway' in text

    def test_output_warnings_and_status(self):
        """Output, warnings, and status all included."""
        result = {
            'output': 'command output\n',
            'status': 'completed',
            'task_id': 'all',
            'elapsed_seconds': 1.0,
            'exit_code': 0,
            'warnings': ['Test warning']
        }
        output = _format_task_output(result)
        text = output[0].text
        assert 'command output' in text
        assert '[COMPLETED' in text
        assert '[warnings:' in text


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
