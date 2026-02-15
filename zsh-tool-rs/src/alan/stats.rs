//! ALAN statistics for MCP tool responses (zsh_alan_stats, zsh_alan_query).

use rusqlite::Connection;
use serde::Serialize;
use std::collections::HashMap;

use super::hash;

/// Overall ALAN database statistics.
#[derive(Debug, Serialize)]
pub struct AlanStats {
    pub total_observations: i64,
    pub unique_patterns: i64,
    pub total_weight: f64,
    pub oldest: Option<String>,
    pub newest: Option<String>,
    pub session: SessionStats,
    pub hot_patterns: Vec<HotPattern>,
}

#[derive(Debug, Serialize)]
pub struct SessionStats {
    pub session_id: String,
    pub total_commands: i64,
    pub successes: i64,
    pub failures: i64,
    pub success_rate: f64,
    pub timeouts: i64,
    pub retries: i64,
    pub avg_duration_ms: Option<f64>,
}

#[derive(Debug, Serialize)]
pub struct HotPattern {
    pub pattern: String,
    pub count: i64,
    pub success_rate: f64,
    pub avg_duration_ms: Option<f64>,
}

/// Get overall ALAN statistics.
pub fn get_stats(conn: &Connection, session_id: &str) -> AlanStats {
    let (total_obs, unique, total_weight, oldest, newest) = conn
        .query_row(
            "SELECT
                COUNT(*) as total_observations,
                COUNT(DISTINCT command_hash) as unique_patterns,
                SUM(weight) as total_weight,
                MIN(created_at) as oldest,
                MAX(created_at) as newest
             FROM observations",
            [],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            },
        )
        .unwrap_or((0, 0, 0.0, None, None));

    AlanStats {
        total_observations: total_obs,
        unique_patterns: unique,
        total_weight,
        oldest,
        newest,
        session: get_session_stats(conn, session_id),
        hot_patterns: get_hot_patterns(conn, session_id, 5),
    }
}

/// Get session statistics.
pub fn get_session_stats(conn: &Connection, session_id: &str) -> SessionStats {
    let (total, successes, timeouts, avg_dur) = conn
        .query_row(
            "SELECT
                COUNT(*) as total,
                SUM(success) as successes,
                SUM(timed_out) as timeouts,
                AVG(duration_ms) as avg_duration
             FROM recent_commands WHERE session_id = ?",
            rusqlite::params![session_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<i64>>(1)?.unwrap_or(0),
                    row.get::<_, Option<i64>>(2)?.unwrap_or(0),
                    row.get::<_, Option<f64>>(3)?,
                ))
            },
        )
        .unwrap_or((0, 0, 0, None));

    let retries: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM (
                SELECT command_hash, COUNT(*) as cnt
                FROM recent_commands WHERE session_id = ?
                GROUP BY command_hash HAVING cnt > 1
             )",
            rusqlite::params![session_id],
            |row| row.get(0),
        )
        .unwrap_or(0);

    let failures = total - successes;

    SessionStats {
        session_id: session_id.to_string(),
        total_commands: total,
        successes,
        failures,
        success_rate: if total > 0 {
            successes as f64 / total as f64
        } else {
            0.0
        },
        timeouts,
        retries,
        avg_duration_ms: avg_dur,
    }
}

/// Get most frequently used patterns in current session.
pub fn get_hot_patterns(conn: &Connection, session_id: &str, limit: i64) -> Vec<HotPattern> {
    let mut stmt = match conn.prepare(
        "SELECT
            command_template,
            COUNT(*) as count,
            SUM(success) as successes,
            AVG(duration_ms) as avg_duration
         FROM recent_commands WHERE session_id = ?
         GROUP BY command_template
         ORDER BY count DESC LIMIT ?",
    ) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    stmt.query_map(rusqlite::params![session_id, limit], |row| {
        let count: i64 = row.get(1)?;
        let successes: i64 = row.get::<_, Option<i64>>(2)?.unwrap_or(0);
        Ok(HotPattern {
            pattern: row.get(0)?,
            count,
            success_rate: if count > 0 {
                successes as f64 / count as f64
            } else {
                0.0
            },
            avg_duration_ms: row.get(3)?,
        })
    })
    .map(|iter| iter.filter_map(|r| r.ok()).collect())
    .unwrap_or_default()
}

/// Pattern stats for zsh_alan_query tool.
#[derive(Debug, Serialize)]
pub struct PatternQueryResult {
    pub known: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observations: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub success_rate: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout_rate: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub avg_duration_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub streak: Option<HashMap<String, serde_json::Value>>,
}

/// Query pattern stats for a command (zsh_alan_query tool).
pub fn query_pattern(conn: &Connection, command: &str) -> PatternQueryResult {
    let command_hash = hash::hash_command(command);

    let row = conn.query_row(
        "SELECT
            COUNT(*) as total,
            SUM(weight) as weighted_total,
            SUM(CASE WHEN timed_out = 1 THEN weight ELSE 0 END) as timeout_weight,
            SUM(CASE WHEN exit_code = 0 THEN weight ELSE 0 END) as success_weight,
            AVG(duration_ms) as avg_duration
         FROM observations WHERE command_hash = ?",
        rusqlite::params![command_hash],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                row.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                row.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                row.get::<_, Option<f64>>(4)?,
            ))
        },
    );

    match row {
        Ok((total, weighted_total, timeout_weight, success_weight, avg_dur)) if total > 0 => {
            let denom = if weighted_total > 0.0 {
                weighted_total
            } else {
                1.0
            };

            // Get streak
            let streak = conn
                .query_row(
                    "SELECT current_streak, longest_success_streak, longest_fail_streak
                     FROM streaks WHERE command_hash = ?",
                    rusqlite::params![command_hash],
                    |row| {
                        let mut map = HashMap::new();
                        map.insert(
                            "current".to_string(),
                            serde_json::Value::Number(
                                serde_json::Number::from(row.get::<_, i64>(0)?),
                            ),
                        );
                        map.insert(
                            "longest_success".to_string(),
                            serde_json::Value::Number(
                                serde_json::Number::from(row.get::<_, i64>(1)?),
                            ),
                        );
                        map.insert(
                            "longest_fail".to_string(),
                            serde_json::Value::Number(
                                serde_json::Number::from(row.get::<_, i64>(2)?),
                            ),
                        );
                        Ok(map)
                    },
                )
                .ok();

            PatternQueryResult {
                known: true,
                observations: Some(total),
                success_rate: Some(success_weight / denom),
                timeout_rate: Some(timeout_weight / denom),
                avg_duration_ms: avg_dur,
                streak,
            }
        }
        _ => PatternQueryResult {
            known: false,
            observations: None,
            success_rate: None,
            timeout_rate: None,
            avg_duration_ms: None,
            streak: None,
        },
    }
}
