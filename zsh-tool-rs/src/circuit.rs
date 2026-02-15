//! NEVERHANG Circuit Breaker.
//!
//! Prevents runaway commands by implementing a circuit breaker pattern.
//! State machine: Closed -> Open -> HalfOpen -> Closed

use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CircuitState {
    Closed,
    Open,
    HalfOpen,
}

impl std::fmt::Display for CircuitState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CircuitState::Closed => write!(f, "closed"),
            CircuitState::Open => write!(f, "open"),
            CircuitState::HalfOpen => write!(f, "half_open"),
        }
    }
}

pub struct CircuitBreaker {
    pub state: CircuitState,
    pub failures: Vec<(f64, String)>, // (timestamp, command_hash)
    pub last_failure: Option<f64>,
    pub opened_at: Option<f64>,
    pub failure_threshold: usize,
    pub recovery_timeout: u64,
    pub sample_window: u64,
}

impl CircuitBreaker {
    pub fn new(failure_threshold: usize, recovery_timeout: u64, sample_window: u64) -> Self {
        Self {
            state: CircuitState::Closed,
            failures: Vec::new(),
            last_failure: None,
            opened_at: None,
            failure_threshold,
            recovery_timeout,
            sample_window,
        }
    }

    fn now() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64()
    }

    /// Record a timeout failure.
    pub fn record_timeout(&mut self, command_hash: &str) {
        let now = Self::now();
        self.failures.push((now, command_hash.to_string()));
        self.last_failure = Some(now);

        // Clean old failures outside sample window
        let cutoff = now - self.sample_window as f64;
        self.failures.retain(|(t, _)| *t > cutoff);

        // Check if we should open the circuit
        if self.failures.len() >= self.failure_threshold {
            self.state = CircuitState::Open;
            self.opened_at = Some(now);
        }
    }

    /// Record a successful execution.
    pub fn record_success(&mut self) {
        if self.state == CircuitState::HalfOpen {
            self.state = CircuitState::Closed;
            self.failures.clear();
        }
    }

    /// Check if execution should be allowed.
    /// Returns (allowed, optional_message).
    pub fn should_allow(&mut self) -> (bool, Option<String>) {
        match self.state {
            CircuitState::Closed => (true, None),
            CircuitState::Open => {
                if let Some(opened_at) = self.opened_at {
                    let elapsed = Self::now() - opened_at;
                    if elapsed > self.recovery_timeout as f64 {
                        self.state = CircuitState::HalfOpen;
                        return (
                            true,
                            Some("NEVERHANG: Circuit half-open, testing recovery".into()),
                        );
                    }
                    let remaining = self.recovery_timeout as f64 - elapsed;
                    (
                        false,
                        Some(format!(
                            "NEVERHANG: Circuit OPEN due to {} recent timeouts. Retry in {}s",
                            self.failures.len(),
                            remaining as i64
                        )),
                    )
                } else {
                    (false, Some("NEVERHANG: Circuit OPEN".into()))
                }
            }
            CircuitState::HalfOpen => (
                true,
                Some("NEVERHANG: Circuit half-open, monitoring".into()),
            ),
        }
    }

    /// Reset the circuit breaker to closed state.
    pub fn reset(&mut self) {
        self.state = CircuitState::Closed;
        self.failures.clear();
        self.last_failure = None;
        self.opened_at = None;
    }

    /// Get circuit breaker status for reporting.
    pub fn get_status(&self) -> CircuitStatus {
        let time_until_retry = self.opened_at.map(|opened_at| {
            let elapsed = Self::now() - opened_at;
            (self.recovery_timeout as f64 - elapsed).max(0.0) as u64
        });

        CircuitStatus {
            state: self.state.to_string(),
            recent_failures: self.failures.len(),
            failure_threshold: self.failure_threshold,
            recovery_timeout: self.recovery_timeout,
            opened_at: self.opened_at,
            time_until_retry,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct CircuitStatus {
    pub state: String,
    pub recent_failures: usize,
    pub failure_threshold: usize,
    pub recovery_timeout: u64,
    pub opened_at: Option<f64>,
    pub time_until_retry: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_initial_state_closed() {
        let cb = CircuitBreaker::new(3, 300, 3600);
        assert_eq!(cb.state, CircuitState::Closed);
    }

    #[test]
    fn test_allows_when_closed() {
        let mut cb = CircuitBreaker::new(3, 300, 3600);
        let (allowed, msg) = cb.should_allow();
        assert!(allowed);
        assert!(msg.is_none());
    }

    #[test]
    fn test_opens_after_threshold() {
        let mut cb = CircuitBreaker::new(3, 300, 3600);
        cb.record_timeout("hash1");
        cb.record_timeout("hash2");
        assert_eq!(cb.state, CircuitState::Closed);
        cb.record_timeout("hash3");
        assert_eq!(cb.state, CircuitState::Open);
    }

    #[test]
    fn test_blocks_when_open() {
        let mut cb = CircuitBreaker::new(3, 300, 3600);
        for i in 0..3 {
            cb.record_timeout(&format!("hash{}", i));
        }
        let (allowed, msg) = cb.should_allow();
        assert!(!allowed);
        assert!(msg.unwrap().contains("NEVERHANG"));
    }

    #[test]
    fn test_success_closes_half_open() {
        let mut cb = CircuitBreaker::new(3, 300, 3600);
        cb.state = CircuitState::HalfOpen;
        cb.record_success();
        assert_eq!(cb.state, CircuitState::Closed);
        assert!(cb.failures.is_empty());
    }

    #[test]
    fn test_reset() {
        let mut cb = CircuitBreaker::new(3, 300, 3600);
        for i in 0..3 {
            cb.record_timeout(&format!("hash{}", i));
        }
        assert_eq!(cb.state, CircuitState::Open);
        cb.reset();
        assert_eq!(cb.state, CircuitState::Closed);
        assert!(cb.failures.is_empty());
    }

    #[test]
    fn test_status_serializable() {
        let cb = CircuitBreaker::new(3, 300, 3600);
        let status = cb.get_status();
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("\"state\":\"closed\""));
    }
}
