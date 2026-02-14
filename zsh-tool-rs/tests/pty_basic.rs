use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_pty_echo_output() {
    let meta = "/tmp/zsh-test-pty-echo.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--pty", "--", "echo PTY_TEST"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("PTY_TEST"), "stdout: {:?}", stdout);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_pty_pipestatus_in_meta_not_stdout() {
    let meta = "/tmp/zsh-test-pty-meta.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--pty", "--", "echo clean"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    // No metadata in stdout
    assert!(!stdout.contains("pipestatus"), "metadata leaked: {}", stdout);

    // Meta-file has pipestatus
    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert!(v["pipestatus"].is_array());

    let _ = fs::remove_file(meta);
}

#[test]
fn test_pty_captures_pipeline() {
    let meta = "/tmp/zsh-test-pty-pipe.json";
    let _ = fs::remove_file(meta);

    let _output = Command::new(exec_path())
        .args(["--meta", meta, "--pty", "--", "false | true"])
        .output()
        .expect("failed to run");

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["pipestatus"], serde_json::json!([1, 0]));

    let _ = fs::remove_file(meta);
}

#[test]
fn test_pty_exit_code() {
    let meta = "/tmp/zsh-test-pty-exit.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--pty", "--", "exit 7"])
        .output()
        .expect("failed to run");

    assert_eq!(output.status.code(), Some(7));

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["exit_code"], 7);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_pty_timeout() {
    let meta = "/tmp/zsh-test-pty-to.json";
    let _ = fs::remove_file(meta);

    let start = std::time::Instant::now();
    let _output = Command::new(exec_path())
        .args(["--meta", meta, "--pty", "--timeout", "2", "--", "sleep 60"])
        .output()
        .expect("failed to run");

    let elapsed = start.elapsed().as_secs();
    assert!(elapsed < 10, "took {}s", elapsed);

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["timed_out"], true);

    let _ = fs::remove_file(meta);
}
