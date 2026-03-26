# Poll Delta Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `zsh_poll` return only new output (delta) with global line numbers instead of the full buffer on every call.

**Architecture:** Add `last_poll_line` to `TaskInfo` for line counting alongside the existing `last_poll_offset` byte cursor. A shared `number_lines()` helper slices, numbers, and truncates output. The `full_output` bool parameter toggles between delta and full buffer.

**Tech Stack:** Rust, serde_json, MCP protocol

**Spec:** `docs/superpowers/specs/2026-03-21-poll-delta-output-design.md`
**Issue:** arktechnwa/mcp/zsh-tool#44
**Branch:** `fix/poll-delta-output`

---

### Task 1: Add `last_poll_line` to TaskInfo and `full_output` to tool definition

**Files:**
- Modify: `zsh-tool-rs/src/serve/mod.rs:47-65` (TaskInfo struct)
- Modify: `zsh-tool-rs/src/serve/mod.rs:674` (TaskInfo initialization in handle_zsh)
- Modify: `zsh-tool-rs/src/serve/tools.rs:39-51` (zsh_poll tool definition)

- [ ] **Step 1: Add `last_poll_line` field to TaskInfo**

In `zsh-tool-rs/src/serve/mod.rs`, add the field after `last_poll_offset`:

```rust
pub struct TaskInfo {
    pub task_id: String,
    pub command: String,
    pub started_at: std::time::Instant,
    pub started_at_epoch: f64,
    pub status: String,
    pub output_buffer: String,
    pub last_poll_offset: usize,
    pub last_poll_line: usize,  // <-- NEW: global line count at last poll
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
```

- [ ] **Step 2: Initialize `last_poll_line: 0` in handle_zsh**

In `zsh-tool-rs/src/serve/mod.rs` at the TaskInfo construction (~line 667-684), add the field:

```rust
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
        last_poll_line: 0,  // <-- NEW
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
```

- [ ] **Step 3: Add `full_output` parameter to zsh_poll tool definition**

In `zsh-tool-rs/src/serve/tools.rs`, update the zsh_poll tool definition:

```rust
tool_def("zsh_poll",
    "Get more output from a running task. Returns delta (new output since last poll) with global line numbers by default. Pass full_output=true for entire buffer. Call repeatedly until status is not 'running'.",
    json!({
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID returned from zsh command"
            },
            "full_output": {
                "type": "boolean",
                "description": "Return entire output buffer with line numbers instead of just the delta since last poll (default: false)"
            }
        },
        "required": ["task_id"]
    })
),
```

- [ ] **Step 4: Verify it compiles**

Run: `cd zsh-tool-rs && cargo check 2>&1 | tail -5`
Expected: Compilation succeeds (no behavioral change yet, just struct + schema)

- [ ] **Step 5: Commit**

```bash
git add zsh-tool-rs/src/serve/mod.rs zsh-tool-rs/src/serve/tools.rs
git commit -m "feat(poll): add last_poll_line to TaskInfo and full_output param to tool def

Part of #44 — poll delta output with line numbers.
No behavioral change yet — struct field and schema only."
```

---

### Task 2: Write the `number_lines` helper

**Files:**
- Modify: `zsh-tool-rs/src/serve/mod.rs` (add helper near `truncate_output` at ~line 1078)

- [ ] **Step 1: Write the helper function**

Add this function near `truncate_output` in `zsh-tool-rs/src/serve/mod.rs`:

```rust
/// Slice output to a delta or full range, prepend global line numbers,
/// and apply truncation. Returns (numbered_output, from_line, to_line).
/// `from_line` and `to_line` are 1-based. Returns (empty, 0, 0) if slice is empty.
fn number_lines(
    full_buffer: &str,
    byte_offset: usize,
    line_offset: usize,
    full_output: bool,
    max_len: usize,
) -> (String, usize, usize) {
    let slice = if full_output {
        full_buffer
    } else {
        &full_buffer[byte_offset..]
    };

    if slice.is_empty() {
        return (String::new(), 0, 0);
    }

    let start_line = if full_output { 1 } else { line_offset + 1 };

    let mut numbered = String::new();
    let mut line_num = start_line;
    for line in slice.split('\n') {
        if !numbered.is_empty() {
            numbered.push('\n');
        }
        numbered.push_str(&format!("{}: {}", line_num, line));
        line_num += 1;
    }
    // split('\n') on "a\n" gives ["a", ""] — the trailing empty element
    // is an artifact, not a real line. Adjust count.
    let to_line = if slice.ends_with('\n') {
        line_num - 2  // back up past the empty trailing split
    } else {
        line_num - 1
    };
    let from_line = start_line;

    // Apply truncation to the numbered output
    let truncated = truncate_output(&numbered, max_len);

    // If truncated, recalculate to_line from what's actually returned
    let actual_to_line = if truncated.len() > numbered.len() || truncated.contains("[OUTPUT TRUNCATED") {
        // Count actual lines in the truncated output (before the truncation notice)
        let content_before_notice = if let Some(pos) = truncated.find("\n\n[OUTPUT TRUNCATED") {
            &truncated[..pos]
        } else {
            &truncated
        };
        let last_numbered_line = content_before_notice.lines().last().unwrap_or("");
        // Parse the line number from "NNN: content"
        last_numbered_line
            .split(':')
            .next()
            .and_then(|s| s.trim().parse::<usize>().ok())
            .unwrap_or(to_line)
    } else {
        to_line
    };

    (truncated, from_line, actual_to_line)
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd zsh-tool-rs && cargo check 2>&1 | tail -5`
Expected: Compiles (function is unused, may get a warning — that's fine)

- [ ] **Step 3: Commit**

```bash
git add zsh-tool-rs/src/serve/mod.rs
git commit -m "feat(poll): add number_lines helper for delta output

Slices output buffer, prepends global line numbers, applies truncation.
Returns (output, from_line, to_line) tuple. Part of #44."
```

---

### Task 3: Wire `number_lines` into `handle_poll` running path

**Files:**
- Modify: `zsh-tool-rs/src/serve/mod.rs:714-815` (handle_poll function)

- [ ] **Step 1: Write a failing test for delta output**

Add this test to `zsh-tool-rs/tests/mcp_integration.rs`:

```rust
#[test]
fn test_poll_returns_delta_with_line_numbers() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    // Start a command that produces multiple lines over time
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "for i in $(seq 1 10); do echo \"line-$i\"; sleep 0.15; done",
                "timeout": 30,
                "yield_after": 0.1
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("RUNNING"), "should yield RUNNING, got: {}", text);
    let task_id = extract_task_id(text);

    // Let some output accumulate
    std::thread::sleep(Duration::from_millis(600));

    // First poll — should get delta with line numbers starting at 1
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

    // Should contain numbered lines like "1: line-1"
    assert!(text.contains("1: "), "first poll should have line numbers starting at 1, got:\n{}", text);

    if text.contains("RUNNING") {
        // Let more output come
        std::thread::sleep(Duration::from_millis(600));

        // Second poll — line numbers should continue from where first poll left off
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
        let text2 = resp["result"]["content"][0]["text"].as_str().unwrap();

        // Should NOT start at "1:" again — should continue from previous
        if text2.contains("RUNNING") || text2.contains("COMPLETED") {
            // If there's output, first line number should be > 1
            let lines: Vec<&str> = text2.lines().collect();
            if let Some(first_content_line) = lines.first() {
                if first_content_line.contains(": line-") {
                    assert!(
                        !first_content_line.starts_with("1: "),
                        "second poll should NOT restart at line 1, got:\n{}", text2
                    );
                }
            }
        }
    }

    // Wait for completion
    std::thread::sleep(Duration::from_secs(2));

    // Final poll with full_output=true — should have all lines numbered from 1
    send_request(
        &mut stdin,
        "tools/call",
        5,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id, "full_output": true }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("1: "), "full_output should start at line 1, got:\n{}", text);
    assert!(text.contains("line-10"), "full_output should contain last line, got:\n{}", text);

    drop(stdin);
    let _ = child.wait();
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd zsh-tool-rs && cargo test test_poll_returns_delta_with_line_numbers -- --test-threads=1 2>&1 | tail -20`
Expected: FAIL — output currently has no line numbers

- [ ] **Step 3: Parse `full_output` param and wire `number_lines` into running path**

In `handle_poll` in `zsh-tool-rs/src/serve/mod.rs`, update the function. After extracting `task_id` (line 715-718), parse the new parameter:

```rust
fn handle_poll(state: &Arc<ServerState>, args: &Value) -> Value {
    let task_id = match args.get("task_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return error_content("Missing required parameter: task_id"),
    };

    let full_output = args
        .get("full_output")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
```

Then replace the "still running" output section (lines 800-814) with:

```rust
    // Still running — compute output delta since last poll
    let new_bytes = task.output_buffer.len().saturating_sub(task.last_poll_offset);

    let (numbered_output, from_line, to_line) = number_lines(
        &task.output_buffer,
        task.last_poll_offset,
        task.last_poll_line,
        full_output,
        state.config.truncate_output_at,
    );

    // Update cursors (only when returning delta, not full)
    if !full_output {
        // Count lines in the delta slice for next poll
        let delta_slice = &task.output_buffer[task.last_poll_offset..];
        let delta_line_count = delta_slice.matches('\n').count();
        task.last_poll_line += delta_line_count;
        task.last_poll_offset = task.output_buffer.len();
    }

    let insights = combine_insights(&task.pre_insights, &[]);
    let mut result = serde_json::json!({
        "task_id": task.task_id,
        "status": "running",
        "output": numbered_output,
        "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
        "has_stdin": task.has_stdin,
        "new_bytes": new_bytes,
        "insights": insights,
    });
    if from_line > 0 {
        result["from_line"] = serde_json::json!(from_line);
        result["to_line"] = serde_json::json!(to_line);
    }
    format_task_output(result.as_object().unwrap())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd zsh-tool-rs && cargo test test_poll_returns_delta_with_line_numbers -- --test-threads=1 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd zsh-tool-rs && cargo test -- --test-threads=1 2>&1 | tail -30`
Expected: All existing tests pass (some may need minor assertion adjustments for the new line number format)

- [ ] **Step 6: Fix any broken existing tests**

The existing `test_poll_running_task_shows_output_delta` test checks for output content like `"output line"`. After this change, lines will be prefixed with numbers (e.g., `"1: output line 1"`). Update assertions to account for the `N: ` prefix. The test at line 630 checking `text.contains("output line")` should still pass since `"1: output line 1"` contains `"output line"`. But check all assertions and fix any that fail.

- [ ] **Step 7: Commit**

```bash
git add zsh-tool-rs/src/serve/mod.rs zsh-tool-rs/tests/mcp_integration.rs
git commit -m "feat(poll): return delta with line numbers in running path

handle_poll now returns only new output since last poll, prefixed with
global line numbers. full_output=true returns entire buffer.
from_line/to_line fields added to response. Part of #44."
```

---

### Task 4: Wire `number_lines` into completed/re-poll paths

**Files:**
- Modify: `zsh-tool-rs/src/serve/mod.rs:729-742` (already-completed re-poll path)
- Modify: `zsh-tool-rs/src/serve/mod.rs:761-797` (completion-during-poll path + finalize_task call)
- Modify: `zsh-tool-rs/src/serve/mod.rs:438-511` (finalize_task function)

- [ ] **Step 1: Write a failing test for delta on completion**

Add to `zsh-tool-rs/tests/mcp_integration.rs`:

```rust
#[test]
fn test_poll_completion_returns_delta_not_full() {
    let (mut stdin, mut reader, mut child) = spawn_server();

    send_request(&mut stdin, "initialize", 1, None);
    let _ = read_response(&mut reader);
    send_notification(&mut stdin, "notifications/initialized");

    // Command that produces output then finishes
    send_request(
        &mut stdin,
        "tools/call",
        2,
        Some(serde_json::json!({
            "name": "zsh",
            "arguments": {
                "command": "echo first-batch; sleep 0.3; echo second-batch; sleep 0.3; echo final-line",
                "timeout": 10,
                "yield_after": 0.1
            }
        })),
    );

    let resp = read_response(&mut reader);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("RUNNING"), "should yield RUNNING, got: {}", text);
    let task_id = extract_task_id(text);

    // First poll while still running — consumes some output
    std::thread::sleep(Duration::from_millis(400));
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
    let text1 = resp["result"]["content"][0]["text"].as_str().unwrap();
    // Should contain first-batch with line number
    assert!(text1.contains("first-batch"), "first poll should have first-batch, got:\n{}", text1);

    // Wait for completion
    std::thread::sleep(Duration::from_secs(1));

    // Poll again — catches completion, should return delta (not repeat first-batch)
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
    let text2 = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text2.contains("COMPLETED") || text2.contains("FAILED"),
        "should be completed, got:\n{}", text2);
    assert!(text2.contains("final-line"), "completion poll should have final-line, got:\n{}", text2);
    // Should NOT repeat first-batch — that was in the first poll's delta
    // (This may or may not hold depending on timing, so only assert if we confirmed
    // first-batch was in the first poll)
    if text1.contains("first-batch") && !text1.contains("final-line") {
        assert!(!text2.starts_with("1: "),
            "completion delta should not restart at line 1, got:\n{}", text2);
    }

    // Re-poll completed task — should return empty delta
    send_request(
        &mut stdin,
        "tools/call",
        5,
        Some(serde_json::json!({
            "name": "zsh_poll",
            "arguments": { "task_id": task_id }
        })),
    );

    let resp = read_response(&mut reader);
    let text3 = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text3.contains("COMPLETED") || text3.contains("FAILED"),
        "re-poll should still show completed, got:\n{}", text3);

    drop(stdin);
    let _ = child.wait();
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd zsh-tool-rs && cargo test test_poll_completion_returns_delta_not_full -- --test-threads=1 2>&1 | tail -20`
Expected: FAIL — completion path still returns full unnumbered output

- [ ] **Step 3: Update the already-completed re-poll path (lines 729-742)**

Replace the block at lines 729-742 in `handle_poll`:

```rust
    // If already finalized, return delta from where we left off
    if task.status != "running" {
        let (numbered_output, from_line, to_line) = number_lines(
            &task.output_buffer,
            task.last_poll_offset,
            task.last_poll_line,
            full_output,
            state.config.truncate_output_at,
        );

        // Update cursors for subsequent re-polls
        if !full_output {
            let delta_slice = &task.output_buffer[task.last_poll_offset..];
            let delta_line_count = delta_slice.matches('\n').count();
            task.last_poll_line += delta_line_count;
            task.last_poll_offset = task.output_buffer.len();
        }

        let mut result = serde_json::json!({
            "task_id": task.task_id,
            "status": task.status,
            "output": numbered_output,
            "elapsed_seconds": format!("{:.1}", task.started_at.elapsed().as_secs_f64())
                .parse::<f64>().unwrap_or(0.0),
            "pipestatus": task.pipestatus,
        });
        if from_line > 0 {
            result["from_line"] = serde_json::json!(from_line);
            result["to_line"] = serde_json::json!(to_line);
        }
        // Caller is observing this task directly — clear any pending [notify] for it.
        drop(tasks);
        suppress_event_for_task(state, task_id);
        return format_task_output(result.as_object().unwrap());
    }
```

- [ ] **Step 4: Update the completion-during-poll path**

The block at lines 761-797 finalizes the task and calls `finalize_task`. We need to pass delta context through. Replace the completion block (after draining output and dropping handles):

After `task.status = "completed".to_string();` (line 783), before cloning output, update the cursors and compute the delta:

```rust
        task.child = None;
        task.stdout = None;
        task.stdin = None;
        task.status = "completed".to_string();

        // Compute delta output with line numbers before dropping lock
        let (numbered_output, from_line, to_line) = number_lines(
            &task.output_buffer,
            task.last_poll_offset,
            task.last_poll_line,
            full_output,
            state.config.truncate_output_at,
        );
        let new_bytes = task.output_buffer.len().saturating_sub(task.last_poll_offset);

        // Update cursors
        if !full_output {
            let delta_slice = &task.output_buffer[task.last_poll_offset..];
            let delta_line_count = delta_slice.matches('\n').count();
            task.last_poll_line += delta_line_count;
            task.last_poll_offset = task.output_buffer.len();
        }

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
        return finalize_task(
            state, &task_id_str, &command, &output, elapsed,
            &pre_insights, &meta_path, true,
            Some((&numbered_output, from_line, to_line)),
        );
```

- [ ] **Step 5: Add `output_override` parameter to `finalize_task`**

Add an optional parameter to `finalize_task` that lets the caller pass pre-computed numbered output. When `Some`, use that instead of calling `truncate_output`. When `None`, existing behavior is unchanged.

In `finalize_task` signature (~line 438), add the parameter:

```rust
fn finalize_task(
    state: &Arc<ServerState>,
    task_id: &str,
    command: &str,
    output: &str,
    elapsed: f64,
    pre_insights: &[(String, String)],
    meta_path: &str,
    suppress_notification: bool,
    output_override: Option<(&str, usize, usize)>,  // (numbered_output, from_line, to_line)
) -> Value {
```

At the response construction (~line 501-510), replace:

```rust
    let (final_output, from_line, to_line) = match output_override {
        Some((numbered, fl, tl)) => (numbered.to_string(), fl, tl),
        None => {
            let out = truncate_output(output, state.config.truncate_output_at);
            (out, 0, 0)
        }
    };

    let mut result = serde_json::json!({
        "success": overall_exit == 0,
        "task_id": task_id,
        "status": "completed",
        "output": final_output,
        "elapsed_seconds": format!("{:.1}", elapsed).parse::<f64>().unwrap_or(elapsed),
        "pipestatus": pipestatus,
        "insights": insights,
    });
    if from_line > 0 {
        result["from_line"] = serde_json::json!(from_line);
        result["to_line"] = serde_json::json!(to_line);
    }
    format_task_output(result.as_object().unwrap())
```

Update **all** existing callers of `finalize_task` to pass `None` for `output_override`. Find them with:

```
grep -n "finalize_task(" zsh-tool-rs/src/serve/mod.rs
```

Every call site that isn't the `handle_poll` completion path gets `None` appended as the last argument. The `handle_poll` completion path (Step 4 above) passes `Some(...)`.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd zsh-tool-rs && cargo test test_poll_completion_returns_delta_not_full -- --test-threads=1 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 7: Run full test suite**

Run: `cd zsh-tool-rs && cargo test -- --test-threads=1 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add zsh-tool-rs/src/serve/mod.rs zsh-tool-rs/tests/mcp_integration.rs
git commit -m "feat(poll): wire delta output into completion and re-poll paths

finalize_task accepts output_override for pre-numbered delta.
Re-poll of completed tasks returns delta from last cursor position.
Subsequent re-polls return empty delta. Part of #44."
```

---

### Task 5: Update `format_task_output` status line to show line range

**Files:**
- Modify: `zsh-tool-rs/src/serve/mod.rs:288-316` (format_task_output running status line)

- [ ] **Step 1: Update the RUNNING status line to show line range instead of byte delta**

In `format_task_output`, replace the delta_str logic (lines 298-304) and the status line format:

```rust
        "running" => {
            let has_stdin = result
                .get("has_stdin")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let new_bytes = result
                .get("new_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let from_line = result
                .get("from_line")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let to_line = result
                .get("to_line")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);

            let delta_str = if from_line > 0 && to_line > 0 {
                format!(" — lines {}-{}", from_line, to_line)
            } else if new_bytes >= 1024 {
                format!(" — {:.1} KB new", new_bytes as f64 / 1024.0)
            } else if new_bytes > 0 {
                format!(" — {} B new", new_bytes)
            } else {
                String::new()
            };
```

- [ ] **Step 2: Run full test suite**

Run: `cd zsh-tool-rs && cargo test -- --test-threads=1 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add zsh-tool-rs/src/serve/mod.rs
git commit -m "feat(poll): show line range in RUNNING status line

Status line now shows 'lines 801-850' instead of byte count
when line numbers are available. Part of #44."
```

---

### Task 6: Push and create MR

- [ ] **Step 1: Run full test suite one final time**

Run: `cd zsh-tool-rs && cargo test -- --test-threads=1 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 2: Run clippy**

Run: `cd zsh-tool-rs && cargo clippy -- -D warnings 2>&1 | tail -20`
Expected: No warnings

- [ ] **Step 3: Push to origin**

```bash
git push origin fix/poll-delta-output
```

- [ ] **Step 4: Create merge request on GitLab**

Create MR via GitLab API:
- Source: `fix/poll-delta-output`
- Target: `master`
- Title: `feat(poll): delta output with line numbers (closes #44)`
- Description: Summary of changes, link to spec and issue
