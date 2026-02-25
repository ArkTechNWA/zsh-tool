//! Integration tests for the MCP server — full JSON-RPC round-trip.
//!
//! These tests spawn the binary in `serve` mode as a subprocess, send
//! JSON-RPC requests via stdin, and read responses from stdout.

use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::Value;

/// Build a JSON-RPC request with Content-Length framing.
fn frame_message(body: &str) -> String {
    format!("Content-Length: {}\r\n\r\n{}", body.len(), body)
}

/// Send a JSON-RPC request to the server's stdin.
fn send_request(stdin: &mut impl Write, method: &str, id: u64, params: Option<Value>) {
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params.unwrap_or(Value::Null),
    });
    let body_str = serde_json::to_string(&body).unwrap();
    let framed = frame_message(&body_str);
    stdin.write_all(framed.as_bytes()).unwrap();
    stdin.flush().unwrap();
}

/// Send a notification (no id) to the server's stdin.
fn send_notification(stdin: &mut impl Write, method: &str) {
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "method": method,
    });
    let body_str = serde_json::to_string(&body).unwrap();
    let framed = frame_message(&body_str);
    stdin.write_all(framed.as_bytes()).unwrap();
    stdin.flush().unwrap();
}

/// Read a Content-Length framed JSON-RPC response from the server's stdout.
fn read_response(reader: &mut BufReader<impl Read>) -> Value {
    // Read headers
    let mut content_length: Option<usize> = None;
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let trimmed = line.trim();
        if trimmed.is_empty() {
            break;
        }
        if let Some(len_str) = trimmed.strip_prefix("Content-Length:") {
            content_length = len_str.trim().parse().ok();
        }
    }

    let length = content_length.expect("No Content-Length header");
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body).unwrap();
    serde_json::from_slice(&body).unwrap()
}

/// Spawn the server subprocess and return (stdin, stdout_reader, child).
fn spawn_server() -> (
    std::process::ChildStdin,
    BufReader<std::process::ChildStdout>,
    std::process::Child,
) {
    // Build in case it hasn't been compiled
    let binary = env!("CARGO_BIN_EXE_zsh-tool-exec");

    let mut child = Command::new(binary)
        .arg("serve")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("Failed to spawn server");

    let stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let reader = BufReader::new(stdout);
    (stdin, reader, child)
}

#[test]
fn test_initialize() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(
        &mut stdin,
        "initialize",
        1,
        Some(serde_json::json!({
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": { "name": "test", "version": "0.1" }
        })),
    );

    let resp = read_response(&mut reader);
    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);

    let result = &resp["result"];
    assert_eq!(result["protocolVersion"], "2024-11-05");
    assert_eq!(result["serverInfo"]["name"], "zsh-tool");
    assert!(result["capabilities"]["tools"].is_object());

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_tools_list() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    // Initialize first
    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);

    // Send initialized notification
    send_notification(&mut stdin, "notifications/initialized");

    // List tools
    send_request(&mut stdin, "tools/list", 2, None);
    let resp = read_response(&mut reader);

    let tools = resp["result"]["tools"].as_array().expect("tools array");
    assert_eq!(tools.len(), 10, "Expected 10 tools");

    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    assert!(names.contains(&"zsh"));
    assert!(names.contains(&"zsh_poll"));
    assert!(names.contains(&"zsh_send"));
    assert!(names.contains(&"zsh_kill"));
    assert!(names.contains(&"zsh_tasks"));
    assert!(names.contains(&"zsh_health"));
    assert!(names.contains(&"zsh_alan_stats"));
    assert!(names.contains(&"zsh_alan_query"));
    assert!(names.contains(&"zsh_neverhang_status"));
    assert!(names.contains(&"zsh_neverhang_reset"));

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_zsh_echo() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    // Initialize
    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    // Execute echo
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "echo hello-from-rust-mcp",
                "timeout": 10,
                "yield_after": 5.0
            }
        })),
    );

    let resp = read_response(&mut reader);
    let result = &resp["result"];
    let text = result["content"][0]["text"].as_str().expect("text content");

    assert!(text.contains("hello-from-rust-mcp"), "Output should contain echo text, got: {}", text);
    assert!(text.contains("COMPLETED"), "Should show COMPLETED status, got: {}", text);

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_zsh_exit_code() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "exit 42",
                "timeout": 10,
                "yield_after": 5.0
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("FAILED"), "Non-zero exit should show FAILED, got: {}", text);
    assert!(text.contains("exit="), "Should show exit code");

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_ping() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "ping", 1, None);
    let resp = read_response(&mut reader);

    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);
    assert!(resp["result"].is_object());

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_unknown_method() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "nonexistent/method", 1, None);
    let resp = read_response(&mut reader);

    assert!(resp["error"].is_object(), "Should return error");
    assert_eq!(resp["error"]["code"], -32601);

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_unknown_tool() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "nonexistent_tool",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let result = &resp["result"];
    assert_eq!(result["isError"], true, "Should be an error response");

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_health() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh_health",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let health: Value = serde_json::from_str(text).expect("health should be JSON");
    assert_eq!(health["status"], "healthy");
    assert!(health["neverhang"].is_object());

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_neverhang_status_and_reset() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);

    // Check status
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh_neverhang_status",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let status: Value = serde_json::from_str(text).unwrap();
    assert_eq!(status["state"], "closed");

    // Reset
    send_request(
        &mut stdin,
        "tools/call",
        3,
        Some(serde_json::json!({
            "name": "zsh_neverhang_reset",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let result: Value = serde_json::from_str(text).unwrap();
    assert_eq!(result["success"], true);

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_tasks_list_empty() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh_tasks",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let result: Value = serde_json::from_str(text).unwrap();
    assert!(result["tasks"].as_array().unwrap().is_empty());

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_yield_poll_complete() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    // Start a command with very short yield_after so it yields while running
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "sleep 2 && echo done-after-sleep",
                "timeout": 30,
                "yield_after": 0.5
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();

    // Should be RUNNING since yield_after < command duration
    assert!(text.contains("RUNNING"), "Should yield as RUNNING, got: {}", text);
    assert!(text.contains("task_id="), "Should include task_id");

    // Extract task_id from the text
    let task_id_start = text.find("task_id=").unwrap() + 8;
    let task_id_end = text[task_id_start..].find(' ').unwrap() + task_id_start;
    let task_id = &text[task_id_start..task_id_end];

    // Wait for command to finish
    std::thread::sleep(Duration::from_secs(3));

    // Poll — should now be completed
    send_request(
        &mut stdin,
        "tools/call",
        3,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("COMPLETED"), "Should be COMPLETED after sleep, got: {}", text);
    assert!(text.contains("done-after-sleep"), "Should contain command output, got: {}", text);

    drop(stdin);
    let _ = child.wait();
}

/// Helper: extract task_id from a RUNNING status line.
fn extract_task_id(text: &str) -> String {
    let start = text.find("task_id=").expect("no task_id in text") + 8;
    let end = text[start..].find(' ').expect("no space after task_id") + start;
    text[start..end].to_string()
}

#[test]
fn test_background_completion_notifies_on_next_tool_call() {
    // When a background task completes while the caller isn't watching,
    // the NEXT tool call (any tool) should include a [notify] line.
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    // Command runs ~0.4s; yield_after=0.1s → yields RUNNING, then finishes in background.
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "sleep 0.4 && echo notify-test",
                "timeout": 10,
                "yield_after": 0.1
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("RUNNING"), "should yield as RUNNING, got: {}", text);

    // Wait well past command completion.
    std::thread::sleep(Duration::from_millis(800));

    // Call a completely unrelated tool. The response should contain a [notify] line
    // reporting that the background task finished.
    send_request(
        &mut stdin,
        "tools/call",
        3,
        Some(serde_json::json!({
            "name": "zsh_health",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        text.contains("[notify]"),
        "Expected [notify] in next tool call response, got:\n{}", text
    );
    assert!(
        text.contains("completed") || text.contains("failed"),
        "Expected completed/failed status in notify, got:\n{}", text
    );

    // The notification should be fire-once: a second unrelated tool call should NOT
    // include the same notification again.
    send_request(
        &mut stdin,
        "tools/call",
        4,
        Some(serde_json::json!({
            "name": "zsh_health",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        !text.contains("[notify]"),
        "Notification should fire only once, but appeared again:\n{}", text
    );

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_direct_poll_does_not_generate_notification() {
    // When the caller directly polls a task to completion via zsh_poll,
    // no [notify] should appear on the next tool call (suppress_notification=true).
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "sleep 0.4 && echo direct-poll-test",
                "timeout": 10,
                "yield_after": 0.1
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("RUNNING"), "should yield RUNNING, got: {}", text);
    let task_id = extract_task_id(text);

    std::thread::sleep(Duration::from_millis(800));

    // Directly poll the task to completion
    send_request(
        &mut stdin,
        "tools/call",
        3,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("COMPLETED"), "poll should return COMPLETED, got: {}", text);
    assert!(!text.contains("[notify]"), "poll result should not contain [notify], got: {}", text);

    // Next unrelated call should also have no notify (task was directly polled)
    send_request(
        &mut stdin,
        "tools/call",
        4,
        Some(serde_json::json!({
            "name": "zsh_health",
            "arguments": {}
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        !text.contains("[notify]"),
        "No notify expected after direct poll, got:\n{}", text
    );

    drop(stdin);
    let _ = child.wait();
}

#[test]
fn test_poll_running_task_shows_output_delta() {
    // zsh_poll on a running task should report how many new bytes arrived
    // since the last poll in the RUNNING status line.
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "for i in 1 2 3 4 5; do echo \"output line $i\"; sleep 0.2; done",
                "timeout": 30,
                "yield_after": 0.1
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("RUNNING"), "should yield RUNNING, got: {}", text);
    let task_id = extract_task_id(text);

    // Let output accumulate
    std::thread::sleep(Duration::from_millis(700));

    // First poll — should show new bytes or output
    send_request(
        &mut stdin,
        "tools/call",
        3,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();

    if text.contains("RUNNING") {
        let has_delta = text.contains(" new") || text.contains("output line");
        assert!(has_delta, "Running poll should show output delta or content, got:\n{}", text);
    } else {
        assert!(text.contains("COMPLETED"), "unexpected status, got:\n{}", text);
        assert!(text.contains("output line"), "output should be present, got:\n{}", text);
    }

    // Immediate re-poll: if still running, no new output means no delta marker
    send_request(
        &mut stdin,
        "tools/call",
        4,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    if text.contains("RUNNING") {
        // delta_str only present when new_bytes > 0
        assert!(
            !text.contains("KB new") && !text.contains("B new"),
            "immediate re-poll should show no delta, got:\n{}", text
        );
    }

    drop(stdin);
    let _ = child.wait();
}
