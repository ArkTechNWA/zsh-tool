"""
Tests for Rust executor integration (Phase 1).

These tests only run when ZSH_TOOL_EXEC is set or the binary exists.
"""
import asyncio
import json
import os
import tempfile

import pytest

from zsh_tool.config import EXEC_BINARY_PATH

# Skip entire module if no Rust binary
pytestmark = pytest.mark.skipif(
    EXEC_BINARY_PATH is None,
    reason="Rust executor not built (set ZSH_TOOL_EXEC or build zsh-tool-rs)"
)


@pytest.mark.asyncio
class TestRustExecutorDirect:
    """Test the Rust binary directly via subprocess."""

    async def test_basic_output(self):
        """Rust executor streams command output on stdout."""
        meta = tempfile.mktemp(suffix='.json')
        proc = await asyncio.create_subprocess_exec(
            EXEC_BINARY_PATH, "--meta", meta, "--", "echo hello rust",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert b"hello rust" in stdout

        with open(meta) as f:
            result = json.load(f)
        assert result["pipestatus"] == [0]
        assert result["exit_code"] == 0
        os.unlink(meta)

    async def test_no_metadata_in_output(self):
        """THE critical test: no pipestatus data in stdout."""
        meta = tempfile.mktemp(suffix='.json')
        proc = await asyncio.create_subprocess_exec(
            EXEC_BINARY_PATH, "--meta", meta, "--", "echo clean output",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        stdout_str = stdout.decode()

        assert "pipestatus" not in stdout_str.lower()
        assert "clean output" in stdout_str

        with open(meta) as f:
            result = json.load(f)
        assert "pipestatus" in result
        os.unlink(meta)

    async def test_pipeline_pipestatus(self):
        """Captures per-segment pipestatus for pipelines."""
        meta = tempfile.mktemp(suffix='.json')
        proc = await asyncio.create_subprocess_exec(
            EXEC_BINARY_PATH, "--meta", meta, "--", "false | true",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        with open(meta) as f:
            result = json.load(f)
        assert result["pipestatus"] == [1, 0]
        os.unlink(meta)

    async def test_nonzero_exit(self):
        """Reports correct exit code."""
        meta = tempfile.mktemp(suffix='.json')
        proc = await asyncio.create_subprocess_exec(
            EXEC_BINARY_PATH, "--meta", meta, "--", "exit 42",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        assert proc.returncode == 42

        with open(meta) as f:
            result = json.load(f)
        assert result["exit_code"] == 42
        os.unlink(meta)

    async def test_timeout(self):
        """Timeout kills command and reports timed_out=true."""
        meta = tempfile.mktemp(suffix='.json')
        proc = await asyncio.create_subprocess_exec(
            EXEC_BINARY_PATH, "--meta", meta, "--timeout", "2", "--", "sleep 60",
            stdout=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        with open(meta) as f:
            result = json.load(f)
        assert result["timed_out"] is True
        os.unlink(meta)
