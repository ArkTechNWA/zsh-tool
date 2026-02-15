//! Pre-execution and post-execution insight generation.
//!
//! Pre-insights: retry detection, streak info, pattern history, SSH, manopt.
//! Post-insights: exit code awareness, pipe masking, silent detection.

use rusqlite::Connection;
use std::collections::HashMap;

use super::hash;

// --- Command awareness (bounded) ---

fn known_exit_codes() -> HashMap<&'static str, HashMap<i32, &'static str>> {
    let mut map = HashMap::new();
    map.insert("grep", HashMap::from([(1, "no match")]));
    map.insert("diff", HashMap::from([(1, "files differ")]));
    map.insert("test", HashMap::from([(1, "condition false")]));
    map.insert("[", HashMap::from([(1, "condition false")]));
    map.insert("cmp", HashMap::from([(1, "files differ")]));
    map
}

fn universal_exit_codes() -> HashMap<i32, &'static str> {
    HashMap::from([
        (126, "permission denied"),
        (127, "command not found"),
        (255, "SSH connection failed"),
    ])
}

/// Generate pre-execution insights for a command.
/// Returns Vec of (level, message) tuples. Level is "info" or "warning".
pub fn get_pre_insights(
    conn: &Connection,
    command: &str,
    session_id: &str,
    streak_threshold: i64,
    recent_window_minutes: u64,
) -> Vec<(String, String)> {
    let mut insights = Vec::new();
    let command_hash = hash::hash_command(command);
    let command_template = hash::template_command(command);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let window_start = now - (recent_window_minutes as f64 * 60.0);

    // --- Recent activity (retry detection) ---
    let (is_retry, retry_count, recent_successes, recent_failures) =
        get_recent_exact(conn, &command_hash, window_start);

    let similar = get_recent_similar(conn, &command_template, &command_hash, window_start);

    // Retry detection
    if is_retry && retry_count >= 1 {
        if recent_failures > 0 && recent_successes == 0 {
            insights.push((
                "warning".into(),
                format!(
                    "Retry #{}. Previous {} all failed. Different approach?",
                    retry_count + 1,
                    recent_failures
                ),
            ));
        } else if recent_successes > 0 && recent_failures == 0 {
            insights.push((
                "info".into(),
                format!("Retry #{}. Previous {} succeeded.", retry_count + 1, recent_successes),
            ));
        } else {
            insights.push((
                "info".into(),
                format!(
                    "Retry #{} in last {}m. {}/{} succeeded.",
                    retry_count + 1,
                    recent_window_minutes,
                    recent_successes,
                    retry_count
                ),
            ));
        }
    }

    // Similar commands
    if !similar.is_empty() && !is_retry {
        let sim_success = similar.iter().filter(|(_, s)| *s).count();
        insights.push((
            "info".into(),
            format!(
                "Similar to '{}' - {}/{} succeeded recently.",
                command_template,
                sim_success,
                similar.len()
            ),
        ));
    }

    // --- Streak info ---
    if let Some((current, _longest_success, _longest_fail)) = get_streak(conn, &command_hash) {
        if current >= streak_threshold {
            insights.push((
                "info".into(),
                format!("Streak: {} successes in a row. Solid.", current),
            ));
        } else if current <= -streak_threshold {
            insights.push((
                "warning".into(),
                format!("Failing streak: {}. Same approach?", current.unsigned_abs()),
            ));
        }
    }

    // --- Pattern history ---
    if let Some(stats) = get_pattern_stats(conn, &command_hash) {
        if stats.timeout_rate > 0.5 {
            insights.push((
                "warning".into(),
                format!("{:.0}% timeout rate for this pattern.", stats.timeout_rate * 100.0),
            ));
        } else if stats.success_rate > 0.9 && stats.observations >= 5 {
            insights.push((
                "info".into(),
                format!(
                    "Reliable pattern: {:.0}% success ({} runs).",
                    stats.success_rate * 100.0,
                    stats.observations
                ),
            ));
        }

        if let Some(avg_ms) = stats.avg_duration_ms {
            let avg_sec = avg_ms / 1000.0;
            if avg_sec > 10.0 {
                insights.push(("info".into(), format!("Usually takes ~{:.0}s.", avg_sec)));
            }
        }
    } else {
        insights.push(("info".into(), "New pattern. No history yet.".into()));
    }

    // --- SSH-specific insights ---
    let ssh_insights = super::ssh::get_ssh_insights(conn, command);
    insights.extend(ssh_insights);

    // --- manopt presentation on repeated failures ---
    let fail_count = get_template_fail_count(conn, session_id, &command_template);
    // fail_present - 1 because we haven't recorded the current execution yet
    if fail_count >= 2 {
        let base_cmd = extract_base_command(command);
        if !base_cmd.is_empty() {
            if let Some(manopt_text) = get_cached_manopt(conn, &base_cmd) {
                insights.push((
                    "info".into(),
                    format!("Options for '{}':\n{}", base_cmd, manopt_text),
                ));
            }
        }
    }

    insights
}

/// Generate post-execution insights based on exit codes and output.
pub fn get_post_insights(
    command: &str,
    pipestatus: &[i32],
    output: &str,
) -> Vec<(String, String)> {
    let mut insights = Vec::new();

    if pipestatus.is_empty() {
        return insights;
    }

    let overall_exit = pipestatus[pipestatus.len() - 1];

    // Silent command detection
    if overall_exit == 0 && output.trim().is_empty() {
        insights.push(("info".into(), "No output produced.".into()));
    }

    // Command awareness
    let base_cmd = extract_base_command(command);
    let universal = universal_exit_codes();
    let known = known_exit_codes();

    if let Some(meaning) = universal.get(&overall_exit) {
        insights.push((
            "warning".into(),
            format!("{} (exit {})", meaning, overall_exit),
        ));
    } else if let Some(cmd_codes) = known.get(base_cmd.as_str()) {
        if let Some(meaning) = cmd_codes.get(&overall_exit) {
            insights.push((
                "info".into(),
                format!("{} exit {} = {} (normal)", base_cmd, overall_exit, meaning),
            ));
        }
    }

    // Pipe masking — left-side failures hidden by downstream
    if pipestatus.len() > 1 {
        for (i, &code) in pipestatus[..pipestatus.len() - 1].iter().enumerate() {
            if code != 0 && code != 141 {
                // 141=SIGPIPE, normal in pipes
                insights.push((
                    "warning".into(),
                    format!("pipe segment {} exited {} (masked by downstream)", i + 1, code),
                ));
            }
        }
    }

    insights
}

// --- Internal helpers ---

fn get_recent_exact(
    conn: &Connection,
    command_hash: &str,
    window_start: f64,
) -> (bool, i64, i64, i64) {
    let mut stmt = conn
        .prepare(
            "SELECT success FROM recent_commands
             WHERE command_hash = ? AND timestamp > ?
             ORDER BY timestamp DESC",
        )
        .unwrap_or_else(|_| {
            // Table might not exist yet
            conn.prepare("SELECT 0 WHERE 0").unwrap()
        });

    let rows: Vec<i64> = stmt
        .query_map(rusqlite::params![command_hash, window_start], |row| {
            row.get(0)
        })
        .map(|iter| iter.filter_map(|r| r.ok()).collect())
        .unwrap_or_default();

    let count = rows.len() as i64;
    let successes = rows.iter().filter(|&&s| s == 1).count() as i64;
    let failures = count - successes;

    (count > 0, count, successes, failures)
}

fn get_recent_similar(
    conn: &Connection,
    command_template: &str,
    command_hash: &str,
    window_start: f64,
) -> Vec<(String, bool)> {
    let mut stmt = match conn.prepare(
        "SELECT command_preview, success FROM recent_commands
         WHERE command_template = ? AND command_hash != ? AND timestamp > ?
         ORDER BY timestamp DESC LIMIT 5",
    ) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    stmt.query_map(
        rusqlite::params![command_template, command_hash, window_start],
        |row| {
            let preview: String = row.get(0)?;
            let success: i64 = row.get(1)?;
            Ok((preview, success == 1))
        },
    )
    .map(|iter| iter.filter_map(|r| r.ok()).collect())
    .unwrap_or_default()
}

fn get_streak(conn: &Connection, command_hash: &str) -> Option<(i64, i64, i64)> {
    conn.query_row(
        "SELECT current_streak, longest_success_streak, longest_fail_streak
         FROM streaks WHERE command_hash = ?",
        rusqlite::params![command_hash],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )
    .ok()
}

struct PatternStats {
    observations: i64,
    timeout_rate: f64,
    success_rate: f64,
    avg_duration_ms: Option<f64>,
}

fn get_pattern_stats(conn: &Connection, command_hash: &str) -> Option<PatternStats> {
    conn.query_row(
        "SELECT
            COUNT(*) as total,
            SUM(weight) as weighted_total,
            SUM(CASE WHEN timed_out = 1 THEN weight ELSE 0 END) as timeout_weight,
            SUM(CASE WHEN exit_code = 0 THEN weight ELSE 0 END) as success_weight,
            AVG(duration_ms) as avg_duration
         FROM observations
         WHERE command_hash = ?",
        rusqlite::params![command_hash],
        |row| {
            let total: i64 = row.get(0)?;
            if total == 0 {
                return Ok(None);
            }
            let weighted_total: f64 = row.get::<_, Option<f64>>(1)?.unwrap_or(0.0);
            let timeout_weight: f64 = row.get::<_, Option<f64>>(2)?.unwrap_or(0.0);
            let success_weight: f64 = row.get::<_, Option<f64>>(3)?.unwrap_or(0.0);
            let avg_dur: Option<f64> = row.get(4)?;
            let denom = if weighted_total > 0.0 {
                weighted_total
            } else {
                1.0
            };
            Ok(Some(PatternStats {
                observations: total,
                timeout_rate: timeout_weight / denom,
                success_rate: success_weight / denom,
                avg_duration_ms: avg_dur,
            }))
        },
    )
    .ok()
    .flatten()
}

fn get_template_fail_count(conn: &Connection, session_id: &str, template: &str) -> i64 {
    let rows: Vec<i64> = conn
        .prepare(
            "SELECT success FROM recent_commands
             WHERE session_id = ? AND command_template = ?
             ORDER BY timestamp DESC LIMIT 10",
        )
        .and_then(|mut stmt| {
            stmt.query_map(rusqlite::params![session_id, template], |row| row.get(0))
                .map(|iter| iter.filter_map(|r| r.ok()).collect())
        })
        .unwrap_or_default();

    let mut count: i64 = 0;
    for success in rows {
        if success == 1 {
            break;
        }
        count += 1;
    }
    count
}

fn get_cached_manopt(conn: &Connection, base_cmd: &str) -> Option<String> {
    conn.query_row(
        "SELECT options_text FROM manopt_cache WHERE base_command = ?",
        rusqlite::params![base_cmd],
        |row| row.get(0),
    )
    .ok()
}

/// Extract base command name from a command string.
/// "git status" -> "git", "/usr/bin/grep foo" -> "grep"
pub fn extract_base_command(command: &str) -> String {
    command
        .split_whitespace()
        .next()
        .unwrap_or("")
        .rsplit('/')
        .next()
        .unwrap_or("")
        .to_string()
}
