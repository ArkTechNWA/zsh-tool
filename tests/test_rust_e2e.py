"""
End-to-end tests: MCP handler -> Rust executor -> output.

Verifies the full stack works when Rust executor is enabled.
"""
import pytest

from zsh_tool.config import EXEC_BINARY_PATH
from zsh_tool.server import _handle_tool_call
from zsh_tool.tasks import circuit_breaker
from zsh_tool.neverhang import CircuitState


pytestmark = pytest.mark.skipif(
    EXEC_BINARY_PATH is None,
    reason="Rust executor not built"
)


def _reset_circuit():
    circuit_breaker.state = CircuitState.CLOSED
    circuit_breaker.failures = []


@pytest.mark.asyncio
class TestRustE2E:
    """End-to-end tests through MCP handler with Rust executor."""

    async def test_echo_through_mcp(self):
        """echo command produces clean output through full stack."""
        _reset_circuit()
        result = await _handle_tool_call("zsh", {
            "command": "echo e2e_test",
            "yield_after": 3
        })
        text = result[0].text
        assert "e2e_test" in text
        assert "PIPESTATUS_MARKER" not in text

    async def test_pipeline_through_mcp(self):
        """Pipeline pipestatus captured correctly through full stack."""
        _reset_circuit()
        result = await _handle_tool_call("zsh", {
            "command": "echo hello | grep hello",
            "yield_after": 3
        })
        text = result[0].text
        assert "hello" in text
        assert "COMPLETED" in text

    async def test_pty_through_mcp(self):
        """PTY mode works through full stack."""
        _reset_circuit()
        result = await _handle_tool_call("zsh", {
            "command": "echo pty_e2e",
            "pty": True,
            "yield_after": 3
        })
        text = result[0].text
        assert "pty_e2e" in text

    async def test_no_trailing_newline_preserved(self):
        """Output without trailing newline is preserved (Issue #41 regression)."""
        _reset_circuit()
        result = await _handle_tool_call("zsh", {
            "command": "printf 'no_newline_here'",
            "yield_after": 3
        })
        text = result[0].text
        assert "no_newline_here" in text
