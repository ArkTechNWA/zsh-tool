use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_pipeline_segments_recorded() {
    let db = "/tmp/zsh-test-alan-pipe-seg.db";
    let meta = "/tmp/zsh-test-alan-pipe-seg-meta.json";
    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);

    let _ = Command::new(exec_path())
        .args(["--meta", meta, "--db", db, "--session-id", "pipetest", "--", "false | true"])
        .output()
        .expect("run");

    let conn = rusqlite::Connection::open(db).unwrap();

    // Should have 3 observations: full command + 2 segments
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM observations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 3, "1 full + 2 segments");

    let recent: i64 = conn
        .query_row("SELECT COUNT(*) FROM recent_commands", [], |r| r.get(0))
        .unwrap();
    assert_eq!(recent, 3);

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_single_command_no_segments() {
    let db = "/tmp/zsh-test-alan-pipe-single.db";
    let meta = "/tmp/zsh-test-alan-pipe-single-meta.json";
    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);

    let _ = Command::new(exec_path())
        .args(["--meta", meta, "--db", db, "--session-id", "singletest", "--", "echo hello"])
        .output()
        .expect("run");

    let conn = rusqlite::Connection::open(db).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM observations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1, "single command = 1 observation, no segments");

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_quoted_pipe_not_split() {
    let db = "/tmp/zsh-test-alan-pipe-quoted.db";
    let meta = "/tmp/zsh-test-alan-pipe-quoted-meta.json";
    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);

    let _ = Command::new(exec_path())
        .args([
            "--meta", meta, "--db", db, "--session-id", "quotetest",
            "--", "echo \"a|b\"",
        ])
        .output()
        .expect("run");

    let conn = rusqlite::Connection::open(db).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM observations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1, "quoted pipe should not split");

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}
