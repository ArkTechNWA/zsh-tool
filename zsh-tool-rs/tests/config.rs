use std::fs;

// Import the binary crate's config module
// Since config is a module in the binary, we test via the public API
// For now, use a separate test binary that links to the library

#[test]
fn test_default_config_values() {
    let cfg = zsh_tool_exec::config::Config::default();
    assert_eq!(cfg.neverhang_timeout_default, 3600);
    assert_eq!(cfg.neverhang_timeout_max, 600);
    assert_eq!(cfg.yield_after_default, 2.0);
    assert_eq!(cfg.alan_decay_half_life_hours, 24);
    assert_eq!(cfg.alan_prune_threshold, 0.01);
    assert_eq!(cfg.alan_max_entries, 10000);
    assert_eq!(cfg.alan_recent_window_minutes, 10);
    assert_eq!(cfg.alan_streak_threshold, 3);
    assert!(cfg.alan_manopt_enabled);
    assert_eq!(cfg.alan_manopt_timeout, 2.0);
    assert_eq!(cfg.alan_manopt_fail_trigger, 2);
    assert_eq!(cfg.alan_manopt_fail_present, 3);
    assert_eq!(cfg.truncate_output_at, 30000);
}

#[test]
fn test_config_from_yaml_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    fs::write(&path, "yield_after: 5.0\n").unwrap();

    let cfg = zsh_tool_exec::config::Config::load_from(&path);
    assert_eq!(cfg.yield_after_default, 5.0);
    // YAML-unset values use default (don't check env-overridable ones due to parallel test races)
    assert_eq!(cfg.alan_decay_half_life_hours, 24);
}

#[test]
fn test_config_env_overrides() {
    // TODO(post-phase3): env var tests race with parallel tests that call load_from/from_env.
    // Fix: add a dedicated ZSH_TOOL_TEST_TIMEOUT env var (or use serial test harness).
    // For now, test_config_from_yaml_file avoids checking env-overridable fields.
    std::env::set_var("NEVERHANG_TIMEOUT_DEFAULT", "60");
    let cfg = zsh_tool_exec::config::Config::from_env();
    assert_eq!(cfg.neverhang_timeout_default, 60);
    std::env::remove_var("NEVERHANG_TIMEOUT_DEFAULT");
}

#[test]
fn test_config_missing_yaml_uses_defaults() {
    let path = std::path::Path::new("/tmp/nonexistent-zsh-tool-config.yaml");
    let cfg = zsh_tool_exec::config::Config::load_from(path);
    assert_eq!(cfg.yield_after_default, 2.0);
}

#[test]
fn test_pipestatus_marker_matches_python() {
    let cfg = zsh_tool_exec::config::Config::default();
    assert_eq!(cfg.pipestatus_marker, "___ZSH_PIPESTATUS_MARKER_f9a8b7c6___");
}
