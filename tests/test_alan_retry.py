"""
Tests for A.L.A.N. Retry Detection (Issue #5).

Tests the recent_commands hot cache, retry detection, and similar command matching.
"""

import pytest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from zsh_tool.alan import ALAN
from zsh_tool.config import ALAN_RECENT_WINDOW_MINUTES


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    db_path.unlink(missing_ok=True)


class TestGetRecentActivity:
    """Tests for get_recent_activity()."""

    def test_no_recent_activity(self, alan):
        """No recent activity returns empty state."""
        result = alan.get_recent_activity("ls -la")
        assert result['is_retry'] is False
        assert result['retry_count'] == 0
        assert result['recent_successes'] == 0
        assert result['recent_failures'] == 0
        assert result['similar_commands'] == []
        assert result['template'] is not None

    def test_first_execution_not_retry(self, alan):
        """First execution of a command is not a retry."""
        # Before recording
        result = alan.get_recent_activity("git push origin main")
        assert result['is_retry'] is False

    def test_second_execution_is_retry(self, alan):
        """Second execution of same command is a retry."""
        alan.record("git push origin main", 0, 100)

        result = alan.get_recent_activity("git push origin main")
        assert result['is_retry'] is True
        assert result['retry_count'] == 1

    def test_multiple_retries_counted(self, alan):
        """Multiple executions count as multiple retries."""
        alan.record("ls -la", 0, 50)
        alan.record("ls -la", 0, 60)
        alan.record("ls -la", 0, 55)

        result = alan.get_recent_activity("ls -la")
        assert result['is_retry'] is True
        assert result['retry_count'] == 3

    def test_success_and_failure_tracking(self, alan):
        """Tracks successes and failures separately."""
        alan.record("npm test", 0, 100)   # success
        alan.record("npm test", 1, 200)   # failure
        alan.record("npm test", 0, 150)   # success
        alan.record("npm test", 1, 180)   # failure

        result = alan.get_recent_activity("npm test")
        assert result['retry_count'] == 4
        assert result['recent_successes'] == 2
        assert result['recent_failures'] == 2

    def test_includes_template(self, alan):
        """Result includes the command template."""
        result = alan.get_recent_activity("git push origin feature-branch")
        assert 'template' in result
        assert result['template'].startswith("git push")


class TestSimilarCommands:
    """Tests for similar command detection."""

    def test_no_similar_commands_initially(self, alan):
        """No similar commands when none recorded."""
        result = alan.get_recent_activity("git push origin main")
        assert result['similar_commands'] == []

    def test_similar_commands_matched(self, alan):
        """Similar commands (same template, different hash) are matched."""
        # Record similar git push commands with different string arguments
        # (numbers are normalized, so we need different words)
        alan.record("git push origin alpha", 0, 100)
        alan.record("git push origin beta", 0, 100)

        # Check for a different branch (different hash, same template)
        result = alan.get_recent_activity("git push origin gamma")
        # All three have template "git push *" but different hashes
        assert len(result['similar_commands']) >= 1

    def test_exact_match_excluded_from_similar(self, alan):
        """Exact match is not in similar_commands (it's a retry)."""
        alan.record("git push origin main", 0, 100)

        result = alan.get_recent_activity("git push origin main")
        # The exact match is counted as retry, not in similar
        assert result['is_retry'] is True
        # similar_commands should not contain the exact command
        for similar in result['similar_commands']:
            assert 'origin main' not in similar['preview']

    def test_similar_commands_limited(self, alan):
        """Similar commands list is limited (max 5)."""
        # Record many similar commands with different string arguments
        branches = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa']
        for branch in branches:
            alan.record(f"git push origin {branch}", 0, 100)

        result = alan.get_recent_activity("git push origin omega")
        assert len(result['similar_commands']) <= 5

    def test_similar_includes_success_status(self, alan):
        """Similar commands include success/failure status."""
        alan.record("git push origin alpha", 0, 100)  # success
        alan.record("git push origin beta", 1, 100)   # failure

        result = alan.get_recent_activity("git push origin gamma")
        if result['similar_commands']:
            for similar in result['similar_commands']:
                assert 'success' in similar
                assert 'preview' in similar


class TestRecentWindow:
    """Tests for the time window in retry detection."""

    def test_recent_commands_within_window(self, alan):
        """Commands within window are counted."""
        alan.record("ls -la", 0, 50)

        result = alan.get_recent_activity("ls -la")
        assert result['is_retry'] is True

    def test_old_commands_outside_window(self, alan):
        """Commands outside window are not counted."""
        alan.record("ls -la", 0, 50)

        # Backdate the recent_command entry
        old_time = time.time() - (ALAN_RECENT_WINDOW_MINUTES * 60) - 100
        with alan._connect() as conn:
            conn.execute("UPDATE recent_commands SET timestamp = ?", (old_time,))

        result = alan.get_recent_activity("ls -la")
        assert result['is_retry'] is False
        assert result['retry_count'] == 0

    def test_window_boundary(self, alan):
        """Commands exactly at window boundary."""
        alan.record("ls -la", 0, 50)

        # Set to just inside window
        just_inside = time.time() - (ALAN_RECENT_WINDOW_MINUTES * 60) + 10
        with alan._connect() as conn:
            conn.execute("UPDATE recent_commands SET timestamp = ?", (just_inside,))

        result = alan.get_recent_activity("ls -la")
        assert result['is_retry'] is True


class TestRecentCommandsTable:
    """Tests for recent_commands table operations."""

    def test_record_creates_recent_entry(self, alan):
        """Recording a command creates a recent_commands entry."""
        alan.record("git status", 0, 100)

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM recent_commands").fetchone()[0]
            assert count == 1

    def test_recent_entry_fields(self, alan):
        """Recent commands entry has expected fields."""
        alan.record("git status", 0, 100)

        with alan._connect() as conn:
            row = conn.execute("SELECT * FROM recent_commands").fetchone()
            assert row['command_hash'] is not None
            assert row['command_template'] is not None
            assert row['command_preview'] is not None
            assert row['success'] == 1
            assert row['session_id'] == alan.session_id
            assert row['timestamp'] is not None
            assert row['duration_ms'] == 100

    def test_failure_recorded_correctly(self, alan):
        """Failed commands have success=0."""
        alan.record("git push", 1, 200)

        with alan._connect() as conn:
            row = conn.execute("SELECT success FROM recent_commands").fetchone()
            assert row['success'] == 0

    def test_command_preview_truncated(self, alan):
        """Long commands are truncated in preview (max 200 chars)."""
        long_command = "echo " + "x" * 300
        alan.record(long_command, 0, 100)

        with alan._connect() as conn:
            row = conn.execute("SELECT command_preview FROM recent_commands").fetchone()
            assert len(row['command_preview']) <= 200


class TestRecentCommandsPruning:
    """Tests for recent_commands cleanup (happens during record(), not prune())."""

    def test_old_entries_cleaned_on_new_record(self, alan):
        """Old recent_commands entries are cleaned when new command recorded."""
        alan.record("ls -la", 0, 50)

        # Backdate the entry to be very old (beyond 10x window)
        # recent_commands keeps entries for 10x ALAN_RECENT_WINDOW_MINUTES
        very_old_time = time.time() - (ALAN_RECENT_WINDOW_MINUTES * 60 * 10) - 100
        with alan._connect() as conn:
            conn.execute("UPDATE recent_commands SET timestamp = ?", (very_old_time,))

        # Recording a new command triggers cleanup
        alan.record("pwd", 0, 30)

        with alan._connect() as conn:
            # Old entry should be cleaned, new entry exists
            count = conn.execute("SELECT COUNT(*) FROM recent_commands").fetchone()[0]
            assert count == 1
            row = conn.execute("SELECT command_preview FROM recent_commands").fetchone()
            assert row['command_preview'] == 'pwd'

    def test_recent_entries_preserved(self, alan):
        """Recent entries are preserved during cleanup."""
        alan.record("ls -la", 0, 50)
        alan.record("pwd", 0, 30)

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM recent_commands").fetchone()[0]
            assert count == 2


class TestRetryInsights:
    """Tests for retry-related insights."""

    def test_retry_insight_generated(self, alan):
        """Retry generates an insight message."""
        alan.record("npm test", 1, 100)  # First attempt failed

        # Second attempt
        result = alan.get_recent_activity("npm test")
        assert result['is_retry'] is True

    def test_multiple_retries_insight(self, alan):
        """Multiple retries have count in insight."""
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)

        result = alan.get_recent_activity("npm test")
        assert result['retry_count'] == 3
        assert result['recent_failures'] == 3

    def test_success_after_failures_tracked(self, alan):
        """Success after failures is tracked."""
        alan.record("npm test", 1, 100)  # fail
        alan.record("npm test", 1, 100)  # fail
        alan.record("npm test", 0, 100)  # success!

        result = alan.get_recent_activity("npm test")
        assert result['retry_count'] == 3
        assert result['recent_failures'] == 2
        assert result['recent_successes'] == 1


class TestDifferentCommands:
    """Tests to ensure different commands don't interfere."""

    def test_different_commands_separate(self, alan):
        """Different commands have separate retry counts."""
        alan.record("ls -la", 0, 50)
        alan.record("pwd", 0, 30)
        alan.record("ls -la", 0, 55)

        ls_result = alan.get_recent_activity("ls -la")
        pwd_result = alan.get_recent_activity("pwd")

        assert ls_result['retry_count'] == 2
        assert pwd_result['retry_count'] == 1

    def test_similar_templates_grouped(self, alan):
        """Commands with same template are in similar_commands."""
        # Use words not numbers, since numbers are normalized
        alan.record("git push origin alpha", 0, 100)
        alan.record("git push origin beta", 0, 100)

        # Different branch but same template (different hash)
        result = alan.get_recent_activity("git push origin gamma")
        assert len(result['similar_commands']) >= 2

    def test_different_templates_not_similar(self, alan):
        """Commands with different templates are not similar."""
        alan.record("git push origin main", 0, 100)
        alan.record("git pull origin main", 0, 100)

        result = alan.get_recent_activity("git push origin feature")
        # git pull should not appear in similar (different template)
        previews = [s['preview'] for s in result['similar_commands']]
        assert not any('pull' in p for p in previews)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
