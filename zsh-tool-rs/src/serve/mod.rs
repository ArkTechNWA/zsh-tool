//! MCP server: JSON-RPC 2.0 over stdio.
//!
//! Handles initialize, tools/list, tools/call, and notifications.

pub mod protocol;
pub mod tools;

use std::collections::HashMap;
use std::io;
use std::process::{Child, ChildStdin, ChildStdout};
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::alan;
use crate::circuit::CircuitBreaker;
use crate::config::Config;

use protocol::{
    error_content, initialize_result, read_message, text_content, write_message, JsonRpcResponse,
};

/// A background task that finished while the caller wasn't watching.
#[derive(Debug, Clone)]
pub struct CompletedEvent {
    pub task_id: String,
    pub exit_code: i32,
    pub elapsed: f64,
}

/// Shared server state.
pub struct ServerState {
    pub config: Config,
    pub circuit_breaker: Mutex<CircuitBreaker>,
    pub session_id: String,
    pub db_path: String,
    pub tasks: Mutex<TaskRegistry>,
    pub event_queue: Mutex<Vec<CompletedEvent>>,
}

/// Active task registry.
pub struct TaskRegistry {
    pub tasks: HashMap<String, TaskInfo>,
}

/// A live or completed task with process handles.
pub struct TaskInfo {
    pub task_id: String,
    pub command: String,
    pub started_at: std::time::Instant,
    pub started_at_epoch: f64,
    pub status: String,
    pub output_buffer: String,
    pub last_poll_offset: usize,
    pub has_stdin: bool,
    pub pipestatus: Vec<i32>,
    pub pid: Option<u32>,
    pub is_pty: bool,
    pub meta_path: String,
    pub pre_insights: Vec<(String, String)>,
    // Live process handles — None after process completes
    pub child: Option<Child>,
    pub stdout: Option<ChildStdout>,
    pub stdin: Option<ChildStdin>,
}

/// Run the MCP server on stdio.
pub fn run_server() {
    eprintln!("[zsh-tool] Starting MCP server v{}", env!("CARGO_PKG_VERSION"));
    let config = Config::load();
    eprintln!("[zsh-tool] Config loaded: db={}, timeout={}, yield_after={}",
        config.alan_db_path, config.neverhang_timeout_default, config.yield_after_default);
    let cb = CircuitBreaker::new(
        config.neverhang_failure_threshold,
        config.neverhang_recovery_timeout,
        config.neverhang_sample_window,
    );

    let state = Arc::new(ServerState {
        db_path: config.alan_db_path.clone(),
        session_id: uuid::Uuid::new_v4().to_string(),
        circuit_breaker: Mutex::new(cb),
        tasks: Mutex::new(TaskRegistry {
            tasks: HashMap::new(),
        }),
        event_queue: Mutex::new(Vec::new()),
        config,
    });

    eprintln!("[zsh-tool] Session {} — waiting for requests on stdin", state.session_id);
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let stdout = io::stdout();
    let mut writer = stdout.lock();

    while let Some(request) = read_message(&mut reader) {
        // Notifications (no id) — just acknowledge
        if request.id.is_none() {
            eprintln!("[zsh-tool] Notification: {}", request.method);
            continue;
        }

        eprintln!("[zsh-tool] Request: {} (id={:?})", request.method, request.id);
        let response = handle_request(&state, &request.method, request.id.clone(), request.params);
        write_message(&mut writer, &response);
        eprintln!("[zsh-tool] Response sent for: {}", request.method);
    }
    eprintln!("[zsh-tool] stdin closed — shutting down");
}

fn handle_request(
    state: &Arc<ServerState>,
    method: &str,
    id: Option<Value>,
    params: Option<Value>,
) -> JsonRpcResponse {
    match method {
        "initialize" => {
            let result = initialize_result("zsh-tool", env!("CARGO_PKG_VERSION"));
            JsonRpcResponse::success(id, result)
        }
        "tools/list" => {
            let result = tools::list_tools(
                state.config.neverhang_timeout_default,
                state.config.neverhang_timeout_max,
                state.config.yield_after_default,
            );
            JsonRpcResponse::success(id, result)
        }
        "tools/call" => {
            let params = params.unwrap_or(Value::Null);
            let tool_name = params
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let arguments = params
                .get("arguments")
                .cloned()
                .unwrap_or(Value::Object(serde_json::Map::new()));

            let result = handle_tool_call(state, tool_name, &arguments);
            JsonRpcResponse::success(id, result)
        }
        "ping" => JsonRpcResponse::success(id, serde_json::json!({})),
        _ => JsonRpcResponse::error(id, -32601, format!("Method not found: {}", method)),
    }
}

/// Data needed to finalize a completed task outside the tasks lock.
type FinalizeArgs = (String, String, String, f64, Vec<(String, String)>, String);

/// If `task_id` is running and its child has exited, drain stdout, mark completed,
/// and return finalization arguments. Returns None if still running or not found.
fn collect_if_done(
    state: &Arc<ServerState>,
    task_id: &str,
) -> Option<FinalizeArgs> {
    let mut tasks = state.tasks.lock().unwrap();
    let task = tasks.tasks.get_mut(task_id)?;
    if task.status != "running" {
        return None;
    }
    let done = task.child.as_mut()
        .and_then(|c| c.try_wait().ok().flatten())
        .is_some();
    if !done {
        return None;
    }
    // Drain remaining output (switch to blocking for clean EOF)
    if let Some(ref mut stdout) = task.stdout {
        use std::io::Read;
        #[cfg(unix)]
        {
            use std::os::unix::io::AsRawFd;
            let fd = stdout.as_raw_fd();
            unsafe {
                let flags = libc::fcntl(fd, libc::F_GETFL);
                libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
            }
        }
        let mut remaining = String::new();
        let _ = stdout.read_to_string(&mut remaining);
        task.output_buffer.push_str(&remaining);
    }
    task.child = None;
    task.stdout = None;
    task.stdin = None;
    task.status = "completed".to_string();
    Some((
        task.task_id.clone(),
        task.command.clone(),
        task.output_buffer.clone(),
        task.started_at.elapsed().as_secs_f64(),
        task.pre_insights.clone(),
        task.meta_path.clone(),
    ))
}

/// Proactively finalize any background tasks that completed without being polled.
/// Called at the start of every tool call so completions are never missed.
fn check_and_finalize_background_tasks(state: &Arc<ServerState>) {
    let running_ids: Vec<String> = {
        let tasks = state.tasks.lock().unwrap();
        tasks.tasks.values()
            .filter(|t| t.status == "running")
            .map(|t| t.task_id.clone())
            .collect()
    };
    for task_id in running_ids {
        if let Some((tid, cmd, output, elapsed, pre, meta)) = collect_if_done(state, &task_id) {
            // suppress_notification=false: background completion, enqueue notification
            finalize_task(state, &tid, &cmd, &output, elapsed, &pre, &meta, false);
        }
    }
}

fn handle_tool_call(state: &Arc<ServerState>, tool_name: &str, args: &Value) -> Value {
    check_and_finalize_background_tasks(state);
    let result = match tool_name {
        "zsh" => handle_zsh(state, args),
        "zsh_poll" => handle_poll(state, args),
        "zsh_send" => handle_send(state, args),
        "zsh_kill" => handle_kill(state, args),
        "zsh_tasks" => handle_list_tasks(state),
        "zsh_health" => handle_health(state),
        "zsh_alan_stats" => handle_alan_stats(state),
        "zsh_alan_query" => handle_alan_query(state, args),
        "zsh_neverhang_status" => handle_neverhang_status(state),
        "zsh_neverhang_reset" => handle_neverhang_reset(state),
        _ => return error_content(&format!("Unknown tool: {}", tool_name)),
    };
    prepend_events(state, result)
}

// ANSI color constants
const C_GREEN: &str = "\x1b[32m";
const C_RED: &str = "\x1b[31m";
const C_YELLOW: &str = "\x1b[33m";
const C_CYAN: &str = "\x1b[36m";
const C_DIM: &str = "\x1b[2m";
const C_RESET: &str = "\x1b[0m";

fn color_exit(code: i32) -> String {
    if code == 0 {
        format!("{}{}{}", C_GREEN, code, C_RESET)
    } else if code > 128 {
        format!("{}{}{}", C_YELLOW, code, C_RESET)
    } else {
        format!("{}{}{}", C_RED, code, C_RESET)
    }
}

fn format_task_output(result: &serde_json::Map<String, Value>) -> Value {
    let mut parts: Vec<String> = Vec::new();

    // Output first — clean, untouched
    let output = result
        .get("output")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if !output.trim().is_empty() {
        parts.push(output.trim_end_matches('\n').to_string());
    } else if result.get("status").and_then(|v| v.as_str()) == Some("completed")
        && output.trim().is_empty()
    {
        parts.push(format!("{}(no output){}", C_DIM, C_RESET));
    }

    // Error
    if let Some(error) = result.get("error").and_then(|v| v.as_str()) {
        parts.push(format!("{}[error]{} {}", C_RED, C_RESET, error));
    }

    // Status line
    let status = result
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let task_id = result
        .get("task_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let elapsed = result
        .get("elapsed_seconds")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);

    match status {
        "running" => {
            let has_stdin = result
                .get("has_stdin")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let new_bytes = result
                .get("new_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let delta_str = if new_bytes >= 1024 {
                format!(" — {:.1} KB new", new_bytes as f64 / 1024.0)
            } else if new_bytes > 0 {
                format!(" — {} B new", new_bytes)
            } else {
                String::new()
            };
            parts.push(format!(
                "{}[RUNNING{} task_id={} elapsed={:.1}s stdin={}{}{}]{}",
                C_CYAN,
                C_RESET,
                task_id,
                elapsed,
                if has_stdin { "yes" } else { "no" },
                delta_str,
                C_CYAN,
                C_RESET
            ));
            parts.push("Use zsh_poll to continue, zsh_send to input, zsh_kill to stop.".into());
        }
        "completed" => {
            let pipestatus: Vec<i32> = result
                .get("pipestatus")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_i64().map(|n| n as i32))
                        .collect()
                })
                .unwrap_or_else(|| vec![0]);

            let overall = *pipestatus.last().unwrap_or(&0);
            let (word, bracket_close) = if overall == 0 {
                (
                    format!("{}[COMPLETED", C_GREEN),
                    format!("{}]{}", C_GREEN, C_RESET),
                )
            } else {
                (
                    format!("{}[FAILED", C_RED),
                    format!("{}]{}", C_RED, C_RESET),
                )
            };

            let mut exit_str = format!("exit={}", color_exit(overall));
            if pipestatus.len() > 1 {
                let colored: Vec<String> = pipestatus.iter().map(|&c| color_exit(c)).collect();
                exit_str.push_str(&format!(" pipestatus=[{}]", colored.join(",")));
            }
            parts.push(format!(
                "{}{} task_id={} elapsed={:.1}s {}{}",
                word, C_RESET, task_id, elapsed, exit_str, bracket_close
            ));
        }
        "timeout" => {
            parts.push(format!(
                "{}[TIMEOUT{} task_id={} elapsed={:.1}s{}]{}",
                C_YELLOW, C_RESET, task_id, elapsed, C_YELLOW, C_RESET
            ));
        }
        "killed" => {
            parts.push(format!(
                "{}[KILLED{} task_id={} elapsed={:.1}s{}]{}",
                C_RED, C_RESET, task_id, elapsed, C_RED, C_RESET
            ));
        }
        "error" => {
            parts.push(format!(
                "{}[ERROR{} task_id={} elapsed={:.1}s{}]{}",
                C_RED, C_RESET, task_id, elapsed, C_RED, C_RESET
            ));
        }
        _ => {}
    }

    // ALAN insights
    if let Some(insights) = result.get("insights").and_then(|v| v.as_object()) {
        for (level, messages) in insights {
            if let Some(arr) = messages.as_array() {
                let joined: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                if !joined.is_empty() {
                    let msg = joined.join(" | ");
                    if level == "warning" {
                        parts.push(format!(
                            "{}[warning: A.L.A.N.: {}]{}",
                            C_YELLOW, msg, C_RESET
                        ));
                    } else {
                        parts.push(format!("{}[info: A.L.A.N.: {}]{}", C_DIM, msg, C_RESET));
                    }
                }
            }
        }
    }

    let text = if parts.is_empty() {
        "(no output)".to_string()
    } else {
        parts.join("\n")
    };
    text_content(&text)
}

// --- Tool handlers ---

/// Non-blocking read of available bytes from a ChildStdout.
/// Sets O_NONBLOCK on the fd, reads what's available, returns it.
fn read_available(stdout: &mut ChildStdout) -> String {
    use std::io::Read;
    #[cfg(unix)]
    {
        use std::os::unix::io::AsRawFd;
        let fd = stdout.as_raw_fd();
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
        }
        let mut collected = Vec::new();
        let mut buf = [0u8; 65536];
        loop {
            match stdout.read(&mut buf) {
                Ok(0) => break,   // EOF
                Ok(n) => collected.extend_from_slice(&buf[..n]),
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => break,
            }
        }
        String::from_utf8_lossy(&collected).to_string()
    }
    #[cfg(not(unix))]
    {
        String::new()
    }
}

/// Finalize a completed task: read meta, compute insights, update circuit breaker, prune.
/// `suppress_notification`: true when the caller is directly receiving this result
/// (zsh immediate completion, zsh_poll). false for tasks that finished in the background
/// and should notify on the next unrelated tool call.
#[allow(clippy::too_many_arguments)]
fn finalize_task(
    state: &Arc<ServerState>,
    task_id: &str,
    command: &str,
    output: &str,
    elapsed: f64,
    pre_insights: &[(String, String)],
    meta_path: &str,
    suppress_notification: bool,
) -> Value {
    // Read meta.json for pipestatus
    let meta = std::fs::read_to_string(meta_path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok());

    let pipestatus: Vec<i32> = meta
        .as_ref()
        .and_then(|m| m.get("pipestatus"))
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_i64().map(|n| n as i32))
                .collect()
        })
        .unwrap_or_else(|| vec![0]);

    let overall_exit = *pipestatus.last().unwrap_or(&0);

    let post_insights = alan::insights::get_post_insights(command, &pipestatus, output);
    let insights = combine_insights(pre_insights, &post_insights);

    // Circuit breaker
    {
        let mut cb = state.circuit_breaker.lock().unwrap();
        let timed_out = meta
            .as_ref()
            .and_then(|m| m.get("timed_out"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if timed_out {
            cb.record_timeout(&alan::hash::hash_command(command));
        } else {
            cb.record_success();
        }
    }

    // Maybe prune
    if let Ok(conn) = alan::open_db(&state.db_path) {
        alan::prune::maybe_prune(
            &conn,
            state.config.alan_decay_half_life_hours,
            state.config.alan_prune_threshold,
            state.config.alan_max_entries,
            state.config.alan_prune_interval_hours,
        );
    }

    let _ = std::fs::remove_file(meta_path);

    if !suppress_notification {
        enqueue_event(state, task_id, overall_exit, elapsed);
    }

    let result = serde_json::json!({
        "success": overall_exit == 0,
        "task_id": task_id,
        "status": "completed",
        "output": truncate_output(output, state.config.truncate_output_at),
        "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
        "pipestatus": pipestatus,
        "insights": insights,
    });
    format_task_output(result.as_object().unwrap())
}

fn handle_zsh(state: &Arc<ServerState>, args: &Value) -> Value {
    let command = match args.get("command").and_then(|v| v.as_str()) {
        Some(c) => c,
        None => return error_content("Missing required parameter: command"),
    };

    let use_pty = args.get("pty").and_then(|v| v.as_bool()).unwrap_or(false);
    let timeout = args
        .get("timeout")
        .and_then(|v| v.as_u64())
        .unwrap_or(state.config.neverhang_timeout_default)
        .min(state.config.neverhang_timeout_max);
    let yield_after = args
        .get("yield_after")
        .and_then(|v| v.as_f64())
        .unwrap_or(state.config.yield_after_default);

    // Circuit breaker check
    {
        let mut cb = state.circuit_breaker.lock().unwrap();
        let (allowed, msg) = cb.should_allow();
        if !allowed {
            let error_msg = msg.unwrap_or_else(|| "NEVERHANG: Circuit OPEN".into());
            let result = serde_json::json!({
                "success": false,
                "error": error_msg,
                "task_id": "",
                "status": "error",
                "output": "",
                "elapsed_seconds": 0,
            });
            return format_task_output(result.as_object().unwrap());
        }
    }

    // Get pre-insights from ALAN
    let pre_insights = if let Ok(conn) = alan::open_db(&state.db_path) {
        alan::insights::get_pre_insights(
            &conn,
            command,
            &state.session_id,
            state.config.alan_streak_threshold,
            state.config.alan_recent_window_minutes,
        )
    } else {
        Vec::new()
    };

    // Execute command via spawning self as `exec`
    let task_id = uuid::Uuid::new_v4().to_string()[..8].to_string();
    let exec_path = std::env::current_exe().unwrap_or_else(|_| "zsh-tool-exec".into());

    let meta_path = format!("/tmp/zsh-tool-meta-{}.json", task_id);

    let mut cmd_args = vec![
        "exec".to_string(),
        "--meta".to_string(),
        meta_path.clone(),
        "--timeout".to_string(),
        timeout.to_string(),
        "--db".to_string(),
        state.db_path.clone(),
        "--session-id".to_string(),
        state.session_id.clone(),
    ];
    if use_pty {
        cmd_args.push("--pty".to_string());
    }
    cmd_args.push("--".to_string());
    cmd_args.push(command.to_string());

    let start = std::time::Instant::now();

    // Spawn exec process
    let child = std::process::Command::new(&exec_path)
        .args(&cmd_args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .stdin(if use_pty {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        })
        .spawn();

    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let result = serde_json::json!({
                "success": false,
                "error": format!("Failed to spawn executor: {}", e),
                "task_id": task_id,
                "status": "error",
                "output": "",
                "elapsed_seconds": 0,
            });
            return format_task_output(result.as_object().unwrap());
        }
    };

    let pid = child.id();

    // Take handles from the child
    let mut stdout_handle = child.stdout.take();
    let stdin_handle = child.stdin.take();

    // Wait for yield_after or completion
    let yield_dur = std::time::Duration::from_secs_f64(yield_after);
    std::thread::sleep(yield_dur);

    let elapsed = start.elapsed().as_secs_f64();

    // Check if process completed
    match child.try_wait() {
        Ok(Some(_exit_status)) => {
            // Process completed — read all remaining output
            let mut output = String::new();
            if let Some(ref mut stdout) = stdout_handle {
                use std::io::Read;
                // Reset to blocking for final drain
                #[cfg(unix)]
                {
                    use std::os::unix::io::AsRawFd;
                    let fd = stdout.as_raw_fd();
                    unsafe {
                        let flags = libc::fcntl(fd, libc::F_GETFL);
                        libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
                    }
                }
                let _ = stdout.read_to_string(&mut output);
            }

            // Caller receives this result directly — no background notification needed.
            finalize_task(state, &task_id, command, &output, elapsed, &pre_insights, &meta_path, true)
        }
        Ok(None) => {
            // Still running — collect partial output and register task
            let output_so_far = if let Some(ref mut stdout) = stdout_handle {
                read_available(stdout)
            } else {
                String::new()
            };

            let now_epoch = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();

            let has_stdin = stdin_handle.is_some();

            {
                let mut tasks = state.tasks.lock().unwrap();
                tasks.tasks.insert(
                    task_id.clone(),
                    TaskInfo {
                        task_id: task_id.clone(),
                        command: command.to_string(),
                        started_at: start,
                        started_at_epoch: now_epoch,
                        status: "running".to_string(),
                        output_buffer: output_so_far.clone(),
                        last_poll_offset: 0,
                        has_stdin,
                        pipestatus: Vec::new(),
                        pid: Some(pid),
                        is_pty: use_pty,
                        meta_path: meta_path.clone(),
                        pre_insights: pre_insights.clone(),
                        child: Some(child),
                        stdout: stdout_handle,
                        stdin: stdin_handle,
                    },
                );
            }

            let insights = combine_insights(&pre_insights, &[]);

            let result = serde_json::json!({
                "task_id": task_id,
                "status": "running",
                "output": truncate_output(&output_so_far, state.config.truncate_output_at),
                "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
                "has_stdin": has_stdin,
                "insights": insights,
            });
            format_task_output(result.as_object().unwrap())
        }
        Err(e) => {
            let result = serde_json::json!({
                "success": false,
                "error": format!("Process wait error: {}", e),
                "task_id": task_id,
                "status": "error",
                "output": "",
                "elapsed_seconds": 0,
            });
            format_task_output(result.as_object().unwrap())
        }
    }
}

fn handle_poll(state: &Arc<ServerState>, args: &Value) -> Value {
    let task_id = match args.get("task_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return error_content("Missing required parameter: task_id"),
    };

    let mut tasks = state.tasks.lock().unwrap();
    let task = match tasks.tasks.get_mut(task_id) {
        Some(t) => t,
        None => {
            return error_content(&format!("Unknown task: {}", task_id));
        }
    };

    // If already finalized, return cached result
    if task.status != "running" {
        let result = serde_json::json!({
            "task_id": task.task_id,
            "status": task.status,
            "output": truncate_output(&task.output_buffer, state.config.truncate_output_at),
            "elapsed_seconds": format!("{:.1}", task.started_at.elapsed().as_secs_f64())
                .parse::<f64>().unwrap_or(0.0),
            "pipestatus": task.pipestatus,
        });
        // Caller is observing this task directly — clear any pending [notify] for it.
        drop(tasks);
        suppress_event_for_task(state, task_id);
        return format_task_output(result.as_object().unwrap());
    }

    // Read any new output
    if let Some(ref mut stdout) = task.stdout {
        let new_output = read_available(stdout);
        if !new_output.is_empty() {
            task.output_buffer.push_str(&new_output);
        }
    }

    let elapsed = task.started_at.elapsed().as_secs_f64();

    // Check if process completed
    let completed = if let Some(ref mut child) = task.child {
        child.try_wait().ok().flatten().is_some()
    } else {
        false
    };

    if completed {
        // Drain remaining output (switch to blocking)
        if let Some(ref mut stdout) = task.stdout {
            use std::io::Read;
            #[cfg(unix)]
            {
                use std::os::unix::io::AsRawFd;
                let fd = stdout.as_raw_fd();
                unsafe {
                    let flags = libc::fcntl(fd, libc::F_GETFL);
                    libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
                }
            }
            let mut remaining = String::new();
            let _ = stdout.read_to_string(&mut remaining);
            task.output_buffer.push_str(&remaining);
        }

        // Drop handles
        task.child = None;
        task.stdout = None;
        task.stdin = None;
        task.status = "completed".to_string();

        let output = task.output_buffer.clone();
        let command = task.command.clone();
        let pre_insights = task.pre_insights.clone();
        let meta_path = task.meta_path.clone();
        let task_id_str = task.task_id.clone();

        // Drop the lock before finalize (it accesses circuit_breaker)
        drop(tasks);

        // Caller is observing this task directly — clear any pending [notify] for it.
        suppress_event_for_task(state, &task_id_str);
        // Caller is actively polling — no background notification needed.
        return finalize_task(state, &task_id_str, &command, &output, elapsed, &pre_insights, &meta_path, true);
    }

    // Still running — compute output delta since last poll
    let new_bytes = task.output_buffer.len().saturating_sub(task.last_poll_offset);
    task.last_poll_offset = task.output_buffer.len();

    let insights = combine_insights(&task.pre_insights, &[]);
    let result = serde_json::json!({
        "task_id": task.task_id,
        "status": "running",
        "output": truncate_output(&task.output_buffer, state.config.truncate_output_at),
        "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
        "has_stdin": task.has_stdin,
        "new_bytes": new_bytes,
        "insights": insights,
    });
    format_task_output(result.as_object().unwrap())
}

fn handle_send(state: &Arc<ServerState>, args: &Value) -> Value {
    let task_id = match args.get("task_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return error_content("Missing required parameter: task_id"),
    };
    let input = args
        .get("input")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let mut tasks = state.tasks.lock().unwrap();
    match tasks.tasks.get_mut(task_id) {
        Some(task) if task.status == "running" => {
            if let Some(ref mut stdin) = task.stdin {
                use std::io::Write;
                let data = format!("{}\n", input);
                match stdin.write_all(data.as_bytes()) {
                    Ok(()) => {
                        let _ = stdin.flush();
                        text_content(&serde_json::to_string_pretty(&serde_json::json!({
                            "success": true,
                            "message": "Input sent"
                        })).unwrap_or_default())
                    }
                    Err(e) => error_content(&format!("Failed to write to stdin: {}", e)),
                }
            } else {
                error_content(&format!("Task {} has no stdin (not a PTY task)", task_id))
            }
        }
        Some(_) => error_content(&format!("Task {} is not running", task_id)),
        None => error_content(&format!("Unknown task: {}", task_id)),
    }
}

fn handle_kill(state: &Arc<ServerState>, args: &Value) -> Value {
    let task_id = match args.get("task_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return error_content("Missing required parameter: task_id"),
    };

    let mut tasks = state.tasks.lock().unwrap();
    match tasks.tasks.get_mut(task_id) {
        Some(task) if task.status == "running" => {
            // Kill the process
            if let Some(pid) = task.pid {
                unsafe {
                    libc::kill(pid as i32, libc::SIGTERM);
                }
                std::thread::sleep(std::time::Duration::from_millis(100));
                unsafe {
                    libc::kill(pid as i32, libc::SIGKILL);
                }
            }

            // Wait for child to reap zombie
            if let Some(ref mut child) = task.child {
                let _ = child.wait();
            }

            // Drain any remaining output
            if let Some(ref mut stdout) = task.stdout {
                let remaining = read_available(stdout);
                task.output_buffer.push_str(&remaining);
            }

            // Clean up meta file
            let _ = std::fs::remove_file(&task.meta_path);

            let elapsed = task.started_at.elapsed().as_secs_f64();
            let output = task.output_buffer.clone();
            let tid = task.task_id.clone();

            // Remove from registry
            tasks.tasks.remove(task_id);

            let result = serde_json::json!({
                "task_id": tid,
                "status": "killed",
                "output": truncate_output(&output, state.config.truncate_output_at),
                "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
            });
            format_task_output(result.as_object().unwrap())
        }
        Some(_) => error_content(&format!("Task {} is not running", task_id)),
        None => error_content(&format!("Unknown task: {}", task_id)),
    }
}

fn handle_list_tasks(state: &Arc<ServerState>) -> Value {
    let tasks = state.tasks.lock().unwrap();
    let task_list: Vec<Value> = tasks
        .tasks
        .values()
        .map(|t| {
            let cmd = if t.command.len() > 50 {
                format!("{}...", &t.command[..47])
            } else {
                t.command.clone()
            };
            let elapsed = t.started_at.elapsed().as_secs_f64();
            serde_json::json!({
                "task_id": t.task_id,
                "command": cmd,
                "status": t.status,
                "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
            })
        })
        .collect();

    text_content(
        &serde_json::to_string_pretty(&serde_json::json!({"tasks": task_list}))
            .unwrap_or_default(),
    )
}

fn handle_health(state: &Arc<ServerState>) -> Value {
    let cb_status = state.circuit_breaker.lock().unwrap().get_status();
    let alan_stats = alan::open_db(&state.db_path)
        .ok()
        .map(|conn| alan::stats::get_stats(&conn, &state.session_id));

    let active_tasks = state.tasks.lock().unwrap().tasks.len();

    let result = serde_json::json!({
        "status": "healthy",
        "neverhang": serde_json::to_value(&cb_status).unwrap_or(Value::Null),
        "alan": alan_stats.map(|s| serde_json::to_value(s).unwrap_or(Value::Null)),
        "active_tasks": active_tasks,
    });
    text_content(&serde_json::to_string_pretty(&result).unwrap_or_default())
}

fn handle_alan_stats(state: &Arc<ServerState>) -> Value {
    match alan::open_db(&state.db_path) {
        Ok(conn) => {
            let stats = alan::stats::get_stats(&conn, &state.session_id);
            text_content(
                &serde_json::to_string_pretty(&serde_json::to_value(stats).unwrap_or(Value::Null))
                    .unwrap_or_default(),
            )
        }
        Err(e) => error_content(&format!("ALAN DB error: {}", e)),
    }
}

fn handle_alan_query(state: &Arc<ServerState>, args: &Value) -> Value {
    let command = match args.get("command").and_then(|v| v.as_str()) {
        Some(c) => c,
        None => return error_content("Missing required parameter: command"),
    };

    match alan::open_db(&state.db_path) {
        Ok(conn) => {
            let result = alan::stats::query_pattern(&conn, command);
            text_content(
                &serde_json::to_string_pretty(
                    &serde_json::to_value(result).unwrap_or(Value::Null),
                )
                .unwrap_or_default(),
            )
        }
        Err(e) => error_content(&format!("ALAN DB error: {}", e)),
    }
}

fn handle_neverhang_status(state: &Arc<ServerState>) -> Value {
    let status = state.circuit_breaker.lock().unwrap().get_status();
    text_content(
        &serde_json::to_string_pretty(&serde_json::to_value(status).unwrap_or(Value::Null))
            .unwrap_or_default(),
    )
}

fn handle_neverhang_reset(state: &Arc<ServerState>) -> Value {
    state.circuit_breaker.lock().unwrap().reset();
    text_content(
        &serde_json::to_string_pretty(&serde_json::json!({
            "success": true,
            "message": "Circuit breaker reset to CLOSED state"
        }))
        .unwrap_or_default(),
    )
}

/// Combine pre and post insights into grouped {level: [messages]} map.
fn combine_insights(
    pre: &[(String, String)],
    post: &[(String, String)],
) -> serde_json::Map<String, Value> {
    let mut map: std::collections::HashMap<String, Vec<String>> =
        std::collections::HashMap::new();
    for (level, msg) in pre.iter().chain(post.iter()) {
        map.entry(level.clone()).or_default().push(msg.clone());
    }
    let mut result = serde_json::Map::new();
    for (level, messages) in map {
        result.insert(
            level,
            Value::Array(messages.into_iter().map(Value::String).collect()),
        );
    }
    result
}

/// Enqueue a background task completion event for notification on next tool call.
fn enqueue_event(state: &Arc<ServerState>, task_id: &str, exit_code: i32, elapsed: f64) {
    state.event_queue.lock().unwrap().push(CompletedEvent {
        task_id: task_id.to_string(),
        exit_code,
        elapsed,
    });
}

/// Remove any pending notification for a specific task.
/// Called by zsh_poll so directly-observed completions don't also show as [notify].
fn suppress_event_for_task(state: &Arc<ServerState>, task_id: &str) {
    let mut queue = state.event_queue.lock().unwrap();
    queue.retain(|ev| ev.task_id != task_id);
}

/// Drain all pending completion events and return formatted notification lines.
/// Events are consumed — each fires exactly once.
fn drain_events(state: &Arc<ServerState>) -> String {
    let mut queue = state.event_queue.lock().unwrap();
    if queue.is_empty() {
        return String::new();
    }
    let lines: Vec<String> = queue.drain(..).map(|ev| {
        if ev.exit_code == 0 {
            format!(
                "{}[notify]{} task '{}' completed (exit=0, {:.1}s) — use zsh_poll to retrieve output",
                C_DIM, C_RESET, ev.task_id, ev.elapsed
            )
        } else {
            format!(
                "{}[notify]{} task '{}' failed (exit={}, {:.1}s) — use zsh_poll to retrieve output",
                C_YELLOW, C_RESET, ev.task_id, ev.exit_code, ev.elapsed
            )
        }
    }).collect();
    lines.join("\n")
}

/// Prepend any pending background task notifications to a tool response.
fn prepend_events(state: &Arc<ServerState>, response: Value) -> Value {
    let notifications = drain_events(state);
    if notifications.is_empty() {
        return response;
    }
    if let Some(text) = response.get("content")
        .and_then(|c| c.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.get("text"))
        .and_then(|t| t.as_str())
    {
        return text_content(&format!("{}\n\n{}", notifications, text));
    }
    response
}

fn truncate_output(output: &str, max_len: usize) -> String {
    if output.len() <= max_len {
        output.to_string()
    } else {
        let truncated = &output[..max_len];
        format!(
            "{}\n\n[OUTPUT TRUNCATED - {} bytes total, showing first {}]",
            truncated,
            output.len(),
            max_len
        )
    }
}
