"""
Tests for A.L.A.N. Streak Tracking (Issue #6).

Tests streak detection, updates, and longest streak tracking.
"""

import pytest
import tempfile
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "zsh_tool"))

from server import ALAN, ALAN_STREAK_THRESHOLD


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    db_path.unlink(missing_ok=True)


class TestGetStreak:
    """Tests for get_streak()."""

    def test_no_streak_initially(self, alan):
        """No streak for unrecorded command."""
        result = alan.get_streak("ls -la")
        assert result['has_streak'] is False
        assert result['current'] == 0

    def test_streak_after_single_success(self, alan):
        """Single success creates streak of 1."""
        alan.record("ls -la", 0, 50)

        result = alan.get_streak("ls -la")
        assert result['has_streak'] is True
        assert result['current'] == 1
        assert result['longest_success'] == 1

    def test_streak_after_single_failure(self, alan):
        """Single failure creates negative streak of -1."""
        alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert result['has_streak'] is True
        assert result['current'] == -1
        assert result['longest_fail'] == 1

    def test_streak_includes_all_fields(self, alan):
        """get_streak returns all expected fields."""
        alan.record("git status", 0, 100)

        result = alan.get_streak("git status")
        assert 'has_streak' in result
        assert 'current' in result
        assert 'longest_success' in result
        assert 'longest_fail' in result
        assert 'last_was_success' in result


class TestSuccessStreak:
    """Tests for success streak accumulation."""

    def test_consecutive_successes_accumulate(self, alan):
        """Consecutive successes increase streak."""
        alan.record("ls -la", 0, 50)
        alan.record("ls -la", 0, 50)
        alan.record("ls -la", 0, 50)

        result = alan.get_streak("ls -la")
        assert result['current'] == 3

    def test_success_streak_is_positive(self, alan):
        """Success streaks are positive numbers."""
        alan.record("git status", 0, 50)
        alan.record("git status", 0, 50)

        result = alan.get_streak("git status")
        assert result['current'] > 0

    def test_longest_success_streak_tracked(self, alan):
        """Longest success streak is tracked."""
        # Build a streak of 5
        for _ in range(5):
            alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['longest_success'] == 5

    def test_longest_success_preserved_after_failure(self, alan):
        """Longest success streak is preserved after failure."""
        # Build a streak of 4
        for _ in range(4):
            alan.record("npm test", 0, 100)

        # Break the streak
        alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert result['longest_success'] == 4
        assert result['current'] == -1


class TestFailureStreak:
    """Tests for failure streak accumulation."""

    def test_consecutive_failures_accumulate(self, alan):
        """Consecutive failures increase negative streak."""
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert result['current'] == -3

    def test_failure_streak_is_negative(self, alan):
        """Failure streaks are negative numbers."""
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert result['current'] < 0

    def test_longest_fail_streak_tracked(self, alan):
        """Longest failure streak is tracked."""
        for _ in range(4):
            alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert result['longest_fail'] == 4

    def test_longest_fail_preserved_after_success(self, alan):
        """Longest failure streak is preserved after success."""
        # Build a failure streak
        for _ in range(3):
            alan.record("npm test", 1, 100)

        # Break with success
        alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['longest_fail'] == 3
        assert result['current'] == 1


class TestStreakTransitions:
    """Tests for streak state transitions."""

    def test_success_to_failure_resets_streak(self, alan):
        """Failure after success resets to -1."""
        alan.record("npm test", 0, 100)
        alan.record("npm test", 0, 100)
        alan.record("npm test", 1, 100)  # failure

        result = alan.get_streak("npm test")
        assert result['current'] == -1

    def test_failure_to_success_resets_streak(self, alan):
        """Success after failure resets to +1."""
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)
        alan.record("npm test", 0, 100)  # success

        result = alan.get_streak("npm test")
        assert result['current'] == 1

    def test_alternating_results_no_accumulation(self, alan):
        """Alternating results don't accumulate streak."""
        alan.record("npm test", 0, 100)
        alan.record("npm test", 1, 100)
        alan.record("npm test", 0, 100)
        alan.record("npm test", 1, 100)

        result = alan.get_streak("npm test")
        assert abs(result['current']) == 1

    def test_last_was_success_tracked(self, alan):
        """Last success state is tracked via last_was_success."""
        alan.record("npm test", 0, 100)  # success (exit 0)
        result = alan.get_streak("npm test")
        assert result['last_was_success'] is True

        alan.record("npm test", 1, 100)  # failure (exit 1)
        result = alan.get_streak("npm test")
        assert result['last_was_success'] is False


class TestStreakTable:
    """Tests for streaks table operations."""

    def test_streak_row_created(self, alan):
        """Recording creates a streak row."""
        alan.record("git status", 0, 100)

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM streaks").fetchone()[0]
            assert count == 1

    def test_streak_row_updated(self, alan):
        """Subsequent records update existing row."""
        alan.record("git status", 0, 100)
        alan.record("git status", 0, 100)

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM streaks").fetchone()[0]
            assert count == 1  # Still just one row

    def test_different_commands_separate_streaks(self, alan):
        """Different commands have separate streak records."""
        alan.record("git status", 0, 100)
        alan.record("ls -la", 0, 50)

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM streaks").fetchone()[0]
            assert count == 2

    def test_last_updated_tracked(self, alan):
        """last_updated timestamp is recorded."""
        alan.record("git status", 0, 100)

        with alan._connect() as conn:
            row = conn.execute("SELECT last_updated FROM streaks").fetchone()
            assert row['last_updated'] is not None


class TestStreakThreshold:
    """Tests for streak threshold reporting."""

    def test_threshold_constant_exists(self):
        """ALAN_STREAK_THRESHOLD constant is defined."""
        assert ALAN_STREAK_THRESHOLD >= 1

    def test_streak_below_threshold(self, alan):
        """Streaks below threshold are still tracked."""
        for _ in range(ALAN_STREAK_THRESHOLD - 1):
            alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['current'] == ALAN_STREAK_THRESHOLD - 1

    def test_streak_at_threshold(self, alan):
        """Streaks at threshold are tracked."""
        for _ in range(ALAN_STREAK_THRESHOLD):
            alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['current'] == ALAN_STREAK_THRESHOLD

    def test_streak_above_threshold(self, alan):
        """Streaks above threshold are tracked."""
        for _ in range(ALAN_STREAK_THRESHOLD + 2):
            alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['current'] == ALAN_STREAK_THRESHOLD + 2


class TestStreakWithHashNormalization:
    """Tests for streak with command hash normalization."""

    def test_normalized_commands_share_streak(self, alan):
        """Commands that normalize to same hash share streak."""
        alan.record("sleep 5", 0, 50)
        alan.record("sleep 10", 0, 50)
        alan.record("sleep 15", 0, 50)

        # All normalize to same hash (numbers -> N)
        result = alan.get_streak("sleep 20")
        assert result['current'] == 3

    def test_different_commands_separate_streaks(self, alan):
        """Commands with different hashes have separate streaks."""
        alan.record("git push origin main", 0, 100)
        alan.record("git pull origin main", 0, 100)

        push_streak = alan.get_streak("git push origin main")
        pull_streak = alan.get_streak("git pull origin main")

        assert push_streak['current'] == 1
        assert pull_streak['current'] == 1


class TestComplexStreakScenarios:
    """Tests for complex streak scenarios."""

    def test_build_and_break_multiple_streaks(self, alan):
        """Can build and break multiple streaks over time."""
        # Build success streak
        for _ in range(3):
            alan.record("npm test", 0, 100)

        # Break it with a failure streak of 2 (need 2+ to update longest_fail)
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)

        # Build another success streak
        for _ in range(5):
            alan.record("npm test", 0, 100)

        result = alan.get_streak("npm test")
        assert result['current'] == 5
        assert result['longest_success'] == 5
        assert result['longest_fail'] == 2  # 2 consecutive failures

    def test_alternating_long_streaks(self, alan):
        """Handles alternating long success/failure streaks."""
        # 4 successes
        for _ in range(4):
            alan.record("make test", 0, 100)

        # 3 failures
        for _ in range(3):
            alan.record("make test", 1, 100)

        # 6 successes
        for _ in range(6):
            alan.record("make test", 0, 100)

        result = alan.get_streak("make test")
        assert result['current'] == 6
        assert result['longest_success'] == 6
        assert result['longest_fail'] == 3

    def test_many_commands_many_streaks(self, alan):
        """Can track streaks for many different commands."""
        commands = ['ls', 'pwd', 'git status', 'npm test', 'cargo build']

        for cmd in commands:
            for _ in range(3):
                alan.record(cmd, 0, 100)

        for cmd in commands:
            result = alan.get_streak(cmd)
            assert result['current'] == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
