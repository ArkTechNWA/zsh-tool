"""
Tests for A.L.A.N. Insights (non-SSH) (Issue #7).

Tests proactive insight generation based on retries, streaks, patterns, and similar commands.
"""

import pytest
import tempfile
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "zsh_tool"))

from server import ALAN, ALAN_RECENT_WINDOW_MINUTES, ALAN_STREAK_THRESHOLD


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    db_path.unlink(missing_ok=True)


class TestGetInsights:
    """Tests for get_insights() method."""

    def test_new_pattern_insight(self, alan):
        """New command shows 'no history' insight."""
        insights = alan.get_insights("brand-new-command")
        assert any("New pattern" in i for i in insights)

    def test_returns_list(self, alan):
        """get_insights returns a list."""
        insights = alan.get_insights("ls -la")
        assert isinstance(insights, list)

    def test_insight_strings(self, alan):
        """All insights are strings."""
        alan.record("npm test", 0, 100)
        insights = alan.get_insights("npm test")
        assert all(isinstance(i, str) for i in insights)


class TestRetryInsights:
    """Tests for retry-related insights."""

    def test_retry_insight_on_second_run(self, alan):
        """Shows retry insight on second run."""
        alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        assert any("Retry" in i for i in insights)

    def test_retry_all_failed_insight(self, alan):
        """Shows 'all failed' insight when all previous runs failed."""
        alan.record("npm test", 1, 100)
        alan.record("npm test", 1, 100)

        insights = alan.get_insights("npm test")
        assert any("all failed" in i.lower() or "different approach" in i.lower() for i in insights)

    def test_retry_all_succeeded_insight(self, alan):
        """Shows 'succeeded' insight when all previous runs succeeded."""
        alan.record("npm test", 0, 100)
        alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        assert any("succeeded" in i.lower() for i in insights)

    def test_retry_mixed_results_insight(self, alan):
        """Shows mixed results insight."""
        alan.record("npm test", 0, 100)  # success
        alan.record("npm test", 1, 100)  # failure
        alan.record("npm test", 0, 100)  # success

        insights = alan.get_insights("npm test")
        # Should show X/Y succeeded
        assert any("/" in i and "succeeded" in i.lower() for i in insights)

    def test_retry_count_in_insight(self, alan):
        """Retry number is included in insight."""
        alan.record("npm test", 0, 100)
        alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        # Should show "Retry #3"
        assert any("#3" in i for i in insights)


class TestSimilarCommandInsights:
    """Tests for similar command insights."""

    def test_similar_command_insight(self, alan):
        """Shows insight about similar commands."""
        # Record similar commands with different hashes
        alan.record("git push origin alpha", 0, 100)
        alan.record("git push origin beta", 0, 100)

        insights = alan.get_insights("git push origin gamma")
        # Should show something about similar commands
        assert any("similar" in i.lower() for i in insights)

    def test_similar_command_shows_success_rate(self, alan):
        """Similar command insight shows success rate."""
        alan.record("git push origin alpha", 0, 100)  # success
        alan.record("git push origin beta", 1, 100)   # failure

        insights = alan.get_insights("git push origin gamma")
        # Should show X/Y succeeded
        similar_insights = [i for i in insights if "similar" in i.lower()]
        if similar_insights:
            assert any("/" in i for i in similar_insights)


class TestStreakInsights:
    """Tests for streak-related insights."""

    def test_success_streak_insight(self, alan):
        """Shows insight for success streaks at threshold."""
        for _ in range(ALAN_STREAK_THRESHOLD):
            alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        assert any("streak" in i.lower() and "success" in i.lower() for i in insights)

    def test_failure_streak_insight(self, alan):
        """Shows insight for failure streaks at threshold."""
        for _ in range(ALAN_STREAK_THRESHOLD):
            alan.record("npm test", 1, 100)

        insights = alan.get_insights("npm test")
        assert any("fail" in i.lower() and "streak" in i.lower() for i in insights)

    def test_no_streak_insight_below_threshold(self, alan):
        """No streak insight when below threshold."""
        for _ in range(ALAN_STREAK_THRESHOLD - 1):
            alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        # Should not have streak insight
        streak_insights = [i for i in insights if "streak" in i.lower() and "solid" in i.lower()]
        assert len(streak_insights) == 0


class TestPatternStatsInsights:
    """Tests for pattern statistics insights."""

    def test_high_timeout_rate_insight(self, alan):
        """Shows insight for high timeout rate."""
        # Record commands with timeouts
        alan.record("slow-command", 0, 100, timed_out=True)
        alan.record("slow-command", 0, 100, timed_out=True)
        alan.record("slow-command", 0, 100, timed_out=False)

        insights = alan.get_insights("slow-command")
        assert any("timeout" in i.lower() for i in insights)

    def test_reliable_pattern_insight(self, alan):
        """Shows insight for reliable patterns."""
        # Record many successful runs
        for _ in range(10):
            alan.record("ls -la", 0, 50)

        insights = alan.get_insights("ls -la")
        assert any("reliable" in i.lower() for i in insights)

    def test_average_duration_insight(self, alan):
        """Shows insight for average duration (>10s to trigger insight)."""
        # Record commands with duration > 10 seconds
        alan.record("sleep-command", 0, 15000)  # 15 seconds
        alan.record("sleep-command", 0, 14000)  # 14 seconds
        alan.record("sleep-command", 0, 16000)  # 16 seconds

        insights = alan.get_insights("sleep-command")
        assert any("takes" in i.lower() and "s" in i for i in insights)


class TestInsightCombinations:
    """Tests for multiple insights combining."""

    def test_multiple_insights(self, alan):
        """Can generate multiple insights at once."""
        # Build history for multiple insights
        for _ in range(5):
            alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        # Should have retry insight and possibly streak insight
        assert len(insights) >= 2

    def test_new_command_only_new_pattern(self, alan):
        """New command only shows 'new pattern' insight."""
        insights = alan.get_insights("completely-new-command-never-seen")
        assert len(insights) == 1
        assert "New pattern" in insights[0]


class TestInsightFormatting:
    """Tests for insight message formatting."""

    def test_retry_format(self, alan):
        """Retry insights have correct format."""
        alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        retry_insights = [i for i in insights if "Retry" in i]
        if retry_insights:
            # Should match pattern like "Retry #2..."
            assert retry_insights[0].startswith("Retry #")

    def test_streak_format(self, alan):
        """Streak insights have correct format."""
        for _ in range(ALAN_STREAK_THRESHOLD):
            alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        streak_insights = [i for i in insights if "Streak:" in i]
        if streak_insights:
            assert any(char.isdigit() for char in streak_insights[0])

    def test_timeout_rate_format(self, alan):
        """Timeout rate insights show percentage."""
        alan.record("slow-cmd", 0, 100, timed_out=True)
        alan.record("slow-cmd", 0, 100, timed_out=True)

        insights = alan.get_insights("slow-cmd")
        timeout_insights = [i for i in insights if "timeout" in i.lower()]
        if timeout_insights:
            assert "%" in timeout_insights[0]


class TestInsightWithTimeout:
    """Tests for insight generation with timeout parameter."""

    def test_accepts_timeout_parameter(self, alan):
        """get_insights accepts timeout parameter."""
        # Should not raise
        insights = alan.get_insights("ls -la", timeout=60)
        assert isinstance(insights, list)

    def test_different_timeouts_same_insights(self, alan):
        """Different timeout values give same insights."""
        alan.record("npm test", 0, 100)

        insights_60 = alan.get_insights("npm test", timeout=60)
        insights_120 = alan.get_insights("npm test", timeout=120)

        # Should be the same (timeout doesn't affect insight content)
        assert insights_60 == insights_120


class TestEdgeCases:
    """Edge case tests for insights."""

    def test_empty_command(self, alan):
        """Empty command handled gracefully."""
        insights = alan.get_insights("")
        assert isinstance(insights, list)

    def test_whitespace_command(self, alan):
        """Whitespace-only command handled gracefully."""
        insights = alan.get_insights("   ")
        assert isinstance(insights, list)

    def test_very_long_command(self, alan):
        """Very long command handled gracefully."""
        long_cmd = "echo " + "x" * 1000
        insights = alan.get_insights(long_cmd)
        assert isinstance(insights, list)

    def test_command_with_special_chars(self, alan):
        """Command with special characters handled."""
        insights = alan.get_insights("echo 'hello world' | grep 'hello'")
        assert isinstance(insights, list)


class TestNonSSHCommands:
    """Tests specifically for non-SSH commands (no SSH insights)."""

    def test_non_ssh_no_host_insights(self, alan):
        """Non-SSH commands don't show SSH host insights."""
        alan.record("npm test", 0, 100)

        insights = alan.get_insights("npm test")
        # Should not contain SSH-specific language
        assert not any("host" in i.lower() and "connection" in i.lower() for i in insights)

    def test_non_ssh_no_remote_command_insights(self, alan):
        """Non-SSH commands don't show remote command insights."""
        alan.record("git push", 0, 100)

        insights = alan.get_insights("git push")
        assert not any("remote command" in i.lower() for i in insights)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
