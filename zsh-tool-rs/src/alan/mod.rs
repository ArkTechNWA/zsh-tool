use rusqlite::Connection;
use std::path::Path;

pub mod hash;
pub mod insights;
pub mod manopt;
pub mod pipeline;
pub mod prune;
pub mod ssh;
pub mod stats;
pub mod streak;

/// Open (or create) the ALAN database and ensure schema exists.
pub fn open_db(db_path: &str) -> Result<Connection, String> {
    let path = Path::new(db_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("mkdir: {}", e))?;
    }
    let conn = Connection::open(db_path).map_err(|e| format!("open db: {}", e))?;
    init_schema(&conn)?;
    Ok(conn)
}

pub fn init_schema(conn: &Connection) -> Result<(), String> {
    conn.execute_batch(
        "
        -- Original observations table (pattern learning)
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            command_hash TEXT NOT NULL,
            command_template TEXT,
            command_preview TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            timed_out INTEGER DEFAULT 0,
            output_snippet TEXT,
            error_snippet TEXT,
            weight REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            last_accessed TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_command_hash ON observations(command_hash);
        CREATE INDEX IF NOT EXISTS idx_created_at ON observations(created_at);
        CREATE INDEX IF NOT EXISTS idx_weight ON observations(weight);
        CREATE INDEX IF NOT EXISTS idx_command_template ON observations(command_template);

        -- Recent commands (hot cache for retry/streak detection)
        CREATE TABLE IF NOT EXISTS recent_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            command_hash TEXT NOT NULL,
            command_template TEXT,
            command_preview TEXT,
            timestamp REAL NOT NULL,
            duration_ms INTEGER,
            exit_code INTEGER,
            timed_out INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_recent_session ON recent_commands(session_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_recent_hash ON recent_commands(command_hash, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_recent_template ON recent_commands(command_template, timestamp DESC);

        -- Streak tracking per pattern
        CREATE TABLE IF NOT EXISTS streaks (
            command_hash TEXT PRIMARY KEY,
            current_streak INTEGER DEFAULT 0,
            longest_success_streak INTEGER DEFAULT 0,
            longest_fail_streak INTEGER DEFAULT 0,
            last_result INTEGER,
            last_updated REAL
        );

        -- Metadata
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- SSH-specific observations
        -- TODO(phase3): port SSH recording to Rust
        CREATE TABLE IF NOT EXISTS ssh_observations (
            id TEXT PRIMARY KEY,
            observation_id TEXT,
            host TEXT NOT NULL,
            remote_command TEXT,
            remote_command_template TEXT,
            exit_code INTEGER,
            exit_type TEXT,
            duration_ms INTEGER,
            timed_out INTEGER DEFAULT 0,
            weight REAL DEFAULT 1.0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ssh_host ON ssh_observations(host);
        CREATE INDEX IF NOT EXISTS idx_ssh_remote_template ON ssh_observations(remote_command_template);
        CREATE INDEX IF NOT EXISTS idx_ssh_exit_type ON ssh_observations(exit_type);

        -- manopt cache
        -- TODO(phase3): port manopt caching to Rust
        CREATE TABLE IF NOT EXISTS manopt_cache (
            base_command TEXT PRIMARY KEY,
            options_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ",
    )
    .map_err(|e| format!("schema: {}", e))
}

/// Record a command execution in the ALAN database.
///
/// This is the core write path — observations, recent_commands, streaks,
/// and pipeline segments. SSH recording and manopt stay in Python.
/// TODO(phase3): port SSH recording and manopt triggering to Rust.
pub fn record(
    conn: &Connection,
    session_id: &str,
    command: &str,
    exit_code: i32,
    duration_ms: u64,
    timed_out: bool,
    stdout_snippet: &str,
    pipestatus: &[i32],
) -> Result<(), String> {
    let command_hash = hash::hash_command(command);
    let command_template = hash::template_command(command);
    let success: i32 = if exit_code == 0 && !timed_out { 1 } else { 0 };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let now_iso = chrono::Utc::now().to_rfc3339();
    let observation_id = uuid::Uuid::new_v4().to_string();

    let preview_len = command.len().min(200);
    let command_preview = &command[..preview_len];

    // Record in observations (long-term learning)
    conn.execute(
        "INSERT INTO observations
         (id, command_hash, command_template, command_preview, exit_code,
          duration_ms, timed_out, output_snippet, error_snippet, weight, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, NULL, 1.0, ?9)",
        rusqlite::params![
            observation_id,
            command_hash,
            command_template,
            command_preview,
            exit_code,
            duration_ms as i64,
            if timed_out { 1 } else { 0 },
            if stdout_snippet.is_empty() {
                None
            } else {
                Some(&stdout_snippet[..stdout_snippet.len().min(500)])
            },
            now_iso,
        ],
    )
    .map_err(|e| format!("insert observation: {}", e))?;

    // Record in recent_commands (hot cache)
    conn.execute(
        "INSERT INTO recent_commands
         (session_id, command_hash, command_template, command_preview,
          timestamp, duration_ms, exit_code, timed_out, success)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        rusqlite::params![
            session_id,
            command_hash,
            command_template,
            command_preview,
            now,
            duration_ms as i64,
            exit_code,
            if timed_out { 1 } else { 0 },
            success,
        ],
    )
    .map_err(|e| format!("insert recent: {}", e))?;

    // Update streak
    streak::update_streak(conn, &command_hash, success, now)?;

    // SSH-specific dual recording
    ssh::record_ssh(conn, &observation_id, command, exit_code, duration_ms, timed_out)?;

    // Pipeline segment recording (if multi-pipe)
    if pipestatus.len() > 1 {
        let segments = pipeline::parse_pipeline(command);
        if segments.len() == pipestatus.len() {
            for (seg, &seg_exit) in segments.iter().zip(pipestatus.iter()) {
                let seg = seg.trim();
                if seg.is_empty() {
                    continue;
                }
                let seg_hash = hash::hash_command(seg);
                let seg_template = hash::template_command(seg);
                let seg_success: i32 = if seg_exit == 0 { 1 } else { 0 };
                let seg_obs_id = uuid::Uuid::new_v4().to_string();
                let seg_preview_len = seg.len().min(200);

                conn.execute(
                    "INSERT INTO observations
                     (id, command_hash, command_template, command_preview, exit_code,
                      duration_ms, timed_out, output_snippet, error_snippet, weight, created_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, 0, 0, NULL, NULL, 1.0, ?6)",
                    rusqlite::params![
                        seg_obs_id,
                        seg_hash,
                        seg_template,
                        &seg[..seg_preview_len],
                        seg_exit,
                        now_iso,
                    ],
                )
                .map_err(|e| format!("insert seg observation: {}", e))?;

                conn.execute(
                    "INSERT INTO recent_commands
                     (session_id, command_hash, command_template, command_preview,
                      timestamp, duration_ms, exit_code, timed_out, success)
                     VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, 0, ?7)",
                    rusqlite::params![
                        session_id,
                        seg_hash,
                        seg_template,
                        &seg[..seg_preview_len],
                        now,
                        seg_exit,
                        seg_success,
                    ],
                )
                .map_err(|e| format!("insert seg recent: {}", e))?;

                streak::update_streak(conn, &seg_hash, seg_success, now)?;
            }
        }
    }

    // Prune old recent commands (keep 10x the window = 100 minutes)
    let cutoff = now - (10.0 * 60.0 * 10.0);
    conn.execute(
        "DELETE FROM recent_commands WHERE timestamp < ?1",
        rusqlite::params![cutoff],
    )
    .map_err(|e| format!("prune: {}", e))?;

    Ok(())
}
