//! SSH command parsing, classification, recording, and insights.

use rusqlite::Connection;

use super::hash;

/// Parsed SSH command info.
pub struct SshInfo {
    pub host: String,
    pub remote_command: Option<String>,
    pub user: Option<String>,
    pub port: Option<String>,
}

/// Parse an SSH command to extract host, remote command, user, port.
/// Returns None if not an SSH command.
pub fn parse_ssh_command(command: &str) -> Option<SshInfo> {
    let normalized: String = command.split_whitespace().collect::<Vec<_>>().join(" ");
    let parts: Vec<&str> = normalized.split(' ').collect();

    if parts.is_empty() || parts[0] != "ssh" {
        return None;
    }

    let mut host: Option<String> = None;
    let mut remote_command: Option<String> = None;
    let mut user: Option<String> = None;
    let mut port: Option<String> = None;

    let mut i = 1;
    while i < parts.len() {
        let part = parts[i];

        // Flags that take an argument
        match part {
            "-p" | "-l" | "-i" | "-o" | "-F" | "-J" | "-W" => {
                if part == "-p" && i + 1 < parts.len() {
                    port = Some(parts[i + 1].to_string());
                } else if part == "-l" && i + 1 < parts.len() {
                    user = Some(parts[i + 1].to_string());
                }
                i += 2;
                continue;
            }
            _ if part.starts_with('-') => {
                i += 1;
                continue;
            }
            _ => {}
        }

        // This should be the host (possibly user@host)
        if host.is_none() {
            if let Some(at_pos) = part.rfind('@') {
                user = Some(part[..at_pos].to_string());
                host = Some(part[at_pos + 1..].to_string());
            } else {
                host = Some(part.to_string());
            }
            i += 1;

            // Everything after host is the remote command
            if i < parts.len() {
                remote_command = Some(parts[i..].join(" "));
            }
            break;
        } else {
            i += 1;
        }
    }

    host.map(|h| SshInfo {
        host: h,
        remote_command,
        user,
        port,
    })
}

/// Classify SSH exit code into failure type.
pub fn classify_ssh_exit(exit_code: i32) -> &'static str {
    match exit_code {
        0 => "success",
        255 => "connection_failed",
        1..=254 => "command_failed",
        _ => "unknown",
    }
}

/// Record SSH-specific observation.
pub fn record_ssh(
    conn: &Connection,
    observation_id: &str,
    command: &str,
    exit_code: i32,
    duration_ms: u64,
    timed_out: bool,
) -> Result<(), String> {
    let ssh_info = match parse_ssh_command(command) {
        Some(info) => info,
        None => return Ok(()), // Not an SSH command, nothing to record
    };

    let exit_type = classify_ssh_exit(exit_code);
    let remote_template = ssh_info
        .remote_command
        .as_deref()
        .map(hash::template_command);
    let remote_preview = ssh_info
        .remote_command
        .as_deref()
        .map(|c| &c[..c.len().min(200)]);

    let ssh_id = uuid::Uuid::new_v4().to_string();
    let now_iso = chrono::Utc::now().to_rfc3339();

    conn.execute(
        "INSERT INTO ssh_observations
         (id, observation_id, host, remote_command, remote_command_template,
          exit_code, exit_type, duration_ms, timed_out, weight, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 1.0, ?10)",
        rusqlite::params![
            ssh_id,
            observation_id,
            ssh_info.host,
            remote_preview,
            remote_template,
            exit_code,
            exit_type,
            duration_ms as i64,
            if timed_out { 1 } else { 0 },
            now_iso,
        ],
    )
    .map_err(|e| format!("insert ssh observation: {}", e))?;

    Ok(())
}

/// Generate SSH-specific insights for a command.
pub fn get_ssh_insights(conn: &Connection, command: &str) -> Vec<(String, String)> {
    let ssh_info = match parse_ssh_command(command) {
        Some(info) => info,
        None => return Vec::new(),
    };

    let mut insights = Vec::new();
    let host = &ssh_info.host;

    // Host connectivity stats
    if let Ok(row) = conn.query_row(
        "SELECT
            COUNT(*) as total,
            SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
            SUM(CASE WHEN exit_type = 'connection_failed' THEN 1 ELSE 0 END) as conn_failures
         FROM ssh_observations WHERE host = ?",
        rusqlite::params![host],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
            ))
        },
    ) {
        let (total, successes, conn_failures) = row;
        if total > 0 {
            let conn_fail_rate = conn_failures as f64 / total as f64;
            if conn_fail_rate > 0.3 {
                insights.push((
                    "warning".into(),
                    format!(
                        "Host '{}' has {:.0}% connection failure rate ({}/{}).",
                        host,
                        conn_fail_rate * 100.0,
                        conn_failures,
                        total
                    ),
                ));
            } else if successes == total && total >= 3 {
                insights.push((
                    "info".into(),
                    format!(
                        "Host '{}' is reliable: {} successful connections.",
                        host, total
                    ),
                ));
            }
        }
    }

    // Remote command stats
    if let Some(ref remote_cmd) = ssh_info.remote_command {
        let remote_template = hash::template_command(remote_cmd);
        if !remote_template.is_empty() {
            if let Ok(row) = conn.query_row(
                "SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN exit_type = 'command_failed' THEN 1 ELSE 0 END) as cmd_failures,
                    GROUP_CONCAT(DISTINCT host) as hosts
                 FROM ssh_observations WHERE remote_command_template = ?",
                rusqlite::params![remote_template],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                },
            ) {
                let (total, successes, cmd_failures, hosts_str) = row;
                if total > 0 {
                    let success_rate = successes as f64 / total as f64;
                    let host_count = hosts_str
                        .as_deref()
                        .map(|s| s.split(',').count())
                        .unwrap_or(0);

                    if cmd_failures > 0 && success_rate < 0.5 {
                        insights.push((
                            "warning".into(),
                            format!(
                                "Remote command '{}' fails often ({}/{} across {} hosts).",
                                remote_template, cmd_failures, total, host_count
                            ),
                        ));
                    } else if success_rate > 0.9 && total >= 3 {
                        insights.push((
                            "info".into(),
                            format!(
                                "Remote command '{}' reliable across {} hosts ({:.0}% success).",
                                remote_template,
                                host_count,
                                success_rate * 100.0
                            ),
                        ));
                    }
                }
            }
        }
    }

    insights
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_simple_ssh() {
        let info = parse_ssh_command("ssh myhost").unwrap();
        assert_eq!(info.host, "myhost");
        assert!(info.remote_command.is_none());
    }

    #[test]
    fn test_parse_ssh_with_user() {
        let info = parse_ssh_command("ssh user@host").unwrap();
        assert_eq!(info.host, "host");
        assert_eq!(info.user.unwrap(), "user");
    }

    #[test]
    fn test_parse_ssh_with_remote_command() {
        let info = parse_ssh_command("ssh host ls -la /tmp").unwrap();
        assert_eq!(info.host, "host");
        assert_eq!(info.remote_command.unwrap(), "ls -la /tmp");
    }

    #[test]
    fn test_parse_ssh_with_port() {
        let info = parse_ssh_command("ssh -p 2222 host").unwrap();
        assert_eq!(info.host, "host");
        assert_eq!(info.port.unwrap(), "2222");
    }

    #[test]
    fn test_parse_not_ssh() {
        assert!(parse_ssh_command("ls -la").is_none());
    }

    #[test]
    fn test_classify_exit() {
        assert_eq!(classify_ssh_exit(0), "success");
        assert_eq!(classify_ssh_exit(1), "command_failed");
        assert_eq!(classify_ssh_exit(255), "connection_failed");
    }
}
