//! Temporal decay and pruning for ALAN observations.

use rusqlite::Connection;

/// Apply temporal decay to all observation weights.
/// Uses exponential half-life decay: weight * 0.5^(age_hours / half_life_hours)
pub fn apply_decay(conn: &Connection, half_life_hours: u64, prune_threshold: f64) {
    let _ = conn.execute(
        "UPDATE observations
         SET weight = weight * POWER(0.5,
             (JULIANDAY('now') - JULIANDAY(created_at)) * 24 / ?1
         )
         WHERE weight > ?2",
        rusqlite::params![half_life_hours as f64, prune_threshold],
    );

    // Also decay SSH observations
    let _ = conn.execute(
        "UPDATE ssh_observations
         SET weight = weight * POWER(0.5,
             (JULIANDAY('now') - JULIANDAY(created_at)) * 24 / ?1
         )
         WHERE weight > ?2",
        rusqlite::params![half_life_hours as f64, prune_threshold],
    );
}

/// Remove decayed entries and enforce max entry limit.
pub fn prune(
    conn: &Connection,
    half_life_hours: u64,
    prune_threshold: f64,
    max_entries: usize,
) {
    apply_decay(conn, half_life_hours, prune_threshold);

    // Remove low-weight observations
    let _ = conn.execute(
        "DELETE FROM observations WHERE weight < ?1",
        rusqlite::params![prune_threshold],
    );

    // Enforce max entries
    let _ = conn.execute(
        "DELETE FROM observations
         WHERE id NOT IN (
             SELECT id FROM observations ORDER BY weight DESC LIMIT ?1
         )",
        rusqlite::params![max_entries as i64],
    );

    // Remove low-weight SSH observations
    let _ = conn.execute(
        "DELETE FROM ssh_observations WHERE weight < ?1",
        rusqlite::params![prune_threshold],
    );

    // Clean orphaned SSH observations (linked observation was deleted)
    let _ = conn.execute(
        "DELETE FROM ssh_observations
         WHERE observation_id NOT IN (SELECT id FROM observations)",
        [],
    );

    // Record prune timestamp
    let now_iso = chrono::Utc::now().to_rfc3339();
    let _ = conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_prune', ?1)",
        rusqlite::params![now_iso],
    );
}

/// Prune only if enough time has passed since last prune.
pub fn maybe_prune(
    conn: &Connection,
    half_life_hours: u64,
    prune_threshold: f64,
    max_entries: usize,
    prune_interval_hours: u64,
) {
    let should_prune = conn
        .query_row(
            "SELECT value FROM meta WHERE key = 'last_prune'",
            [],
            |row| row.get::<_, String>(0),
        )
        .ok()
        .map(|last_prune_str| {
            chrono::DateTime::parse_from_rfc3339(&last_prune_str)
                .map(|dt| {
                    let elapsed = chrono::Utc::now().signed_duration_since(dt);
                    elapsed.num_hours() >= prune_interval_hours as i64
                })
                .unwrap_or(true) // Can't parse -> prune
        })
        .unwrap_or(true); // No record -> prune

    if should_prune {
        prune(conn, half_life_hours, prune_threshold, max_entries);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alan;

    fn fresh_db() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        alan::init_schema(&conn).unwrap();
        conn
    }

    #[test]
    fn test_prune_removes_low_weight() {
        let conn = fresh_db();

        // Insert observation with very low weight
        conn.execute(
            "INSERT INTO observations (id, command_hash, command_template, command_preview,
             exit_code, duration_ms, weight, created_at)
             VALUES ('old1', 'hash1', 'tpl1', 'echo old', 0, 100, 0.001, '2020-01-01T00:00:00Z')",
            [],
        )
        .unwrap();

        // Insert one with good weight
        conn.execute(
            "INSERT INTO observations (id, command_hash, command_template, command_preview,
             exit_code, duration_ms, weight, created_at)
             VALUES ('new1', 'hash2', 'tpl2', 'echo new', 0, 100, 1.0, datetime('now'))",
            [],
        )
        .unwrap();

        prune(&conn, 24, 0.01, 10000);

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM observations", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1); // Only the good one survives
    }

    #[test]
    fn test_prune_enforces_max_entries() {
        let conn = fresh_db();

        // Insert 5 observations
        for i in 0..5 {
            conn.execute(
                "INSERT INTO observations (id, command_hash, command_template, command_preview,
                 exit_code, duration_ms, weight, created_at)
                 VALUES (?1, ?2, ?3, 'echo test', 0, 100, 1.0, datetime('now'))",
                rusqlite::params![
                    format!("id{}", i),
                    format!("hash{}", i),
                    format!("tpl{}", i)
                ],
            )
            .unwrap();
        }

        // Prune with max 3
        prune(&conn, 24, 0.01, 3);

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM observations", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 3);
    }

    #[test]
    fn test_prune_cleans_orphaned_ssh() {
        let conn = fresh_db();

        // Insert observation + linked SSH observation
        conn.execute(
            "INSERT INTO observations (id, command_hash, command_template, command_preview,
             exit_code, duration_ms, weight, created_at)
             VALUES ('obs1', 'hash1', 'tpl1', 'ssh host', 0, 100, 0.001, '2020-01-01T00:00:00Z')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO ssh_observations (id, observation_id, host, exit_code, exit_type,
             duration_ms, weight, created_at)
             VALUES ('ssh1', 'obs1', 'host', 0, 'success', 100, 0.001, '2020-01-01T00:00:00Z')",
            [],
        )
        .unwrap();

        prune(&conn, 24, 0.01, 10000);

        // Both should be gone (obs pruned by weight, ssh orphaned)
        let obs_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM observations", [], |row| row.get(0))
            .unwrap();
        let ssh_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM ssh_observations", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(obs_count, 0);
        assert_eq!(ssh_count, 0);
    }

    #[test]
    fn test_maybe_prune_skips_if_recent() {
        let conn = fresh_db();

        // Record a recent prune
        let now_iso = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_prune', ?1)",
            rusqlite::params![now_iso],
        )
        .unwrap();

        // Insert low-weight observation
        conn.execute(
            "INSERT INTO observations (id, command_hash, command_template, command_preview,
             exit_code, duration_ms, weight, created_at)
             VALUES ('old1', 'hash1', 'tpl1', 'echo', 0, 100, 0.001, '2020-01-01T00:00:00Z')",
            [],
        )
        .unwrap();

        maybe_prune(&conn, 24, 0.01, 10000, 6);

        // Should NOT have pruned (interval not elapsed)
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM observations", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }
}
