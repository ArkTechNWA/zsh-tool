use rusqlite::Connection;

/// Update streak tracking for a command pattern.
///
/// Matches Python's `_update_streak()`:
/// - Success continues positive streak, failure continues negative streak
/// - Different result resets streak to 1 or -1
/// - Tracks longest success and failure streaks
pub fn update_streak(
    conn: &Connection,
    command_hash: &str,
    success: i32,
    now: f64,
) -> Result<(), String> {
    let existing: Option<(i64, i64, i64, i64)> = conn
        .query_row(
            "SELECT current_streak, longest_success_streak, longest_fail_streak, last_result
             FROM streaks WHERE command_hash = ?1",
            rusqlite::params![command_hash],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .ok();

    if let Some((current, longest_success, longest_fail, last_result)) = existing {
        let (new_current, new_longest_success, new_longest_fail) =
            if success as i64 == last_result {
                // Continue streak
                if success != 0 {
                    let c = current + 1;
                    (c, longest_success.max(c), longest_fail)
                } else {
                    let c = current - 1;
                    (c, longest_success, longest_fail.max(c.unsigned_abs() as i64))
                }
            } else {
                // Streak broken
                let c = if success != 0 { 1i64 } else { -1i64 };
                (c, longest_success, longest_fail)
            };

        conn.execute(
            "UPDATE streaks
             SET current_streak = ?1, longest_success_streak = ?2, longest_fail_streak = ?3,
                 last_result = ?4, last_updated = ?5
             WHERE command_hash = ?6",
            rusqlite::params![
                new_current,
                new_longest_success,
                new_longest_fail,
                success,
                now,
                command_hash,
            ],
        )
        .map_err(|e| format!("update streak: {}", e))?;
    } else {
        // New entry
        let initial_streak: i64 = if success != 0 { 1 } else { -1 };
        let initial_success: i64 = if success != 0 { 1 } else { 0 };
        let initial_fail: i64 = if success != 0 { 0 } else { 1 };

        conn.execute(
            "INSERT INTO streaks
             (command_hash, current_streak, longest_success_streak,
              longest_fail_streak, last_result, last_updated)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                command_hash,
                initial_streak,
                initial_success,
                initial_fail,
                success,
                now,
            ],
        )
        .map_err(|e| format!("insert streak: {}", e))?;
    }

    Ok(())
}
