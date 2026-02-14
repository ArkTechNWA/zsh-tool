use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_echo_output_on_stdout() {
    let meta = "/tmp/zsh-test-echo.json";
    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "echo hello world"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("hello world"), "stdout was: {}", stdout);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_meta_file_written_with_pipestatus() {
    let meta = "/tmp/zsh-test-meta.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "echo ok"])
        .output()
        .expect("failed to run");

    assert!(output.status.success(), "exit: {:?}", output.status);

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["pipestatus"], serde_json::json!([0]));
    assert_eq!(v["exit_code"], 0);
    assert_eq!(v["timed_out"], false);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_pipestatus_captures_pipeline() {
    let meta = "/tmp/zsh-test-pipe.json";
    let _ = fs::remove_file(meta);

    // false | true -> pipestatus [1, 0], overall exit 0
    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "false | true"])
        .output()
        .expect("failed to run");

    assert!(output.status.success());

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["pipestatus"], serde_json::json!([1, 0]));

    let _ = fs::remove_file(meta);
}

#[test]
fn test_no_metadata_in_stdout() {
    // THE critical test: pipestatus NEVER appears in command output
    let meta = "/tmp/zsh-test-clean.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "echo clean; echo output"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    // stdout must ONLY contain command output, no pipestatus metadata
    assert!(!stdout.contains("pipestatus"), "metadata leaked to stdout: {}", stdout);
    assert!(stdout.contains("clean"), "stdout: {}", stdout);
    assert!(stdout.contains("output"), "stdout: {}", stdout);

    // Pipestatus must be in meta-file, not stdout
    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    assert!(meta_content.contains("pipestatus"));

    let _ = fs::remove_file(meta);
}

#[test]
fn test_stderr_merged_into_stdout() {
    let meta = "/tmp/zsh-test-stderr.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "echo out; echo err >&2"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("out"), "stdout: {}", stdout);
    assert!(stdout.contains("err"), "stderr not merged: {}", stdout);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_nonzero_exit_code() {
    let meta = "/tmp/zsh-test-exit.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "exit 42"])
        .output()
        .expect("failed to run");

    assert_eq!(output.status.code(), Some(42));

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["exit_code"], 42);
    assert_eq!(v["pipestatus"], serde_json::json!([42]));

    let _ = fs::remove_file(meta);
}

#[test]
fn test_no_trailing_newline_output_preserved() {
    // Regression test for Issue #41
    let meta = "/tmp/zsh-test-nonl.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "printf 'no newline here'"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("no newline here"), "stdout: {:?}", stdout);

    let _ = fs::remove_file(meta);
}
