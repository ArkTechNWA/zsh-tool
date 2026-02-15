use zsh_tool_exec::alan;

fn fresh_db() -> (rusqlite::Connection, String) {
    let path = format!("/tmp/zsh-test-ssh-{}.db", uuid::Uuid::new_v4());
    let conn = rusqlite::Connection::open(&path).unwrap();
    alan::init_schema(&conn).unwrap();
    (conn, path)
}

#[test]
fn test_ssh_recording() {
    let (conn, path) = fresh_db();

    // Record an SSH command via the main record path
    alan::record(&conn, "s1", "ssh myhost ls -la", 0, 500, false, "", &[0]).unwrap();

    // Verify SSH observation was created
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM ssh_observations WHERE host = 'myhost'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_ssh_not_recorded_for_non_ssh() {
    let (conn, path) = fresh_db();

    alan::record(&conn, "s1", "ls -la /tmp", 0, 100, false, "", &[0]).unwrap();

    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM ssh_observations", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_ssh_connection_failure_insight() {
    let (conn, path) = fresh_db();

    // Record several connection failures
    for _ in 0..4 {
        alan::record(&conn, "s1", "ssh badhost", 255, 1000, false, "", &[255]).unwrap();
    }

    // Get insights for next SSH to badhost
    let insights = alan::insights::get_pre_insights(&conn, "ssh badhost", "s1", 3, 10);
    assert!(
        insights
            .iter()
            .any(|(level, msg)| level == "warning" && msg.contains("connection failure rate")),
        "Expected connection failure insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_ssh_reliable_host_insight() {
    let (conn, path) = fresh_db();

    for _ in 0..4 {
        alan::record(
            &conn,
            "s1",
            "ssh goodhost echo hello",
            0,
            200,
            false,
            "",
            &[0],
        )
        .unwrap();
    }

    let insights = alan::insights::get_pre_insights(&conn, "ssh goodhost uptime", "s1", 3, 10);
    assert!(
        insights
            .iter()
            .any(|(_, msg)| msg.contains("reliable")),
        "Expected reliable host insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_ssh_exit_classification() {
    assert_eq!(alan::ssh::classify_ssh_exit(0), "success");
    assert_eq!(alan::ssh::classify_ssh_exit(1), "command_failed");
    assert_eq!(alan::ssh::classify_ssh_exit(255), "connection_failed");
}
