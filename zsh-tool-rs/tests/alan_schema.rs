use std::fs;
use std::process::Command;

fn exec_path() -> String {
    env!("CARGO_BIN_EXE_zsh-tool-exec").to_string()
}

#[test]
fn test_alan_db_created_with_all_tables() {
    let db_path = "/tmp/zsh-test-alan-schema.db";
    let meta = "/tmp/zsh-test-alan-schema-meta.json";
    let _ = fs::remove_file(db_path);
    let _ = fs::remove_file(meta);

    let _output = Command::new(exec_path())
        .args([
            "--meta", meta,
            "--db", db_path,
            "--session-id", "test1234",
            "--", "echo hello",
        ])
        .output()
        .expect("failed to run");

    // Verify DB exists
    assert!(
        std::path::Path::new(db_path).exists(),
        "alan.db should exist"
    );

    // Verify all 6 tables exist
    let conn = rusqlite::Connection::open(db_path).unwrap();
    let tables: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();

    assert!(tables.contains(&"observations".to_string()), "tables: {:?}", tables);
    assert!(tables.contains(&"recent_commands".to_string()), "tables: {:?}", tables);
    assert!(tables.contains(&"streaks".to_string()), "tables: {:?}", tables);
    assert!(tables.contains(&"meta".to_string()), "tables: {:?}", tables);
    assert!(tables.contains(&"ssh_observations".to_string()), "tables: {:?}", tables);
    assert!(tables.contains(&"manopt_cache".to_string()), "tables: {:?}", tables);

    let _ = fs::remove_file(db_path);
    let _ = fs::remove_file(meta);
}

#[test]
fn test_without_db_flag_still_works() {
    // Backward compatibility: no --db flag should work fine (no ALAN recording)
    let meta = "/tmp/zsh-test-alan-nodb-meta.json";
    let _ = fs::remove_file(meta);

    let output = Command::new(exec_path())
        .args(["--meta", meta, "--", "echo noalan"])
        .output()
        .expect("failed to run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("noalan"), "stdout: {:?}", stdout);
    assert_eq!(output.status.code(), Some(0));

    let _ = fs::remove_file(meta);
}
