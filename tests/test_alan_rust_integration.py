"""
Integration tests: Rust executor writes to ALAN DB, Python reads it.

Verifies the Phase 2 contract — Rust handles core recording,
Python handles insight generation from the same database.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from zsh_tool.alan import ALAN
from zsh_tool.config import EXEC_BINARY_PATH


RUST_EXEC = EXEC_BINARY_PATH
if RUST_EXEC is None:
    # Fallback: look for it directly
    _candidate = Path(__file__).parent.parent / "zsh-tool-rs" / "target" / "release" / "zsh-tool-exec"
    if _candidate.exists():
        RUST_EXEC = str(_candidate)

pytestmark = pytest.mark.skipif(
    RUST_EXEC is None, reason="Rust executor not found"
)


def _run_rust(db_path: str, meta_path: str, command: str, session_id: str = "inttest"):
    """Run the Rust executor with ALAN recording enabled."""
    result = subprocess.run(
        [
            RUST_EXEC,
            "--meta", meta_path,
            "--db", db_path,
            "--session-id", session_id,
            "--", command,
        ],
        capture_output=True,
        timeout=30,
    )
    return result


class TestRustWritesPythonReads:
    """Rust executor writes observations; Python ALAN reads them."""

    def test_rust_writes_observation_python_reads(self):
        """Rust records an observation that Python can query."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "alan.db")
            meta = os.path.join(td, "meta.json")

            _run_rust(db, meta, "echo rust_integration_test")

            alan = ALAN(Path(db))
            stats = alan.get_pattern_stats("echo rust_integration_test")

            assert stats['known'] is True
            assert stats['observations'] >= 1
            assert stats['success_rate'] > 0.0

    def test_rust_records_streak_python_reads(self):
        """Rust records streaks that Python can query."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "alan.db")
            meta = os.path.join(td, "meta.json")

            # Run same command 3 times — Rust records streak
            for _ in range(3):
                os.unlink(meta) if os.path.exists(meta) else None
                _run_rust(db, meta, "echo streak_inttest")

            alan = ALAN(Path(db))
            streak = alan.get_streak("echo streak_inttest")

            assert streak['has_streak'] is True
            assert streak['current'] >= 3

    def test_rust_failure_python_sees_low_success_rate(self):
        """Rust records a failing command; Python sees low success rate."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "alan.db")
            meta = os.path.join(td, "meta.json")

            _run_rust(db, meta, "false")

            alan = ALAN(Path(db))
            stats = alan.get_pattern_stats("false")

            assert stats['known'] is True
            assert stats['success_rate'] == 0.0

    def test_rust_pipeline_segments_visible_to_python(self):
        """Rust records pipeline segments; Python sees per-segment stats."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "alan.db")
            meta = os.path.join(td, "meta.json")

            _run_rust(db, meta, "echo hello | cat")

            alan = ALAN(Path(db))
            # Full pipeline recorded
            stats = alan.get_pattern_stats("echo hello | cat")
            assert stats['known'] is True

            # Individual segments also recorded
            echo_stats = alan.get_pattern_stats("echo hello")
            cat_stats = alan.get_pattern_stats("cat")
            assert echo_stats['known'] is True
            assert cat_stats['known'] is True

    def test_python_insights_from_rust_data(self):
        """Python generates insights from data Rust recorded."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "alan.db")
            meta = os.path.join(td, "meta.json")

            # Record several successes so Python has pattern history
            for _ in range(5):
                os.unlink(meta) if os.path.exists(meta) else None
                _run_rust(db, meta, "echo insight_test")

            alan = ALAN(Path(db))
            insights = alan.get_insights("echo insight_test")

            # Should get pattern-based insights (not "New pattern")
            messages = [msg for _, msg in insights]
            joined = " ".join(messages)
            assert "New pattern" not in joined
            # Should see something about reliability or success
            assert any("success" in m.lower() or "reliable" in m.lower() or "streak" in m.lower()
                       for m in messages)
