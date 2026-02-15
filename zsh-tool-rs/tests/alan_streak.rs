use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_streak_created_on_first_run() {
    let db = "/tmp/zsh-test-alan-streak-first.db";
    let meta = "/tmp/zsh-test-alan-streak-first-meta.json";
    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);

    let _ = Command::new(exec_path())
        .args(["--meta", meta, "--db", db, "--session-id", "str1", "--", "echo streaktest"])
        .output()
        .expect("run");

    let conn = rusqlite::Connection::open(db).unwrap();
    let streak: i64 = conn
        .query_row(
            "SELECT current_streak FROM streaks LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(streak, 1, "first success = streak 1");

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_streak_increments_on_repeated_success() {
    let db = "/tmp/zsh-test-alan-streak-inc.db";
    let meta = "/tmp/zsh-test-alan-streak-inc-meta.json";
    let _ = fs::remove_file(db);

    for _ in 0..3 {
        let _ = fs::remove_file(meta);
        let _ = Command::new(exec_path())
            .args(["--meta", meta, "--db", db, "--session-id", "str2", "--", "echo repeat"])
            .output()
            .expect("run");
    }

    let conn = rusqlite::Connection::open(db).unwrap();
    let streak: i64 = conn
        .query_row(
            "SELECT current_streak FROM streaks LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(streak, 3, "3 successes = streak 3");

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_streak_resets_on_failure() {
    let db = "/tmp/zsh-test-alan-streak-reset.db";
    let meta = "/tmp/zsh-test-alan-streak-reset-meta.json";
    let _ = fs::remove_file(db);

    // 2 successes then 1 failure
    for cmd in &["echo ok", "echo ok", "false"] {
        let _ = fs::remove_file(meta);
        let _ = Command::new(exec_path())
            .args(["--meta", meta, "--db", db, "--session-id", "str3", "--", cmd])
            .output()
            .expect("run");
    }

    let conn = rusqlite::Connection::open(db).unwrap();

    let rows: Vec<(String, i64, i64)> = conn
        .prepare("SELECT command_hash, current_streak, longest_success_streak FROM streaks")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();

    // Find the failure streak
    let fail_streak = rows.iter().find(|r| r.1 < 0);
    assert!(fail_streak.is_some(), "should have negative streak for 'false'");
    assert_eq!(fail_streak.unwrap().1, -1);

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}
