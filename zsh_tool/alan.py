"""
A.L.A.N. 2.0 - As Long As Necessary.

Short-term learning database with temporal decay, retry detection,
streak tracking, and proactive insights.

"Maybe you're fuckin' up, maybe you're doing it right."
"""

import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import (
    ALAN_DECAY_HALF_LIFE_HOURS,
    ALAN_PRUNE_THRESHOLD,
    ALAN_PRUNE_INTERVAL_HOURS,
    ALAN_MAX_ENTRIES,
    ALAN_RECENT_WINDOW_MINUTES,
    ALAN_STREAK_THRESHOLD,
    PIPESTATUS_MARKER,
)


def _wrap_for_pipestatus(command: str) -> str:
    """Wrap command to capture pipestatus array from zsh."""
    return f'{command}; echo "{PIPESTATUS_MARKER}:${{pipestatus[*]}}"'


def _extract_pipestatus(output: str) -> tuple[str, list[int] | None]:
    """Extract and strip pipestatus marker from output.

    Returns (clean_output, pipestatus_list).
    If marker not found, returns (output, None).
    """
    marker_pattern = f"{PIPESTATUS_MARKER}:"
    marker_pos = output.rfind(marker_pattern)

    if marker_pos == -1:
        return output, None

    # Find the end of the marker line
    line_end = output.find('\n', marker_pos)
    if line_end == -1:
        line_end = len(output)

    # Extract pipestatus values
    pipestatus_str = output[marker_pos + len(marker_pattern):line_end].strip()

    try:
        pipestatus = [int(x) for x in pipestatus_str.split()]
    except ValueError:
        # Couldn't parse pipestatus, return original output
        return output, None

    # Strip the marker line from output
    clean_output = output[:marker_pos] + output[line_end + 1:] if line_end < len(output) else output[:marker_pos]

    return clean_output, pipestatus


# --- Command awareness (bounded) ---

KNOWN_EXIT_CODES: dict[str, dict[int, str]] = {
    "grep":  {1: "no match"},
    "diff":  {1: "files differ"},
    "test":  {1: "condition false"},
    "[":     {1: "condition false"},
    "cmp":   {1: "files differ"},
}

UNIVERSAL_EXIT_CODES: dict[int, str] = {
    126: "permission denied",
    127: "command not found",
    255: "SSH connection failed",
}


def get_post_insights(
    command: str,
    pipestatus: list[int] | None,
    output: str = "",
) -> list[tuple[str, str]]:
    """Generate post-execution insights based on exit codes and output.

    Handles:
    - Silent command detection (no output + exit 0)
    - Command-specific exit code meanings (grep 1 = no match)
    - Universal exit code meanings (127 = command not found)
    - Pipe segment failure masking (left-side failures hidden by right-side success)

    Returns list of (level, message) tuples.
    """
    insights: list[tuple[str, str]] = []

    if not pipestatus:
        return insights

    overall_exit = pipestatus[-1]

    # Silent command detection
    if overall_exit == 0 and not output.strip():
        insights.append(("info", "No output produced."))

    # Command awareness — universal exit codes
    base_cmd = command.split()[0].split("/")[-1] if command.strip() else ""

    if overall_exit in UNIVERSAL_EXIT_CODES:
        insights.append(("warning", f"{UNIVERSAL_EXIT_CODES[overall_exit]} (exit {overall_exit})"))
    elif base_cmd in KNOWN_EXIT_CODES and overall_exit in KNOWN_EXIT_CODES[base_cmd]:
        meaning = KNOWN_EXIT_CODES[base_cmd][overall_exit]
        insights.append(("info", f"{base_cmd} exit {overall_exit} = {meaning} (normal)"))

    # Pipe masking — left-side failures hidden by downstream
    if len(pipestatus) > 1:
        for i, code in enumerate(pipestatus[:-1]):
            if code != 0 and code not in (141,):  # 141=SIGPIPE, normal in pipes
                insights.append(("warning", f"pipe segment {i + 1} exited {code} (masked by downstream)"))

    return insights


class ALAN:
    """
    Short-term learning database with temporal decay, retry detection,
    streak tracking, and proactive insights.

    A.L.A.N. 2.0: Now with personality.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid.uuid4())[:8]  # Unique per server instance
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            # Create tables
            conn.executescript("""
                -- Original observations table (pattern learning)
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    command_hash TEXT NOT NULL,
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

                -- SSH-specific observations (Issue #2: separate host and command tracking)
                CREATE TABLE IF NOT EXISTS ssh_observations (
                    id TEXT PRIMARY KEY,
                    observation_id TEXT,  -- Links to main observations table
                    host TEXT NOT NULL,
                    remote_command TEXT,
                    remote_command_template TEXT,
                    exit_code INTEGER,
                    exit_type TEXT,  -- 'success', 'connection_failed', 'command_failed'
                    duration_ms INTEGER,
                    timed_out INTEGER DEFAULT 0,
                    weight REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ssh_host ON ssh_observations(host);
                CREATE INDEX IF NOT EXISTS idx_ssh_remote_template ON ssh_observations(remote_command_template);
                CREATE INDEX IF NOT EXISTS idx_ssh_exit_type ON ssh_observations(exit_type);
            """)

            # A.L.A.N. 2.0 migrations - add columns if missing
            self._migrate_add_column(conn, 'observations', 'command_template', 'TEXT')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_command_template ON observations(command_template)")

    def _migrate_add_column(self, conn, table: str, column: str, col_type: str):
        """Add column to table if it doesn't exist (SQLite migration helper)."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _hash_command(self, command: str) -> str:
        """Create an exact hash for command pattern matching."""
        normalized = re.sub(r'\s+', ' ', command.strip())
        normalized = re.sub(r'"[^"]*"', '""', normalized)
        normalized = re.sub(r"'[^']*'", "''", normalized)
        normalized = re.sub(r'\b\d+\b', 'N', normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _template_command(self, command: str) -> str:
        """
        Create a fuzzy template for similarity matching.
        'git push origin feature-1' -> 'git push origin *'
        """
        normalized = re.sub(r'\s+', ' ', command.strip())
        parts = normalized.split()
        if not parts:
            return ""

        # Keep first 1-3 tokens as-is (the command), replace rest with wildcards
        # Heuristic: common commands have 1-2 word base
        base_commands = {
            'git', 'npm', 'yarn', 'pip', 'docker', 'kubectl', 'make',
            'cargo', 'go', 'python', 'node', 'ruby', 'curl', 'wget',
            'cat', 'grep', 'find', 'ls', 'cd', 'rm', 'cp', 'mv', 'mkdir',
            'systemctl', 'journalctl', 'apt', 'pacman', 'brew'
        }

        template_parts = []
        found_base = False
        for i, part in enumerate(parts):
            if not found_base:
                template_parts.append(part)
                # Check if this or next is a subcommand
                if part.lower() in base_commands:
                    # Include next part if it looks like subcommand
                    if i + 1 < len(parts) and not parts[i+1].startswith('-'):
                        continue
                    found_base = True
                elif i >= 1:  # After 2 parts, assume we have base
                    found_base = True
            else:
                # Replace arguments with wildcards, keep flags
                if part.startswith('-'):
                    template_parts.append(part)
                else:
                    if template_parts[-1] != '*':
                        template_parts.append('*')

        return ' '.join(template_parts)

    def _parse_ssh_command(self, command: str) -> Optional[dict]:
        """
        Parse SSH command to extract host and remote command.

        Returns dict with:
          - host: the target hostname/IP
          - remote_command: the command run on remote (if any)
          - user: username (if specified)
          - port: port (if specified)

        Returns None if not an SSH command.
        """
        normalized = re.sub(r'\s+', ' ', command.strip())
        parts = normalized.split()

        if not parts or parts[0] != 'ssh':
            return None

        result = {'host': None, 'remote_command': None, 'user': None, 'port': None}

        i = 1
        while i < len(parts):
            part = parts[i]

            # Handle common SSH flags
            if part in ('-p', '-l', '-i', '-o', '-F', '-J', '-W'):
                # These take an argument
                i += 2
                if part == '-p' and i - 1 < len(parts):
                    result['port'] = parts[i - 1]
                elif part == '-l' and i - 1 < len(parts):
                    result['user'] = parts[i - 1]
                continue
            elif part.startswith('-'):
                # Flags without arguments (or combined like -vvv)
                i += 1
                continue

            # This should be the host (possibly user@host)
            if result['host'] is None:
                if '@' in part:
                    result['user'], result['host'] = part.rsplit('@', 1)
                else:
                    result['host'] = part
                i += 1

                # Everything after host is the remote command
                if i < len(parts):
                    result['remote_command'] = ' '.join(parts[i:])
                break
            else:
                i += 1

        return result if result['host'] else None

    def _parse_pipeline(self, command: str) -> list[str]:
        """Split command on unquoted pipe characters.

        Handles:
        - Simple pipes: cmd1 | cmd2
        - Quoted pipes: echo "a|b" | grep a (the | in quotes is NOT a delimiter)
        - Escaped pipes: echo a\\|b | grep a

        Returns list of pipeline segment strings.
        """
        segments = []
        current = []
        i = 0
        in_single_quote = False
        in_double_quote = False
        escape_next = False

        while i < len(command):
            char = command[i]

            if escape_next:
                current.append(char)
                escape_next = False
                i += 1
                continue

            if char == '\\':
                escape_next = True
                current.append(char)
                i += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current.append(char)
                i += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current.append(char)
                i += 1
                continue

            if char == '|' and not in_single_quote and not in_double_quote:
                # Check for || (logical OR) - not a pipe
                if i + 1 < len(command) and command[i + 1] == '|':
                    current.append('||')
                    i += 2
                    continue
                # It's a pipe delimiter
                segment = ''.join(current).strip()
                if segment:
                    segments.append(segment)
                current = []
                i += 1
                continue

            current.append(char)
            i += 1

        # Add final segment
        final = ''.join(current).strip()
        if final:
            segments.append(final)

        return segments

    def _classify_ssh_exit(self, exit_code: int) -> str:
        """
        Classify SSH exit code into failure type.

        Returns:
          - 'success': Connection and command both succeeded
          - 'connection_failed': SSH couldn't connect (exit 255)
          - 'command_failed': SSH connected but remote command failed (exit 1-254)
          - 'unknown': Unexpected exit code
        """
        if exit_code == 0:
            return 'success'
        elif exit_code == 255:
            return 'connection_failed'
        elif 1 <= exit_code <= 254:
            return 'command_failed'
        else:
            return 'unknown'

    def record(self, command: str, exit_code: int, duration_ms: int,
               timed_out: bool = False, stdout: str = "", stderr: str = "",
               pipestatus: list[int] | None = None):
        """Record a command execution for learning.

        Args:
            command: The full command string
            exit_code: Exit code of the command
            duration_ms: Duration in milliseconds
            timed_out: Whether the command timed out
            stdout: Standard output (truncated)
            stderr: Standard error (truncated)
            pipestatus: List of exit codes for each pipeline segment (Issue #20)
        """
        command_hash = self._hash_command(command)
        command_template = self._template_command(command)
        success = 1 if (exit_code == 0 and not timed_out) else 0
        now = time.time()
        observation_id = str(uuid.uuid4())

        with self._connect() as conn:
            # Record in observations (long-term learning)
            conn.execute("""
                INSERT INTO observations
                (id, command_hash, command_template, command_preview, exit_code,
                 duration_ms, timed_out, output_snippet, error_snippet, weight, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
            """, (
                observation_id,
                command_hash,
                command_template,
                command[:200],
                exit_code,
                duration_ms,
                1 if timed_out else 0,
                stdout[:500] if stdout else None,
                stderr[:500] if stderr else None,
                datetime.now(timezone.utc).isoformat()
            ))

            # Record in recent_commands (hot cache)
            conn.execute("""
                INSERT INTO recent_commands
                (session_id, command_hash, command_template, command_preview,
                 timestamp, duration_ms, exit_code, timed_out, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.session_id,
                command_hash,
                command_template,
                command[:200],
                now,
                duration_ms,
                exit_code,
                1 if timed_out else 0,
                success
            ))

            # SSH-specific dual recording (Issue #2)
            ssh_info = self._parse_ssh_command(command)
            if ssh_info:
                exit_type = self._classify_ssh_exit(exit_code)
                remote_template = self._template_command(ssh_info['remote_command']) if ssh_info['remote_command'] else None

                conn.execute("""
                    INSERT INTO ssh_observations
                    (id, observation_id, host, remote_command, remote_command_template,
                     exit_code, exit_type, duration_ms, timed_out, weight, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
                """, (
                    str(uuid.uuid4()),
                    observation_id,
                    ssh_info['host'],
                    ssh_info['remote_command'][:200] if ssh_info['remote_command'] else None,
                    remote_template,
                    exit_code,
                    exit_type,
                    duration_ms,
                    1 if timed_out else 0,
                    datetime.now(timezone.utc).isoformat()
                ))

            # Update streak
            self._update_streak(conn, command_hash, success, now)

            # Pipeline segment recording (Issue #20)
            # Record each segment of a pipeline separately for per-segment learning
            if pipestatus and len(pipestatus) > 1:
                segments = self._parse_pipeline(command)
                if len(segments) == len(pipestatus):
                    for seg, seg_exit in zip(segments, pipestatus):
                        seg = seg.strip()
                        if seg:  # Skip empty segments
                            seg_hash = self._hash_command(seg)
                            seg_template = self._template_command(seg)
                            seg_success = 1 if seg_exit == 0 else 0
                            seg_obs_id = str(uuid.uuid4())

                            # Record segment observation
                            conn.execute("""
                                INSERT INTO observations
                                (id, command_hash, command_template, command_preview, exit_code,
                                 duration_ms, timed_out, output_snippet, error_snippet, weight, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
                            """, (
                                seg_obs_id,
                                seg_hash,
                                seg_template,
                                seg[:200],
                                seg_exit,
                                0,  # Unknown per-segment duration
                                0,  # Segments don't have individual timeout
                                None,  # No output per segment
                                None,
                                datetime.now(timezone.utc).isoformat()
                            ))

                            # Record in recent_commands for segment
                            conn.execute("""
                                INSERT INTO recent_commands
                                (session_id, command_hash, command_template, command_preview,
                                 timestamp, duration_ms, exit_code, timed_out, success)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                self.session_id,
                                seg_hash,
                                seg_template,
                                seg[:200],
                                now,
                                0,  # Unknown per-segment duration
                                seg_exit,
                                0,  # Not timed out
                                seg_success
                            ))

                            # Update streak for segment
                            self._update_streak(conn, seg_hash, seg_success, now)

            # Prune old recent commands
            cutoff = now - (ALAN_RECENT_WINDOW_MINUTES * 60 * 10)  # Keep 10x window
            conn.execute("DELETE FROM recent_commands WHERE timestamp < ?", (cutoff,))

    def _update_streak(self, conn, command_hash: str, success: int, now: float):
        """Update streak tracking for a command pattern."""
        row = conn.execute(
            "SELECT current_streak, longest_success_streak, longest_fail_streak, last_result FROM streaks WHERE command_hash = ?",
            (command_hash,)
        ).fetchone()

        if row:
            current = row['current_streak']
            longest_success = row['longest_success_streak']
            longest_fail = row['longest_fail_streak']
            last_result = row['last_result']

            if success == last_result:
                # Continue streak
                if success:
                    current += 1
                    longest_success = max(longest_success, current)
                else:
                    current -= 1
                    longest_fail = max(longest_fail, abs(current))
            else:
                # Streak broken, start new
                current = 1 if success else -1

            conn.execute("""
                UPDATE streaks
                SET current_streak = ?, longest_success_streak = ?, longest_fail_streak = ?,
                    last_result = ?, last_updated = ?
                WHERE command_hash = ?
            """, (current, longest_success, longest_fail, success, now, command_hash))
        else:
            conn.execute("""
                INSERT INTO streaks (command_hash, current_streak, longest_success_streak,
                                    longest_fail_streak, last_result, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (command_hash, 1 if success else -1, 1 if success else 0, 0 if success else 1, success, now))

    def get_recent_activity(self, command: str) -> dict:
        """Get recent activity for retry detection."""
        command_hash = self._hash_command(command)
        command_template = self._template_command(command)
        now = time.time()
        window_start = now - (ALAN_RECENT_WINDOW_MINUTES * 60)

        with self._connect() as conn:
            # Exact match retries
            exact_rows = conn.execute("""
                SELECT success, timestamp, duration_ms
                FROM recent_commands
                WHERE command_hash = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """, (command_hash, window_start)).fetchall()

            # Similar (template) matches
            similar_rows = conn.execute("""
                SELECT command_preview, success, timestamp
                FROM recent_commands
                WHERE command_template = ? AND command_hash != ? AND timestamp > ?
                ORDER BY timestamp DESC
                LIMIT 5
            """, (command_template, command_hash, window_start)).fetchall()

            exact_count = len(exact_rows)
            exact_successes = sum(1 for r in exact_rows if r['success'])
            exact_failures = exact_count - exact_successes

            return {
                'is_retry': exact_count > 0,
                'retry_count': exact_count,
                'recent_successes': exact_successes,
                'recent_failures': exact_failures,
                'similar_commands': [
                    {'preview': r['command_preview'][:50], 'success': bool(r['success'])}
                    for r in similar_rows
                ],
                'template': command_template
            }

    def get_streak(self, command: str) -> dict:
        """Get streak info for a command pattern."""
        command_hash = self._hash_command(command)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM streaks WHERE command_hash = ?",
                (command_hash,)
            ).fetchone()

            if not row:
                return {'has_streak': False, 'current': 0}

            return {
                'has_streak': True,
                'current': row['current_streak'],
                'longest_success': row['longest_success_streak'],
                'longest_fail': row['longest_fail_streak'],
                'last_was_success': bool(row['last_result'])
            }

    def get_pattern_stats(self, command: str) -> dict:
        """Get comprehensive statistics for a command pattern."""
        command_hash = self._hash_command(command)

        with self._connect() as conn:
            self._apply_decay(conn)

            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(weight) as weighted_total,
                    SUM(CASE WHEN timed_out = 1 THEN weight ELSE 0 END) as timeout_weight,
                    SUM(CASE WHEN exit_code = 0 THEN weight ELSE 0 END) as success_weight,
                    AVG(duration_ms) as avg_duration,
                    MAX(duration_ms) as max_duration
                FROM observations
                WHERE command_hash = ?
            """, (command_hash,)).fetchone()

            if not row or row['total'] == 0:
                base = {'known': False}
            else:
                base = {
                    'known': True,
                    'observations': row['total'],
                    'weighted_total': row['weighted_total'] or 0,
                    'timeout_rate': (row['timeout_weight'] or 0) / (row['weighted_total'] or 1),
                    'success_rate': (row['success_weight'] or 0) / (row['weighted_total'] or 1),
                    'avg_duration_ms': row['avg_duration'],
                    'max_duration_ms': row['max_duration']
                }

        # Add recent activity and streak info
        base['recent'] = self.get_recent_activity(command)
        base['streak'] = self.get_streak(command)

        return base

    def get_insights(self, command: str, timeout: int = 120) -> list[tuple[str, str]]:
        """
        Generate proactive insights for a command.
        Returns list of (level, message) tuples. Level is "info" or "warning".
        """
        insights: list[tuple[str, str]] = []
        stats = self.get_pattern_stats(command)
        recent = stats.get('recent', {})
        streak = stats.get('streak', {})

        # Retry detection
        if recent.get('is_retry'):
            count = recent['retry_count']
            successes = recent['recent_successes']
            failures = recent['recent_failures']

            if count >= 1:
                if failures > 0 and successes == 0:
                    insights.append(("warning", f"Retry #{count + 1}. Previous {failures} all failed. Different approach?"))
                elif successes > 0 and failures == 0:
                    insights.append(("info", f"Retry #{count + 1}. Previous {successes} succeeded."))
                else:
                    insights.append(("info", f"Retry #{count + 1} in last {ALAN_RECENT_WINDOW_MINUTES}m. {successes}/{count} succeeded."))

        # Similar commands
        if recent.get('similar_commands') and not recent.get('is_retry'):
            similar = recent['similar_commands']
            if similar:
                sim_success = sum(1 for s in similar if s['success'])
                template = recent.get('template', 'similar pattern')
                insights.append(("info", f"Similar to '{template}' - {sim_success}/{len(similar)} succeeded recently."))

        # Streak info
        if streak.get('has_streak'):
            current = streak['current']
            if current >= ALAN_STREAK_THRESHOLD:
                insights.append(("info", f"Streak: {current} successes in a row. Solid."))
            elif current <= -ALAN_STREAK_THRESHOLD:
                insights.append(("warning", f"Failing streak: {abs(current)}. Same approach?"))

        # Pattern history warnings
        if stats.get('known'):
            if stats['timeout_rate'] > 0.5:
                insights.append(("warning", f"{stats['timeout_rate']*100:.0f}% timeout rate for this pattern."))
            elif stats['success_rate'] > 0.9 and stats['observations'] >= 5:
                insights.append(("info", f"Reliable pattern: {stats['success_rate']*100:.0f}% success ({stats['observations']} runs)."))

            if stats.get('avg_duration_ms'):
                avg_sec = stats['avg_duration_ms'] / 1000
                if avg_sec > 10:
                    insights.append(("info", f"Usually takes ~{avg_sec:.0f}s."))
        else:
            insights.append(("info", "New pattern. No history yet."))

        # SSH-specific insights (Issue #2)
        ssh_info = self._parse_ssh_command(command)
        if ssh_info:
            ssh_insights = self._get_ssh_insights(ssh_info)
            insights.extend(ssh_insights)

        return insights

    def _get_ssh_insights(self, ssh_info: dict) -> list[tuple[str, str]]:
        """Generate SSH-specific insights based on host and command history."""
        insights: list[tuple[str, str]] = []
        host = ssh_info['host']
        remote_cmd = ssh_info['remote_command']

        with self._connect() as conn:
            # Host connectivity stats
            host_stats = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN exit_type = 'connection_failed' THEN 1 ELSE 0 END) as conn_failures,
                    SUM(CASE WHEN exit_type = 'command_failed' THEN 1 ELSE 0 END) as cmd_failures,
                    AVG(duration_ms) as avg_duration
                FROM ssh_observations
                WHERE host = ?
            """, (host,)).fetchone()

            if host_stats and host_stats['total'] > 0:
                total = host_stats['total']
                conn_fail_rate = host_stats['conn_failures'] / total

                if conn_fail_rate > 0.3:
                    insights.append(("warning", f"Host '{host}' has {conn_fail_rate*100:.0f}% connection failure rate ({host_stats['conn_failures']}/{total})."))
                elif host_stats['successes'] == total and total >= 3:
                    insights.append(("info", f"Host '{host}' is reliable: {total} successful connections."))

            # Remote command stats (if there's a command)
            if remote_cmd:
                remote_template = self._template_command(remote_cmd)
                if remote_template:
                    cmd_stats = conn.execute("""
                        SELECT
                            COUNT(*) as total,
                            SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
                            SUM(CASE WHEN exit_type = 'command_failed' THEN 1 ELSE 0 END) as cmd_failures,
                            GROUP_CONCAT(DISTINCT host) as hosts
                        FROM ssh_observations
                        WHERE remote_command_template = ?
                    """, (remote_template,)).fetchone()

                    if cmd_stats and cmd_stats['total'] > 0:
                        total = cmd_stats['total']
                        success_rate = cmd_stats['successes'] / total
                        hosts = cmd_stats['hosts'].split(',') if cmd_stats['hosts'] else []

                        if cmd_stats['cmd_failures'] > 0 and success_rate < 0.5:
                            insights.append(("warning", f"Remote command '{remote_template}' fails often ({cmd_stats['cmd_failures']}/{total} across {len(hosts)} hosts)."))
                        elif success_rate > 0.9 and total >= 3:
                            insights.append(("info", f"Remote command '{remote_template}' reliable across {len(hosts)} hosts ({success_rate*100:.0f}% success)."))

        return insights

    def get_ssh_host_stats(self, host: str) -> dict:
        """Get statistics for a specific SSH host."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN exit_type = 'connection_failed' THEN 1 ELSE 0 END) as connection_failures,
                    SUM(CASE WHEN exit_type = 'command_failed' THEN 1 ELSE 0 END) as command_failures,
                    AVG(duration_ms) as avg_duration_ms,
                    MIN(created_at) as first_seen,
                    MAX(created_at) as last_seen
                FROM ssh_observations
                WHERE host = ?
            """, (host,)).fetchone()

            if not row or row['total'] == 0:
                return {'known': False, 'host': host}

            total = row['total']
            return {
                'known': True,
                'host': host,
                'total_connections': total,
                'successes': row['successes'] or 0,
                'connection_failures': row['connection_failures'] or 0,
                'command_failures': row['command_failures'] or 0,
                'connection_success_rate': (total - (row['connection_failures'] or 0)) / total,
                'overall_success_rate': (row['successes'] or 0) / total,
                'avg_duration_ms': row['avg_duration_ms'],
                'first_seen': row['first_seen'],
                'last_seen': row['last_seen']
            }

    def get_ssh_command_stats(self, remote_command: str) -> dict:
        """Get statistics for a remote command across all hosts."""
        remote_template = self._template_command(remote_command)

        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(DISTINCT host) as host_count,
                    SUM(CASE WHEN exit_type = 'success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN exit_type = 'command_failed' THEN 1 ELSE 0 END) as command_failures,
                    AVG(duration_ms) as avg_duration_ms,
                    GROUP_CONCAT(DISTINCT host) as hosts
                FROM ssh_observations
                WHERE remote_command_template = ?
            """, (remote_template,)).fetchone()

            if not row or row['total'] == 0:
                return {'known': False, 'command_template': remote_template}

            total = row['total']
            return {
                'known': True,
                'command_template': remote_template,
                'total_executions': total,
                'host_count': row['host_count'],
                'hosts': row['hosts'].split(',') if row['hosts'] else [],
                'successes': row['successes'] or 0,
                'command_failures': row['command_failures'] or 0,
                'success_rate': (row['successes'] or 0) / total,
                'avg_duration_ms': row['avg_duration_ms']
            }

    def get_session_stats(self) -> dict:
        """Get statistics for the current session."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(success) as successes,
                    SUM(timed_out) as timeouts,
                    AVG(duration_ms) as avg_duration
                FROM recent_commands
                WHERE session_id = ?
            """, (self.session_id,)).fetchone()

            total = row['total'] or 0
            successes = row['successes'] or 0

            # Count retries (same hash appearing multiple times)
            retry_row = conn.execute("""
                SELECT COUNT(*) as retries FROM (
                    SELECT command_hash, COUNT(*) as cnt
                    FROM recent_commands
                    WHERE session_id = ?
                    GROUP BY command_hash
                    HAVING cnt > 1
                )
            """, (self.session_id,)).fetchone()

            return {
                'session_id': self.session_id,
                'total_commands': total,
                'successes': successes,
                'failures': total - successes,
                'success_rate': successes / total if total > 0 else 0,
                'timeouts': row['timeouts'] or 0,
                'retries': retry_row['retries'] or 0,
                'avg_duration_ms': row['avg_duration']
            }

    def get_hot_patterns(self, limit: int = 10) -> list:
        """Get most frequently used patterns in recent window."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT
                    command_template,
                    COUNT(*) as count,
                    SUM(success) as successes,
                    AVG(duration_ms) as avg_duration
                FROM recent_commands
                WHERE session_id = ?
                GROUP BY command_template
                ORDER BY count DESC
                LIMIT ?
            """, (self.session_id, limit)).fetchall()

            return [
                {
                    'pattern': row['command_template'],
                    'count': row['count'],
                    'success_rate': row['successes'] / row['count'] if row['count'] > 0 else 0,
                    'avg_duration_ms': row['avg_duration']
                }
                for row in rows
            ]

    def _apply_decay(self, conn):
        """Apply temporal decay to all weights."""
        conn.execute("""
            UPDATE observations
            SET weight = weight * POWER(0.5,
                (JULIANDAY('now') - JULIANDAY(created_at)) * 24 / ?
            )
            WHERE weight > ?
        """, (ALAN_DECAY_HALF_LIFE_HOURS, ALAN_PRUNE_THRESHOLD))

    def prune(self):
        """Remove decayed entries and enforce limits."""
        with self._connect() as conn:
            self._apply_decay(conn)
            conn.execute("DELETE FROM observations WHERE weight < ?", (ALAN_PRUNE_THRESHOLD,))
            conn.execute("""
                DELETE FROM observations
                WHERE id NOT IN (
                    SELECT id FROM observations ORDER BY weight DESC LIMIT ?
                )
            """, (ALAN_MAX_ENTRIES,))

            # Also prune SSH observations (Issue #2)
            # Apply decay to ssh_observations
            conn.execute("""
                UPDATE ssh_observations
                SET weight = weight * POWER(0.5,
                    (JULIANDAY('now') - JULIANDAY(created_at)) * 24 / ?
                )
                WHERE weight > ?
            """, (ALAN_DECAY_HALF_LIFE_HOURS, ALAN_PRUNE_THRESHOLD))
            conn.execute("DELETE FROM ssh_observations WHERE weight < ?", (ALAN_PRUNE_THRESHOLD,))
            # Clean up orphaned ssh_observations (linked observation was deleted)
            conn.execute("""
                DELETE FROM ssh_observations
                WHERE observation_id NOT IN (SELECT id FROM observations)
            """)

            conn.execute("""
                INSERT OR REPLACE INTO meta (key, value) VALUES ('last_prune', ?)
            """, (datetime.now(timezone.utc).isoformat(),))

    def maybe_prune(self):
        """Prune if enough time has passed."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'last_prune'").fetchone()
            if row:
                last_prune = datetime.fromisoformat(row['value'].replace('Z', '+00:00'))
                if last_prune.tzinfo is None:
                    last_prune = last_prune.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_prune < timedelta(hours=ALAN_PRUNE_INTERVAL_HOURS):
                    return
        self.prune()

    def get_stats(self) -> dict:
        """Get overall A.L.A.N. statistics."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_observations,
                    COUNT(DISTINCT command_hash) as unique_patterns,
                    SUM(weight) as total_weight,
                    MIN(created_at) as oldest,
                    MAX(created_at) as newest
                FROM observations
            """).fetchone()

            base = {
                'total_observations': row['total_observations'],
                'unique_patterns': row['unique_patterns'],
                'total_weight': row['total_weight'] or 0,
                'oldest': row['oldest'],
                'newest': row['newest']
            }

        # Add session and hot patterns
        base['session'] = self.get_session_stats()
        base['hot_patterns'] = self.get_hot_patterns(5)

        return base
