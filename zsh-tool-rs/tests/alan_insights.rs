use zsh_tool_exec::alan;

fn fresh_db() -> (rusqlite::Connection, String) {
    let path = format!("/tmp/zsh-test-insights-{}.db", uuid::Uuid::new_v4());
    let conn = rusqlite::Connection::open(&path).unwrap();
    alan::init_schema(&conn).unwrap();
    (conn, path)
}

fn record(conn: &rusqlite::Connection, cmd: &str, session: &str, exit_code: i32) {
    alan::record(conn, session, cmd, exit_code, 100, false, "", &[exit_code]).unwrap();
}

#[test]
fn test_new_pattern_insight() {
    let (conn, path) = fresh_db();

    let insights = alan::insights::get_pre_insights(&conn, "echo never_seen_before", "s1", 3, 10);
    assert!(
        insights.iter().any(|(_, msg)| msg.contains("New pattern")),
        "Expected 'New pattern' insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_retry_detection_insight() {
    let (conn, path) = fresh_db();

    record(&conn, "echo retry_test", "s1", 0);
    record(&conn, "echo retry_test", "s1", 0);

    let insights = alan::insights::get_pre_insights(&conn, "echo retry_test", "s1", 3, 10);
    assert!(
        insights.iter().any(|(_, msg)| msg.contains("Retry")),
        "Expected 'Retry' insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_streak_insight() {
    let (conn, path) = fresh_db();

    for _ in 0..4 {
        record(&conn, "echo streak_cmd", "s1", 0);
    }

    let insights = alan::insights::get_pre_insights(&conn, "echo streak_cmd", "s1", 3, 10);
    assert!(
        insights
            .iter()
            .any(|(_, msg)| msg.contains("Streak") || msg.contains("streak")),
        "Expected streak insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_failing_streak_warning() {
    let (conn, path) = fresh_db();

    for _ in 0..4 {
        record(&conn, "echo fail_cmd", "s1", 1);
    }

    let insights = alan::insights::get_pre_insights(&conn, "echo fail_cmd", "s1", 3, 10);
    assert!(
        insights
            .iter()
            .any(|(level, msg)| level == "warning" && msg.contains("Failing streak")),
        "Expected failing streak warning, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_reliable_pattern_insight() {
    let (conn, path) = fresh_db();

    for _ in 0..6 {
        record(&conn, "echo reliable_cmd", "s1", 0);
    }

    let insights = alan::insights::get_pre_insights(&conn, "echo reliable_cmd", "s1", 3, 10);
    assert!(
        insights.iter().any(|(_, msg)| msg.contains("Reliable")),
        "Expected 'Reliable' insight, got: {:?}",
        insights
    );

    let _ = std::fs::remove_file(path);
}

#[test]
fn test_post_insights_silent_command() {
    let insights = alan::insights::get_post_insights("echo test", &[0], "");
    assert!(
        insights
            .iter()
            .any(|(_, msg)| msg.contains("No output produced")),
        "Expected silent detection, got: {:?}",
        insights
    );
}

#[test]
fn test_post_insights_command_not_found() {
    let insights = alan::insights::get_post_insights("nonexistent_cmd", &[127], "");
    assert!(
        insights
            .iter()
            .any(|(level, msg)| level == "warning" && msg.contains("command not found")),
        "Expected 'command not found', got: {:?}",
        insights
    );
}

#[test]
fn test_post_insights_grep_no_match() {
    let insights = alan::insights::get_post_insights("grep pattern file", &[1], "");
    assert!(
        insights
            .iter()
            .any(|(_, msg)| msg.contains("no match")),
        "Expected 'no match', got: {:?}",
        insights
    );
}

#[test]
fn test_post_insights_pipe_masking() {
    let insights = alan::insights::get_post_insights("fail | succeed", &[1, 0], "output");
    assert!(
        insights
            .iter()
            .any(|(level, msg)| level == "warning" && msg.contains("masked by downstream")),
        "Expected pipe masking warning, got: {:?}",
        insights
    );
}

#[test]
fn test_post_insights_sigpipe_not_warned() {
    let insights = alan::insights::get_post_insights("head | cat", &[141, 0], "output");
    assert!(
        !insights
            .iter()
            .any(|(_, msg)| msg.contains("masked by downstream")),
        "SIGPIPE should not trigger warning, got: {:?}",
        insights
    );
}

#[test]
fn test_extract_base_command() {
    assert_eq!(alan::insights::extract_base_command("git status"), "git");
    assert_eq!(
        alan::insights::extract_base_command("/usr/bin/grep foo"),
        "grep"
    );
    assert_eq!(alan::insights::extract_base_command(""), "");
}
