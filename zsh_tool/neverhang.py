"""
NEVERHANG Circuit Breaker for zsh-tool.

Prevents runaway commands by implementing a circuit breaker pattern.
"""

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from .config import (
    NEVERHANG_FAILURE_THRESHOLD,
    NEVERHANG_RECOVERY_TIMEOUT,
    NEVERHANG_SAMPLE_WINDOW,
)


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking execution
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """Circuit breaker for command patterns that tend to hang."""

    state: CircuitState = CircuitState.CLOSED
    failures: list = field(default_factory=list)  # List of (timestamp, command_hash)
    last_failure: Optional[float] = None
    opened_at: Optional[float] = None

    def record_timeout(self, command_hash: str):
        """Record a timeout failure."""
        now = time.time()
        self.failures.append((now, command_hash))
        self.last_failure = now

        # Clean old failures outside sample window
        cutoff = now - NEVERHANG_SAMPLE_WINDOW
        self.failures = [(t, h) for t, h in self.failures if t > cutoff]

        # Check if we should open the circuit
        if len(self.failures) >= NEVERHANG_FAILURE_THRESHOLD:
            self.state = CircuitState.OPEN
            self.opened_at = now

    def record_success(self):
        """Record a successful execution."""
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self.failures = []

    def should_allow(self) -> tuple[bool, Optional[str]]:
        """Check if execution should be allowed."""
        if self.state == CircuitState.CLOSED:
            return True, None

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.opened_at and time.time() - self.opened_at > NEVERHANG_RECOVERY_TIMEOUT:
                self.state = CircuitState.HALF_OPEN
                return True, "NEVERHANG: Circuit half-open, testing recovery"
            return False, f"NEVERHANG: Circuit OPEN due to {len(self.failures)} recent timeouts. Retry in {int(NEVERHANG_RECOVERY_TIMEOUT - (time.time() - (self.opened_at or 0)))}s"

        # HALF_OPEN - allow but monitor
        return True, "NEVERHANG: Circuit half-open, monitoring"

    def get_status(self) -> dict:
        """Get circuit breaker status."""
        return {
            'state': self.state.value,
            'recent_failures': len(self.failures),
            'failure_threshold': NEVERHANG_FAILURE_THRESHOLD,
            'recovery_timeout': NEVERHANG_RECOVERY_TIMEOUT,
            'opened_at': self.opened_at,
            'time_until_retry': max(0, NEVERHANG_RECOVERY_TIMEOUT - (time.time() - (self.opened_at or 0))) if self.opened_at else None
        }
