use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_same_command_same_hash() {
    let db = "/tmp/zsh-test-alan-hash-same.db";
    let meta = "/tmp/zsh-test-alan-hash-same-meta.json";
    let _ = fs::remove_file(db);

    for _ in 0..2 {
        let _ = fs::remove_file(meta);
        let _ = Command::new(exec_path())
            .args(["--meta", meta, "--db", db, "--session-id", "hashtest", "--", "echo hello"])
            .output()
            .expect("run");
    }

    let conn = rusqlite::Connection::open(db).unwrap();
    let hashes: Vec<String> = conn
        .prepare("SELECT DISTINCT command_hash FROM observations")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();

    assert_eq!(hashes.len(), 1, "same command should produce same hash");

    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM observations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 2);

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_template_stored() {
    let db = "/tmp/zsh-test-alan-hash-tpl.db";
    let meta = "/tmp/zsh-test-alan-hash-tpl-meta.json";
    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);

    let _ = Command::new(exec_path())
        .args(["--meta", meta, "--db", db, "--session-id", "tpltest", "--", "git push origin main"])
        .output()
        .expect("run");

    let conn = rusqlite::Connection::open(db).unwrap();
    let tpl: String = conn
        .query_row(
            "SELECT command_template FROM observations LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();

    assert!(tpl.contains("git"), "template should contain base command: {}", tpl);
    assert!(tpl.contains("push"), "template should contain subcommand: {}", tpl);
    assert!(tpl.contains("*"), "template should wildcard args: {}", tpl);

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_numbers_normalized_in_hash() {
    let db = "/tmp/zsh-test-alan-hash-num.db";
    let meta = "/tmp/zsh-test-alan-hash-num-meta.json";
    let _ = fs::remove_file(db);

    for cmd in &["echo 123", "echo 456"] {
        let _ = fs::remove_file(meta);
        let _ = Command::new(exec_path())
            .args(["--meta", meta, "--db", db, "--session-id", "numtest", "--", cmd])
            .output()
            .expect("run");
    }

    let conn = rusqlite::Connection::open(db).unwrap();
    let hashes: Vec<String> = conn
        .prepare("SELECT DISTINCT command_hash FROM observations")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();

    assert_eq!(hashes.len(), 1, "numbers should normalize to same hash");

    let _ = fs::remove_file(db);
    let _ = fs::remove_file(meta);
}
