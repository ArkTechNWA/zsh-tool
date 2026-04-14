//! Rich output formatting for MCP tool responses.

use serde_json::Value;

// ANSI palette
pub const C_GREEN: &str = "\x1b[32m";
pub const C_RED: &str = "\x1b[31m";
pub const C_YELLOW: &str = "\x1b[33m";
pub const C_CYAN: &str = "\x1b[36m";
pub const C_DIM: &str = "\x1b[2m";
pub const C_BOLD: &str = "\x1b[1m";
pub const C_RESET: &str = "\x1b[0m";
pub const C_MAGENTA: &str = "\x1b[35m";
pub const C_WHITE: &str = "\x1b[37m";

pub fn separator(width: usize) -> String {
    "─".repeat(width)
}

pub fn separator_styled(width: usize) -> String {
    format!("{}{}{}", C_DIM, separator(width), C_RESET)
}

pub fn color_exit(code: i32) -> String {
    if code == 0 {
        format!("{}{}{}", C_GREEN, code, C_RESET)
    } else if code > 128 {
        format!("{}{}{}", C_YELLOW, code, C_RESET)
    } else {
        format!("{}{}{}", C_RED, code, C_RESET)
    }
}

pub fn status_icon(exit_code: i32) -> String {
    if exit_code == 0 {
        format!("{}✔{}", C_GREEN, C_RESET)
    } else {
        format!("{}✘{}", C_RED, C_RESET)
    }
}

pub const SEP_WIDTH: usize = 40;

pub fn status_completed(task_id: &str, elapsed: f64, pipestatus: &[i32]) -> String {
    let overall = *pipestatus.last().unwrap_or(&0);
    let icon = status_icon(overall);
    let mut exit_str = format!("exit={}", color_exit(overall));
    if pipestatus.len() > 1 {
        let colored: Vec<String> = pipestatus.iter().map(|&c| color_exit(c)).collect();
        exit_str = format!("{}  pipestatus=[{}]", exit_str, colored.join(","));
    }
    format!("{} {}  {:.1}s  task={}", icon, exit_str, elapsed, task_id)
}

pub fn status_running(
    task_id: &str,
    elapsed: f64,
    has_stdin: bool,
    _lines_range: Option<(usize, usize)>,
    _new_bytes: Option<u64>,
) -> String {
    format!(
        "{}⟳ RUNNING{}  {:.1}s  task={}  stdin={}",
        C_CYAN,
        C_RESET,
        elapsed,
        task_id,
        if has_stdin { "yes" } else { "no" }
    )
}

pub fn status_running_footer(
    lines_range: Option<(usize, usize)>,
    new_bytes: Option<u64>,
) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some((from, to)) = lines_range {
        parts.push(format!("↻ lines {}-{}", from, to));
    }
    if let Some(bytes) = new_bytes {
        if bytes >= 1024 {
            parts.push(format!("{:.1} KB new", bytes as f64 / 1024.0));
        } else if bytes > 0 {
            parts.push(format!("{} B new", bytes));
        }
    }
    parts.push(format!("{}zsh_poll · zsh_send · zsh_kill{}", C_DIM, C_RESET));
    parts.join("  |  ")
}

pub fn status_timeout(task_id: &str, elapsed: f64) -> String {
    format!("{}⏱ TIMEOUT{}  {:.1}s  task={}", C_YELLOW, C_RESET, elapsed, task_id)
}

pub fn status_killed(task_id: &str, elapsed: f64) -> String {
    format!("{}✘ KILLED{}  {:.1}s  task={}", C_RED, C_RESET, elapsed, task_id)
}

pub fn status_error(task_id: &str, elapsed: f64) -> String {
    format!("{}✘ ERROR{}  {:.1}s  task={}", C_RED, C_RESET, elapsed, task_id)
}

// ── Progress bar ──────────────────────────────────────────────

/// Render a progress bar: `████████████░░░░░░░░  58%`
pub fn progress_bar(percent: u32, width: usize) -> String {
    let pct = percent.min(100);
    let filled = (width as u32 * pct / 100) as usize;
    let empty = width - filled;
    format!(
        "{}{}{}{}  {}%",
        C_GREEN, "█".repeat(filled), "░".repeat(empty), C_RESET, pct
    )
}

/// Heuristic: does this line look like a progress indicator?
pub fn looks_like_progress(line: &str) -> bool {
    let trimmed = line.trim();
    // Percentage pattern: digits followed by %
    if regex_lite_percent(trimmed) {
        return true;
    }
    // Bar pattern: [===>, [###, etc.
    if trimmed.contains("[=") || trimmed.contains("[#") || trimmed.contains("[█") {
        return true;
    }
    false
}

/// Simple percent detection without regex crate.
fn regex_lite_percent(s: &str) -> bool {
    let bytes = s.as_bytes();
    for i in 0..bytes.len() {
        if bytes[i] == b'%' && i > 0 && bytes[i - 1].is_ascii_digit() {
            return true;
        }
    }
    false
}

/// Collapse consecutive progress lines, keeping only the last one.
/// Non-progress lines are always preserved.
pub fn consolidate_progress(lines: Vec<String>) -> Vec<String> {
    let mut result: Vec<String> = Vec::new();
    let mut last_progress: Option<String> = None;

    for line in lines {
        if looks_like_progress(&line) {
            last_progress = Some(line);
        } else {
            if let Some(prog) = last_progress.take() {
                result.push(prog);
            }
            result.push(line);
        }
    }
    if let Some(prog) = last_progress {
        result.push(prog);
    }
    result
}

// ── Notifications ─────────────────────────────────────────────

pub fn format_notification(task_id: &str, exit_code: i32, elapsed: f64) -> String {
    if exit_code == 0 {
        format!(
            "{}┌ notify:{} task '{}' completed (exit=0, {:.1}s)",
            C_DIM, C_RESET, task_id, elapsed
        )
    } else {
        format!(
            "{}┌ notify:{} task '{}' {}failed{} (exit={}, {:.1}s)",
            C_YELLOW, C_RESET, task_id, C_RED, C_RESET, exit_code, elapsed
        )
    }
}

// ── ALAN insights ─────────────────────────────────────────────

pub fn format_insight(level: &str, messages: &[&str]) -> String {
    let joined = messages.join(" | ");
    if level == "warning" {
        format!("{}⚠ A.L.A.N.:{} {}", C_YELLOW, C_RESET, joined)
    } else {
        format!("{}ℹ A.L.A.N.:{} {}", C_DIM, C_RESET, joined)
    }
}

// ── Placeholders ──────────────────────────────────────────────

pub fn no_output() -> String {
    format!("{}(no output){}", C_DIM, C_RESET)
}

pub fn format_error(msg: &str) -> String {
    format!("{}✘ error:{} {}", C_RED, C_RESET, msg)
}

// ── Command header ────────────────────────────────────────────

const MAX_CMD_DISPLAY: usize = 120;

pub fn command_header(command: &str) -> String {
    let display = if command.len() > MAX_CMD_DISPLAY {
        format!("{}…", &command[..MAX_CMD_DISPLAY])
    } else {
        command.to_string()
    };
    format!("{}$ {}{}", C_BOLD, display, C_RESET)
}

// ── Main compositor ───────────────────────────────────────────

/// Prefix on every tool result — reinforces the relay instruction at point of attention.
const RELAY_HEADER: &str = "[RELAY TO USER — tool output is not visible to the user. You MUST include this output verbatim in your response.]";

/// Format a complete task result into rich, structured output.
///
/// Layout for completed:
/// ```text
/// $ command here
/// ─────────────────────────────────────────
/// 1: output line
/// 2: output line
/// ─────────────────────────────────────────
/// ✔ exit=0  0.1s  task=abc12345
/// ```
pub fn format_rich_output(result: &serde_json::Map<String, Value>) -> String {
    let mut parts: Vec<String> = Vec::new();

    let status = result.get("status").and_then(|v| v.as_str()).unwrap_or("unknown");
    let task_id = result.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
    let elapsed = result.get("elapsed_seconds").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let command = result.get("command").and_then(|v| v.as_str()).unwrap_or("");
    let output = result.get("output").and_then(|v| v.as_str()).unwrap_or("");

    // Relay instruction — model sees this at top of every result
    parts.push(RELAY_HEADER.to_string());

    // Command header
    if !command.is_empty() {
        parts.push(command_header(command));
    }

    // Separator before output
    parts.push(separator_styled(SEP_WIDTH));

    // Output body
    if !output.trim().is_empty() {
        let lines: Vec<String> = output
            .trim_end_matches('\n')
            .split('\n')
            .map(|s| s.to_string())
            .collect();
        let consolidated = consolidate_progress(lines);
        for line in consolidated {
            parts.push(line);
        }
    } else if status == "completed" || status == "error" {
        parts.push(no_output());
    }

    // Error field
    if let Some(error) = result.get("error").and_then(|v| v.as_str()) {
        parts.push(format_error(error));
    }

    // Separator before status
    parts.push(separator_styled(SEP_WIDTH));

    // Status line
    match status {
        "running" => {
            let has_stdin = result.get("has_stdin").and_then(|v| v.as_bool()).unwrap_or(false);
            let from_line = result.get("from_line").and_then(|v| v.as_u64()).map(|v| v as usize);
            let to_line = result.get("to_line").and_then(|v| v.as_u64()).map(|v| v as usize);
            let new_bytes = result.get("new_bytes").and_then(|v| v.as_u64());
            let lines_range = match (from_line, to_line) {
                (Some(f), Some(t)) if f > 0 && t > 0 => Some((f, t)),
                _ => None,
            };
            parts.push(status_running(task_id, elapsed, has_stdin, lines_range, new_bytes));
            parts.push(status_running_footer(lines_range, new_bytes));
        }
        "completed" => {
            let pipestatus: Vec<i32> = result
                .get("pipestatus")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|v| v.as_i64().map(|n| n as i32)).collect())
                .unwrap_or_else(|| vec![0]);
            parts.push(status_completed(task_id, elapsed, &pipestatus));
        }
        "timeout" => parts.push(status_timeout(task_id, elapsed)),
        "killed" => parts.push(status_killed(task_id, elapsed)),
        "error" => parts.push(status_error(task_id, elapsed)),
        _ => {}
    }

    // ALAN insights
    if let Some(insights) = result.get("insights").and_then(|v| v.as_object()) {
        for (level, messages) in insights {
            if let Some(arr) = messages.as_array() {
                let msgs: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                if !msgs.is_empty() {
                    parts.push(format_insight(level, &msgs));
                }
            }
        }
    }

    parts.join("\n")
}

/// Format a batch of background task completion notifications.
pub fn format_notifications(events: &[(String, i32, f64)]) -> String {
    if events.is_empty() {
        return String::new();
    }
    events
        .iter()
        .map(|(task_id, exit_code, elapsed)| format_notification(task_id, *exit_code, *elapsed))
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_separator_default_width() {
        let sep = separator(40);
        assert_eq!(sep.chars().count(), 40);
        assert!(sep.chars().all(|c| c == '─'));
    }

    #[test]
    fn test_separator_styled_has_ansi() {
        let sep = separator_styled(40);
        assert!(sep.contains('\x1b'));
        assert!(sep.contains('─'));
    }

    #[test]
    fn test_color_exit_zero_is_green() {
        let s = color_exit(0);
        assert!(s.contains("\x1b[32m"));
        assert!(s.contains("0"));
    }

    #[test]
    fn test_color_exit_nonzero_is_red() {
        let s = color_exit(1);
        assert!(s.contains("\x1b[31m"));
    }

    #[test]
    fn test_color_exit_signal_is_yellow() {
        let s = color_exit(137);
        assert!(s.contains("\x1b[33m"));
    }

    #[test]
    fn test_status_icon_success() {
        assert_eq!(status_icon(0), format!("{}✔{}", C_GREEN, C_RESET));
    }

    #[test]
    fn test_status_icon_failure() {
        assert_eq!(status_icon(1), format!("{}✘{}", C_RED, C_RESET));
    }

    #[test]
    fn test_status_completed_success() {
        let s = status_completed("abc123", 1.5, &[0]);
        assert!(s.contains("✔"));
        assert!(s.contains("exit="));
        assert!(s.contains("1.5s"));
        assert!(s.contains("abc123"));
        assert!(!s.contains("pipestatus"));
    }

    #[test]
    fn test_status_completed_failure_with_pipestatus() {
        let s = status_completed("abc123", 2.0, &[0, 0, 1]);
        assert!(s.contains("✘"));
        assert!(s.contains("pipestatus="));
    }

    #[test]
    fn test_status_running() {
        let s = status_running("abc123", 5.0, true, Some((10, 15)), None);
        assert!(s.contains("⟳"));
        assert!(s.contains("RUNNING"));
        assert!(s.contains("stdin=yes"));
    }

    #[test]
    fn test_status_running_bytes_delta() {
        let footer = status_running_footer(None, Some(2048));
        assert!(footer.contains("2.0 KB new"));
    }

    #[test]
    fn test_status_timeout() {
        let s = status_timeout("abc123", 30.0);
        assert!(s.contains("TIMEOUT"));
        assert!(s.contains("\x1b[33m"));
    }

    #[test]
    fn test_status_killed() {
        let s = status_killed("abc123", 10.0);
        assert!(s.contains("KILLED"));
        assert!(s.contains("\x1b[31m"));
    }

    #[test]
    fn test_status_error() {
        let s = status_error("abc123", 0.5);
        assert!(s.contains("ERROR"));
        assert!(s.contains("\x1b[31m"));
    }

    #[test]
    fn test_progress_bar_50_percent() {
        let bar = progress_bar(50, 20);
        assert!(bar.contains("█"));
        assert!(bar.contains("░"));
        assert!(bar.contains("50%"));
    }

    #[test]
    fn test_progress_bar_100_percent() {
        let bar = progress_bar(100, 20);
        assert!(!bar.contains("░"));
        assert!(bar.contains("100%"));
    }

    #[test]
    fn test_progress_bar_0_percent() {
        let bar = progress_bar(0, 20);
        assert!(bar.contains("0%"));
    }

    #[test]
    fn test_detect_progress_line() {
        assert!(looks_like_progress("Downloading: 49% [#####.....]"));
        assert!(looks_like_progress("  50%  ETA: 30s"));
        assert!(looks_like_progress("[========>     ] 65%"));
        assert!(!looks_like_progress("Hello world"));
        assert!(!looks_like_progress("exit code: 0"));
    }

    #[test]
    fn test_consolidate_progress_lines() {
        let lines = vec![
            "Starting download...".to_string(),
            "Progress: 10%".to_string(),
            "Progress: 20%".to_string(),
            "Progress: 30%".to_string(),
            "Done.".to_string(),
        ];
        let consolidated = consolidate_progress(lines);
        assert!(consolidated.iter().any(|l| l.contains("Starting")));
        assert!(consolidated.iter().any(|l| l.contains("30%")));
        assert!(consolidated.iter().any(|l| l.contains("Done")));
        assert!(!consolidated.iter().any(|l| l.contains("10%")));
        assert!(!consolidated.iter().any(|l| l.contains("20%")));
    }

    #[test]
    fn test_format_notification_success() {
        let s = format_notification("abc123", 0, 2.1);
        assert!(s.contains("┌ notify"));
        assert!(s.contains("completed"));
        assert!(s.contains("abc123"));
    }

    #[test]
    fn test_format_notification_failure() {
        let s = format_notification("abc123", 127, 0.4);
        assert!(s.contains("failed"));
        assert!(s.contains("127"));
    }

    #[test]
    fn test_format_insight_warning() {
        let s = format_insight("warning", &["retry detected", "possible loop"]);
        assert!(s.contains("⚠"));
        assert!(s.contains("A.L.A.N."));
        assert!(s.contains("retry detected"));
        assert!(s.contains("\x1b[33m"));
    }

    #[test]
    fn test_format_insight_info() {
        let s = format_insight("info", &["command completed normally"]);
        assert!(s.contains("ℹ"));
        assert!(s.contains("A.L.A.N."));
    }

    #[test]
    fn test_no_output_placeholder() {
        let s = no_output();
        assert!(s.contains("(no output)"));
        assert!(s.contains("\x1b[2m"));
    }

    #[test]
    fn test_command_header() {
        let s = command_header("echo hello && ls /tmp");
        assert!(s.contains("$"));
        assert!(s.contains("echo hello && ls /tmp"));
        assert!(s.contains(C_BOLD));
    }

    #[test]
    fn test_command_header_long_truncates() {
        let long_cmd = "a".repeat(200);
        let s = command_header(&long_cmd);
        assert!(s.contains("…"));
    }

    // ── Tasks 7-8 tests ───────────────────────────────────────

    use serde_json::json;

    fn make_result(overrides: serde_json::Value) -> serde_json::Map<String, serde_json::Value> {
        let mut base = json!({
            "task_id": "abc12345",
            "status": "completed",
            "output": "hello world\n",
            "elapsed_seconds": 0.1,
            "pipestatus": [0],
        });
        if let serde_json::Value::Object(map) = overrides {
            for (k, v) in map {
                base[&k] = v;
            }
        }
        base.as_object().unwrap().clone()
    }

    #[test]
    fn test_rich_output_completed_success() {
        let result = make_result(json!({"command": "echo hello"}));
        let text = format_rich_output(&result);
        assert!(text.contains("$ echo hello"));
        assert!(text.contains("─"));
        assert!(text.contains("hello world"));
        assert!(text.contains("✔"));
        assert!(text.contains("exit="));
    }

    #[test]
    fn test_rich_output_completed_no_output() {
        let result = make_result(json!({"output": "", "command": "mkdir /tmp/test"}));
        let text = format_rich_output(&result);
        assert!(text.contains("(no output)"));
        assert!(text.contains("✔"));
    }

    #[test]
    fn test_rich_output_failed() {
        let result = make_result(json!({
            "output": "cat: /nope: No such file or directory\n",
            "pipestatus": [1],
            "command": "cat /nope"
        }));
        let text = format_rich_output(&result);
        assert!(text.contains("✘"));
        assert!(text.contains("No such file"));
    }

    #[test]
    fn test_rich_output_running() {
        let result = make_result(json!({
            "status": "running",
            "output": "line 1\nline 2\n",
            "has_stdin": false,
            "from_line": 1,
            "to_line": 2,
            "command": "long_running_cmd"
        }));
        let text = format_rich_output(&result);
        assert!(text.contains("⟳"));
        assert!(text.contains("RUNNING"));
        assert!(text.contains("line 1"));
    }

    #[test]
    fn test_rich_output_with_error_field() {
        let result = make_result(json!({
            "error": "spawn failed",
            "status": "error",
            "output": "",
            "command": "bad_cmd"
        }));
        let text = format_rich_output(&result);
        assert!(text.contains("✘ error:"));
        assert!(text.contains("spawn failed"));
    }

    #[test]
    fn test_rich_output_with_insights() {
        let result = make_result(json!({
            "command": "retry_cmd",
            "insights": {
                "warning": ["retry detected"],
                "info": ["completed normally"]
            }
        }));
        let text = format_rich_output(&result);
        assert!(text.contains("⚠ A.L.A.N."));
        assert!(text.contains("ℹ A.L.A.N."));
    }

    #[test]
    fn test_rich_output_with_progress_consolidation() {
        let output = "Starting...\nProgress: 10%\nProgress: 20%\nProgress: 30%\nDone.\n";
        let result = make_result(json!({
            "output": output,
            "command": "download_thing"
        }));
        let text = format_rich_output(&result);
        assert!(!text.contains("10%"));
        assert!(!text.contains("20%"));
        assert!(text.contains("30%"));
        assert!(text.contains("Done."));
    }

    #[test]
    fn test_format_notifications_block() {
        let events = vec![
            ("task1".to_string(), 0_i32, 2.1_f64),
            ("task2".to_string(), 127, 0.4),
        ];
        let block = format_notifications(&events);
        assert!(block.contains("┌ notify"));
        assert!(block.contains("task1"));
        assert!(block.contains("task2"));
        assert!(block.contains("failed"));
        assert!(block.contains("completed"));
    }

    #[test]
    fn test_format_notifications_empty() {
        let events: Vec<(String, i32, f64)> = vec![];
        let block = format_notifications(&events);
        assert!(block.is_empty());
    }
}
