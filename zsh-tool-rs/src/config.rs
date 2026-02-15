use std::path::Path;

pub struct Config {
    // NEVERHANG
    pub neverhang_timeout_default: u64,
    pub neverhang_timeout_max: u64,
    pub neverhang_failure_threshold: usize,
    pub neverhang_recovery_timeout: u64,
    pub neverhang_sample_window: u64,
    // Yield
    pub yield_after_default: f64,
    // ALAN
    pub alan_db_path: String,
    pub alan_decay_half_life_hours: u64,
    pub alan_prune_threshold: f64,
    pub alan_prune_interval_hours: u64,
    pub alan_max_entries: usize,
    pub alan_recent_window_minutes: u64,
    pub alan_streak_threshold: i64,
    // manopt
    pub alan_manopt_enabled: bool,
    pub alan_manopt_timeout: f64,
    pub alan_manopt_fail_trigger: i64,
    pub alan_manopt_fail_present: i64,
    // Output
    pub truncate_output_at: usize,
    // Pipestatus marker
    pub pipestatus_marker: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            neverhang_timeout_default: 3600,
            neverhang_timeout_max: 600,
            neverhang_failure_threshold: 3,
            neverhang_recovery_timeout: 300,
            neverhang_sample_window: 3600,
            yield_after_default: 2.0,
            alan_db_path: expand_tilde("~/.claude/plugins/zsh-tool/data/alan.db"),
            alan_decay_half_life_hours: 24,
            alan_prune_threshold: 0.01,
            alan_prune_interval_hours: 6,
            alan_max_entries: 10000,
            alan_recent_window_minutes: 10,
            alan_streak_threshold: 3,
            alan_manopt_enabled: true,
            alan_manopt_timeout: 2.0,
            alan_manopt_fail_trigger: 2,
            alan_manopt_fail_present: 3,
            truncate_output_at: 30000,
            pipestatus_marker: "___ZSH_PIPESTATUS_MARKER_f9a8b7c6___".to_string(),
        }
    }
}

impl Config {
    /// Load config from YAML file, then apply env overrides.
    pub fn load_from(path: &Path) -> Self {
        let mut cfg = Self::default();
        if let Ok(content) = std::fs::read_to_string(path) {
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                if let Some((key, value)) = line.split_once(':') {
                    let key = key.trim();
                    let value = value.trim();
                    match key {
                        "yield_after" => {
                            if let Ok(v) = value.parse() {
                                cfg.yield_after_default = v;
                            }
                        }
                        _ => {} // Ignore unknown keys for now
                    }
                }
            }
        }
        cfg.apply_env_overrides();
        cfg
    }

    /// Load config from env overrides only (no YAML file).
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        cfg.apply_env_overrides();
        cfg
    }

    /// Load from default config path (~/.config/zsh-tool/config.yaml) + env.
    pub fn load() -> Self {
        let config_path = expand_tilde("~/.config/zsh-tool/config.yaml");
        let path = Path::new(&config_path);
        if path.exists() {
            Self::load_from(path)
        } else {
            Self::from_env()
        }
    }

    fn apply_env_overrides(&mut self) {
        if let Ok(v) = std::env::var("NEVERHANG_TIMEOUT_DEFAULT") {
            if let Ok(n) = v.parse() {
                self.neverhang_timeout_default = n;
            }
        }
        if let Ok(v) = std::env::var("NEVERHANG_TIMEOUT_MAX") {
            if let Ok(n) = v.parse() {
                self.neverhang_timeout_max = n;
            }
        }
        if let Ok(v) = std::env::var("ALAN_DB_PATH") {
            self.alan_db_path = expand_tilde(&v);
        }
        if let Ok(v) = std::env::var("ALAN_MANOPT_ENABLED") {
            self.alan_manopt_enabled =
                !["0", "false", "no", "off"].contains(&v.to_lowercase().as_str());
        }
        if let Ok(v) = std::env::var("ALAN_MANOPT_TIMEOUT") {
            if let Ok(n) = v.parse() {
                self.alan_manopt_timeout = n;
            }
        }
    }
}

/// Expand ~ to home directory. Simple replacement, no shellexpand dep needed.
fn expand_tilde(path: &str) -> String {
    if path.starts_with("~/") || path == "~" {
        if let Ok(home) = std::env::var("HOME") {
            return path.replacen('~', &home, 1);
        }
    }
    path.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_expand_tilde() {
        let expanded = expand_tilde("~/foo/bar");
        assert!(!expanded.starts_with('~'));
        assert!(expanded.ends_with("/foo/bar"));
    }

    #[test]
    fn test_expand_tilde_no_tilde() {
        assert_eq!(expand_tilde("/absolute/path"), "/absolute/path");
    }
}
