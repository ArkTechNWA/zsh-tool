use std::fs;
use std::process::Command;
use std::time::Instant;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_timeout_kills_long_command() {
    let meta = "/tmp/zsh-test-timeout.json";
    let _ = fs::remove_file(meta);

    let start = Instant::now();
    let _output = Command::new(exec_path())
        .args(["--meta", meta, "--timeout", "2", "--", "sleep 60"])
        .output()
        .expect("failed to run");

    let elapsed = start.elapsed().as_secs();
    // Should finish in ~2s, not 60s
    assert!(elapsed < 10, "took {}s, should have timed out at 2s", elapsed);

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["timed_out"], true);

    let _ = fs::remove_file(meta);
}

#[test]
fn test_fast_command_no_timeout() {
    let meta = "/tmp/zsh-test-fast.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--timeout", "30", "--", "echo fast"])
        .output()
        .expect("failed to run");

    assert!(output.status.success());

    let meta_content = fs::read_to_string(meta).expect("meta file missing");
    let v: serde_json::Value = serde_json::from_str(&meta_content).expect("invalid json");
    assert_eq!(v["timed_out"], false);

    let _ = fs::remove_file(meta);
}
