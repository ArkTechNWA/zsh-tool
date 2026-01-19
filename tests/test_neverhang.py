"""
Tests for NEVERHANG Circuit Breaker (Issue #3).

Tests state transitions, timeout recording, recovery logic, and edge cases.
"""

import pytest
import time
from unittest.mock import patch

from zsh_tool.server import CircuitBreaker, CircuitState
from zsh_tool.config import NEVERHANG_FAILURE_THRESHOLD, NEVERHANG_RECOVERY_TIMEOUT, NEVERHANG_SAMPLE_WINDOW


@pytest.fixture
def breaker():
    """Create a fresh circuit breaker for each test."""
    return CircuitBreaker()


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_closed_value(self):
        """CLOSED state has correct value."""
        assert CircuitState.CLOSED.value == "closed"

    def test_open_value(self):
        """OPEN state has correct value."""
        assert CircuitState.OPEN.value == "open"

    def test_half_open_value(self):
        """HALF_OPEN state has correct value."""
        assert CircuitState.HALF_OPEN.value == "half_open"


class TestCircuitBreakerInit:
    """Tests for CircuitBreaker initialization."""

    def test_initial_state_closed(self, breaker):
        """Circuit starts in CLOSED state."""
        assert breaker.state == CircuitState.CLOSED

    def test_initial_failures_empty(self, breaker):
        """Failures list starts empty."""
        assert breaker.failures == []

    def test_initial_last_failure_none(self, breaker):
        """last_failure starts as None."""
        assert breaker.last_failure is None

    def test_initial_opened_at_none(self, breaker):
        """opened_at starts as None."""
        assert breaker.opened_at is None


class TestRecordTimeout:
    """Tests for record_timeout()."""

    def test_records_failure(self, breaker):
        """Timeout is recorded in failures list."""
        breaker.record_timeout("cmd_hash_1")
        assert len(breaker.failures) == 1
        assert breaker.failures[0][1] == "cmd_hash_1"

    def test_updates_last_failure(self, breaker):
        """last_failure timestamp is updated."""
        before = time.time()
        breaker.record_timeout("cmd_hash_1")
        after = time.time()
        assert before <= breaker.last_failure <= after

    def test_multiple_failures_accumulate(self, breaker):
        """Multiple timeouts accumulate."""
        breaker.record_timeout("cmd_1")
        breaker.record_timeout("cmd_2")
        assert len(breaker.failures) == 2

    def test_circuit_opens_at_threshold(self, breaker):
        """Circuit opens after NEVERHANG_FAILURE_THRESHOLD timeouts."""
        for i in range(NEVERHANG_FAILURE_THRESHOLD - 1):
            breaker.record_timeout(f"cmd_{i}")
        assert breaker.state == CircuitState.CLOSED

        breaker.record_timeout("final_cmd")
        assert breaker.state == CircuitState.OPEN
        assert breaker.opened_at is not None

    def test_old_failures_pruned(self, breaker):
        """Failures outside sample window are pruned."""
        old_time = time.time() - NEVERHANG_SAMPLE_WINDOW - 100

        with patch('time.time', return_value=old_time):
            breaker.record_timeout("old_cmd")

        # Now record with current time - old failure should be pruned
        breaker.record_timeout("new_cmd")
        assert len(breaker.failures) == 1
        assert breaker.failures[0][1] == "new_cmd"

    def test_circuit_stays_closed_if_failures_pruned(self, breaker):
        """Circuit doesn't open if old failures are pruned below threshold."""
        old_time = time.time() - NEVERHANG_SAMPLE_WINDOW - 100

        # Record old failures
        with patch('time.time', return_value=old_time):
            for i in range(NEVERHANG_FAILURE_THRESHOLD - 1):
                breaker.record_timeout(f"old_{i}")

        # Record one new failure - old ones should be pruned
        breaker.record_timeout("new_cmd")
        assert breaker.state == CircuitState.CLOSED
        assert len(breaker.failures) == 1


class TestRecordSuccess:
    """Tests for record_success()."""

    def test_no_effect_when_closed(self, breaker):
        """Success has no effect in CLOSED state."""
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    def test_closes_circuit_from_half_open(self, breaker):
        """Success in HALF_OPEN state closes circuit."""
        breaker.state = CircuitState.HALF_OPEN
        breaker.failures = [("time", "hash")]
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failures == []

    def test_no_effect_when_open(self, breaker):
        """Success has no effect in OPEN state (shouldn't happen normally)."""
        breaker.state = CircuitState.OPEN
        breaker.failures = [("time", "hash")]
        breaker.record_success()
        assert breaker.state == CircuitState.OPEN
        assert len(breaker.failures) == 1


class TestShouldAllow:
    """Tests for should_allow()."""

    def test_allows_when_closed(self, breaker):
        """Returns True with no message when CLOSED."""
        allowed, message = breaker.should_allow()
        assert allowed is True
        assert message is None

    def test_blocks_when_open(self, breaker):
        """Returns False with message when OPEN."""
        breaker.state = CircuitState.OPEN
        breaker.opened_at = time.time()
        breaker.failures = [(time.time(), "hash")] * 3

        allowed, message = breaker.should_allow()
        assert allowed is False
        assert "NEVERHANG: Circuit OPEN" in message
        assert "3 recent timeouts" in message

    def test_allows_when_half_open(self, breaker):
        """Returns True with monitoring message when HALF_OPEN."""
        breaker.state = CircuitState.HALF_OPEN

        allowed, message = breaker.should_allow()
        assert allowed is True
        assert "half-open" in message
        assert "monitoring" in message

    def test_transitions_open_to_half_open_after_recovery(self, breaker):
        """OPEN transitions to HALF_OPEN after recovery timeout."""
        breaker.state = CircuitState.OPEN
        breaker.opened_at = time.time() - NEVERHANG_RECOVERY_TIMEOUT - 1

        allowed, message = breaker.should_allow()
        assert allowed is True
        assert breaker.state == CircuitState.HALF_OPEN
        assert "testing recovery" in message

    def test_stays_open_before_recovery_timeout(self, breaker):
        """OPEN stays OPEN before recovery timeout passes."""
        breaker.state = CircuitState.OPEN
        breaker.opened_at = time.time() - (NEVERHANG_RECOVERY_TIMEOUT / 2)
        breaker.failures = [(time.time(), "hash")]

        allowed, message = breaker.should_allow()
        assert allowed is False
        assert breaker.state == CircuitState.OPEN


class TestGetStatus:
    """Tests for get_status()."""

    def test_status_closed(self, breaker):
        """Status reports CLOSED state correctly."""
        status = breaker.get_status()
        assert status['state'] == 'closed'
        assert status['recent_failures'] == 0
        assert status['failure_threshold'] == NEVERHANG_FAILURE_THRESHOLD
        assert status['recovery_timeout'] == NEVERHANG_RECOVERY_TIMEOUT
        assert status['opened_at'] is None

    def test_status_open(self, breaker):
        """Status reports OPEN state correctly."""
        now = time.time()
        breaker.state = CircuitState.OPEN
        breaker.opened_at = now
        breaker.failures = [(now, "h1"), (now, "h2"), (now, "h3")]

        status = breaker.get_status()
        assert status['state'] == 'open'
        assert status['recent_failures'] == 3
        assert status['opened_at'] == now
        assert status['time_until_retry'] is not None
        assert status['time_until_retry'] > 0

    def test_status_half_open(self, breaker):
        """Status reports HALF_OPEN state correctly."""
        breaker.state = CircuitState.HALF_OPEN

        status = breaker.get_status()
        assert status['state'] == 'half_open'


class TestStateTransitions:
    """Tests for full state transition cycles."""

    def test_closed_to_open_to_half_open_to_closed(self, breaker):
        """Full cycle: CLOSED → OPEN → HALF_OPEN → CLOSED."""
        # Start CLOSED
        assert breaker.state == CircuitState.CLOSED

        # Trigger enough failures to open
        for i in range(NEVERHANG_FAILURE_THRESHOLD):
            breaker.record_timeout(f"cmd_{i}")
        assert breaker.state == CircuitState.OPEN

        # Simulate recovery timeout passing
        breaker.opened_at = time.time() - NEVERHANG_RECOVERY_TIMEOUT - 1
        allowed, _ = breaker.should_allow()
        assert breaker.state == CircuitState.HALF_OPEN

        # Success closes it
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failures == []

    def test_half_open_to_open_on_failure(self, breaker):
        """HALF_OPEN → OPEN if failure occurs during test."""
        breaker.state = CircuitState.HALF_OPEN

        # Record enough failures to trip threshold again
        for i in range(NEVERHANG_FAILURE_THRESHOLD):
            breaker.record_timeout(f"cmd_{i}")

        assert breaker.state == CircuitState.OPEN


class TestEdgeCases:
    """Edge case tests."""

    def test_rapid_timeouts(self, breaker):
        """Rapid consecutive timeouts are handled correctly."""
        for i in range(10):
            breaker.record_timeout(f"rapid_{i}")

        assert breaker.state == CircuitState.OPEN
        assert len(breaker.failures) == 10

    def test_exact_threshold(self, breaker):
        """Exactly at threshold opens circuit."""
        for i in range(NEVERHANG_FAILURE_THRESHOLD):
            breaker.record_timeout(f"cmd_{i}")

        assert breaker.state == CircuitState.OPEN
        assert len(breaker.failures) == NEVERHANG_FAILURE_THRESHOLD

    def test_opened_at_none_in_time_calculation(self, breaker):
        """Handle opened_at=None in time calculations gracefully."""
        breaker.state = CircuitState.OPEN
        breaker.opened_at = None  # Edge case

        # This shouldn't crash - returns None when opened_at is None
        status = breaker.get_status()
        assert status['time_until_retry'] is None  # Correct: no retry time without opened_at

    def test_multiple_command_hashes(self, breaker):
        """Different command hashes are all tracked."""
        breaker.record_timeout("hash_a")
        breaker.record_timeout("hash_b")
        breaker.record_timeout("hash_c")

        hashes = [h for _, h in breaker.failures]
        assert "hash_a" in hashes
        assert "hash_b" in hashes
        assert "hash_c" in hashes


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
