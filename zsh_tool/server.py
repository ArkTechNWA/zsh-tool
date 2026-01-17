#!/usr/bin/env python3
"""
Zsh Tool MCP Server
===================
Full-parity zsh execution with NEVERHANG circuit breaker and A.L.A.N. learning.

For Johnny5. For us.
"""

import anyio
import asyncio
import fcntl
import hashlib
import os
import pty
import re
import select
import sqlite3
import struct
import termios
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# =============================================================================
# Configuration
# =============================================================================

ALAN_DB_PATH = Path(os.environ.get("ALAN_DB_PATH", "~/.claude/plugins/zsh-tool/data/alan.db")).expanduser()
NEVERHANG_TIMEOUT_DEFAULT = int(os.environ.get("NEVERHANG_TIMEOUT_DEFAULT", 3600))  # 1 hour - effectively never auto-kills
NEVERHANG_TIMEOUT_MAX = int(os.environ.get("NEVERHANG_TIMEOUT_MAX", 600))
TRUNCATE_OUTPUT_AT = 30000  # Match Bash tool behavior

# A.L.A.N. decay settings
ALAN_DECAY_HALF_LIFE_HOURS = 24  # Weight halves every 24 hours
ALAN_PRUNE_THRESHOLD = 0.01     # Prune entries with weight below this
ALAN_PRUNE_INTERVAL_HOURS = 6   # Run pruning every 6 hours
ALAN_MAX_ENTRIES = 10000        # Hard cap on entries

# NEVERHANG circuit breaker settings
NEVERHANG_FAILURE_THRESHOLD = 3   # Failures before circuit opens
NEVERHANG_RECOVERY_TIMEOUT = 300  # Seconds before trying again
NEVERHANG_SAMPLE_WINDOW = 3600    # Only count failures in last hour


# =============================================================================
# A.L.A.N. 2.0 - As Long As Necessary
# "Maybe you're fuckin' up, maybe you're doing it right."
# =============================================================================

# A.L.A.N. 2.0 settings
ALAN_RECENT_WINDOW_SIZE = 50      # Track last N commands
ALAN_RECENT_WINDOW_MINUTES = 10   # For retry detection
ALAN_STREAK_THRESHOLD = 3         # Min streak to report

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
               timed_out: bool = False, stdout: str = "", stderr: str = ""):
        """Record a command execution for learning."""
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
                datetime.utcnow().isoformat()
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
                    datetime.utcnow().isoformat()
                ))

            # Update streak
            self._update_streak(conn, command_hash, success, now)

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

    def get_insights(self, command: str, timeout: int = 120) -> list:
        """
        Generate proactive insights for a command.
        Returns list of insight messages to show the user.
        """
        insights = []
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
                    insights.append(f"Retry #{count + 1}. Previous {failures} all failed. Different approach?")
                elif successes > 0 and failures == 0:
                    insights.append(f"Retry #{count + 1}. Previous {successes} succeeded.")
                else:
                    insights.append(f"Retry #{count + 1} in last {ALAN_RECENT_WINDOW_MINUTES}m. {successes}/{count} succeeded.")

        # Similar commands
        if recent.get('similar_commands') and not recent.get('is_retry'):
            similar = recent['similar_commands']
            if similar:
                sim_success = sum(1 for s in similar if s['success'])
                template = recent.get('template', 'similar pattern')
                insights.append(f"Similar to '{template}' - {sim_success}/{len(similar)} succeeded recently.")

        # Streak info
        if streak.get('has_streak'):
            current = streak['current']
            if current >= ALAN_STREAK_THRESHOLD:
                insights.append(f"Streak: {current} successes in a row. Solid.")
            elif current <= -ALAN_STREAK_THRESHOLD:
                insights.append(f"Failing streak: {abs(current)}. Same approach?")

        # Pattern history warnings
        if stats.get('known'):
            if stats['timeout_rate'] > 0.5:
                insights.append(f"{stats['timeout_rate']*100:.0f}% timeout rate for this pattern.")
            elif stats['success_rate'] > 0.9 and stats['observations'] >= 5:
                insights.append(f"Reliable pattern: {stats['success_rate']*100:.0f}% success ({stats['observations']} runs).")

            if stats.get('avg_duration_ms'):
                avg_sec = stats['avg_duration_ms'] / 1000
                if avg_sec > 10:
                    insights.append(f"Usually takes ~{avg_sec:.0f}s.")
        else:
            insights.append("New pattern. No history yet.")

        # SSH-specific insights (Issue #2)
        ssh_info = self._parse_ssh_command(command)
        if ssh_info:
            ssh_insights = self._get_ssh_insights(ssh_info)
            insights.extend(ssh_insights)

        return insights

    def _get_ssh_insights(self, ssh_info: dict) -> list:
        """Generate SSH-specific insights based on host and command history."""
        insights = []
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
                    insights.append(f"Host '{host}' has {conn_fail_rate*100:.0f}% connection failure rate ({host_stats['conn_failures']}/{total}).")
                elif host_stats['successes'] == total and total >= 3:
                    insights.append(f"Host '{host}' is reliable: {total} successful connections.")

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
                            insights.append(f"Remote command '{remote_template}' fails often ({cmd_stats['cmd_failures']}/{total} across {len(hosts)} hosts).")
                        elif success_rate > 0.9 and total >= 3:
                            insights.append(f"Remote command '{remote_template}' reliable across {len(hosts)} hosts ({success_rate*100:.0f}% success).")

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
            """, (datetime.utcnow().isoformat(),))

    def maybe_prune(self):
        """Prune if enough time has passed."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'last_prune'").fetchone()
            if row:
                last_prune = datetime.fromisoformat(row['value'])
                if datetime.utcnow() - last_prune < timedelta(hours=ALAN_PRUNE_INTERVAL_HOURS):
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


# =============================================================================
# NEVERHANG Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking execution
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """Circuit breaker for command patterns that tend to hang."""

    state: CircuitState = CircuitState.CLOSED
    failures: list = field(default_factory=list)  # List of (timestamp, command_hash)
    last_failure: Optional[float] = None
    opened_at: Optional[float] = None

    def record_timeout(self, command_hash: str):
        """Record a timeout failure."""
        now = time.time()
        self.failures.append((now, command_hash))
        self.last_failure = now

        # Clean old failures outside sample window
        cutoff = now - NEVERHANG_SAMPLE_WINDOW
        self.failures = [(t, h) for t, h in self.failures if t > cutoff]

        # Check if we should open the circuit
        if len(self.failures) >= NEVERHANG_FAILURE_THRESHOLD:
            self.state = CircuitState.OPEN
            self.opened_at = now

    def record_success(self):
        """Record a successful execution."""
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self.failures = []

    def should_allow(self) -> tuple[bool, Optional[str]]:
        """Check if execution should be allowed."""
        if self.state == CircuitState.CLOSED:
            return True, None

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.opened_at and time.time() - self.opened_at > NEVERHANG_RECOVERY_TIMEOUT:
                self.state = CircuitState.HALF_OPEN
                return True, "NEVERHANG: Circuit half-open, testing recovery"
            return False, f"NEVERHANG: Circuit OPEN due to {len(self.failures)} recent timeouts. Retry in {int(NEVERHANG_RECOVERY_TIMEOUT - (time.time() - (self.opened_at or 0)))}s"

        # HALF_OPEN - allow but monitor
        return True, "NEVERHANG: Circuit half-open, monitoring"

    def get_status(self) -> dict:
        """Get circuit breaker status."""
        return {
            'state': self.state.value,
            'recent_failures': len(self.failures),
            'failure_threshold': NEVERHANG_FAILURE_THRESHOLD,
            'recovery_timeout': NEVERHANG_RECOVERY_TIMEOUT,
            'opened_at': self.opened_at,
            'time_until_retry': max(0, NEVERHANG_RECOVERY_TIMEOUT - (time.time() - (self.opened_at or 0))) if self.opened_at else None
        }


# Global instances
alan = ALAN(ALAN_DB_PATH)
circuit_breaker = CircuitBreaker()

# =============================================================================
# Live Task Manager (Issue #1: Yield-based execution with oversight)
# =============================================================================

YIELD_AFTER_DEFAULT = 2.0  # Seconds before yielding control back (MCP call returns)

@dataclass
class LiveTask:
    """A running command with live output buffering."""
    task_id: str
    command: str
    process: Any  # asyncio.subprocess.Process or PID for PTY
    started_at: float
    timeout: int
    output_buffer: str = ""
    output_read_pos: int = 0  # How much output has been returned to caller
    status: str = "running"  # running, completed, timeout, killed, error
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # PTY mode fields
    is_pty: bool = False
    pty_fd: Optional[int] = None  # Master PTY file descriptor


# Active tasks registry
live_tasks: dict[str, LiveTask] = {}


def _cleanup_task(task_id: str):
    """Clean up task resources and remove from registry."""
    if task_id in live_tasks:
        task = live_tasks[task_id]
        if task.is_pty:
            # Close PTY file descriptor
            if task.pty_fd is not None:
                try:
                    os.close(task.pty_fd)
                except Exception:
                    pass
        else:
            # Close stdin if still open
            if task.process.stdin and not task.process.stdin.is_closing():
                try:
                    task.process.stdin.close()
                except Exception:
                    pass
        # Remove from registry to prevent memory leak
        del live_tasks[task_id]


async def _output_collector(task: LiveTask):
    """Background coroutine that collects output from a running process."""
    try:
        while True:
            # Read available output (non-blocking style via small reads)
            try:
                chunk = await asyncio.wait_for(
                    task.process.stdout.read(4096),
                    timeout=0.1
                )
                if chunk:
                    task.output_buffer += chunk.decode('utf-8', errors='replace')
                elif task.process.returncode is not None:
                    # Process finished
                    break
                else:
                    # Empty read but process still running - yield to prevent spin
                    await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                # No data available right now, check if process done
                if task.process.returncode is not None:
                    break
                # Check overall timeout
                elapsed = time.time() - task.started_at
                if elapsed > task.timeout:
                    task.status = "timeout"
                    task.process.kill()
                    await task.process.wait()
                    circuit_breaker.record_timeout(alan._hash_command(task.command))
                    break
                # Yield before continuing to prevent event loop starvation
                await asyncio.sleep(0.01)
                continue

        # Process completed
        if task.status == "running":
            task.status = "completed"
            task.exit_code = task.process.returncode
            circuit_breaker.record_success()

        # Record in A.L.A.N.
        duration_ms = int((time.time() - task.started_at) * 1000)
        alan.record(
            task.command,
            task.exit_code if task.exit_code is not None else -1,
            duration_ms,
            task.status == "timeout",
            task.output_buffer[:500],
            ""
        )
        alan.maybe_prune()

    except Exception as e:
        task.status = "error"
        task.error = str(e)


async def _pty_output_collector(task: LiveTask):
    """Background coroutine that collects output from a PTY."""
    loop = asyncio.get_event_loop()

    try:
        while True:
            # Check if process is still running
            try:
                pid_result = os.waitpid(task.process, os.WNOHANG)
                if pid_result[0] != 0:
                    # Process exited
                    task.exit_code = os.WEXITSTATUS(pid_result[1]) if os.WIFEXITED(pid_result[1]) else -1
                    task.status = "completed"
                    circuit_breaker.record_success()
                    break
            except ChildProcessError:
                # Process already reaped
                task.status = "completed"
                task.exit_code = 0
                break

            # Check timeout
            elapsed = time.time() - task.started_at
            if elapsed > task.timeout:
                task.status = "timeout"
                try:
                    os.kill(task.process, 9)  # SIGKILL
                except ProcessLookupError:
                    pass
                circuit_breaker.record_timeout(alan._hash_command(task.command))
                break

            # Read available output from PTY (non-blocking)
            try:
                # Use select to check if data available
                readable, _, _ = select.select([task.pty_fd], [], [], 0.1)
                if readable:
                    chunk = os.read(task.pty_fd, 4096)
                    if chunk:
                        task.output_buffer += chunk.decode('utf-8', errors='replace')
            except (OSError, IOError):
                # PTY closed or error
                break

            # Small yield to not hog CPU
            await asyncio.sleep(0.05)

        # Read any remaining output
        try:
            while True:
                readable, _, _ = select.select([task.pty_fd], [], [], 0.1)
                if not readable:
                    break
                chunk = os.read(task.pty_fd, 4096)
                if not chunk:
                    break
                task.output_buffer += chunk.decode('utf-8', errors='replace')
        except (OSError, IOError):
            pass

        # Record in A.L.A.N.
        duration_ms = int((time.time() - task.started_at) * 1000)
        alan.record(
            task.command,
            task.exit_code if task.exit_code is not None else -1,
            duration_ms,
            task.status == "timeout",
            task.output_buffer[:500],
            ""
        )
        alan.maybe_prune()

    except Exception as e:
        task.status = "error"
        task.error = str(e)


async def execute_zsh_pty(
    command: str,
    timeout: int = NEVERHANG_TIMEOUT_DEFAULT,
    yield_after: float = YIELD_AFTER_DEFAULT,
    description: Optional[str] = None
) -> dict:
    """Execute command in a PTY for full terminal emulation."""

    # Validate timeout
    timeout = min(timeout, NEVERHANG_TIMEOUT_MAX)

    # Check A.L.A.N. 2.0 for insights
    insights = alan.get_insights(command, timeout)
    warnings = [f"A.L.A.N.: {i}" for i in insights]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        warnings.append(circuit_message)

    # Create task ID
    task_id = str(uuid.uuid4())[:8]

    # Fork with PTY
    pid, master_fd = pty.fork()

    if pid == 0:
        # Child process - exec zsh with command
        os.execvp('/bin/zsh', ['/bin/zsh', '-c', command])
        # If exec fails, exit
        os._exit(1)

    # Parent process - set up non-blocking read
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # Set terminal size (80x24 default)
    try:
        winsize = struct.pack('HHHH', 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    task = LiveTask(
        task_id=task_id,
        command=command,
        process=pid,  # PID instead of Process object
        started_at=time.time(),
        timeout=timeout,
        is_pty=True,
        pty_fd=master_fd
    )
    live_tasks[task_id] = task

    # Start background output collector
    asyncio.create_task(_pty_output_collector(task))

    # Wait for yield_after seconds then return (process continues in background)
    await asyncio.sleep(yield_after)

    # Check status and return
    return _build_task_response(task, warnings)


async def execute_zsh_yielding(
    command: str,
    timeout: int = NEVERHANG_TIMEOUT_DEFAULT,
    yield_after: float = YIELD_AFTER_DEFAULT,
    description: Optional[str] = None
) -> dict:
    """Execute with yield - returns partial output after yield_after seconds if still running."""

    # Validate timeout
    timeout = min(timeout, NEVERHANG_TIMEOUT_MAX)

    # Check A.L.A.N. 2.0 for insights
    insights = alan.get_insights(command, timeout)
    warnings = [f"A.L.A.N.: {i}" for i in insights]

    # Check NEVERHANG circuit breaker
    allowed, circuit_message = circuit_breaker.should_allow()
    if not allowed:
        return {
            'success': False,
            'error': circuit_message,
            'circuit_status': circuit_breaker.get_status()
        }
    if circuit_message:
        warnings.append(circuit_message)

    # Create task
    task_id = str(uuid.uuid4())[:8]

    # Start process with stdin pipe for interactive input
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
        executable='/bin/zsh'
    )

    task = LiveTask(
        task_id=task_id,
        command=command,
        process=proc,
        started_at=time.time(),
        timeout=timeout
    )
    live_tasks[task_id] = task

    # Start background output collector
    asyncio.create_task(_output_collector(task))

    # Wait for yield_after seconds then return (process continues in background)
    await asyncio.sleep(yield_after)

    # Check status and return
    return _build_task_response(task, warnings)


def _build_task_response(task: LiveTask, warnings: list = None) -> dict:
    """Build response dict from task state."""
    # Get new output since last read
    new_output = task.output_buffer[task.output_read_pos:]
    task.output_read_pos = len(task.output_buffer)

    # Truncate if needed
    if len(new_output) > TRUNCATE_OUTPUT_AT:
        new_output = new_output[:TRUNCATE_OUTPUT_AT] + f"\n[TRUNCATED - {len(new_output)} chars total]"

    elapsed = time.time() - task.started_at

    result = {
        'task_id': task.task_id,
        'status': task.status,
        'output': new_output,
        'elapsed_seconds': round(elapsed, 1),
    }

    if task.status == "running":
        result['has_stdin'] = task.is_pty or (hasattr(task.process, 'stdin') and task.process.stdin is not None)
        result['is_pty'] = task.is_pty
        result['message'] = f"Command running ({elapsed:.1f}s). Use zsh_poll to get more output, zsh_send to send input."
    elif task.status == "completed":
        result['success'] = task.exit_code == 0
        result['exit_code'] = task.exit_code
        _cleanup_task(task.task_id)
    elif task.status == "timeout":
        result['success'] = False
        result['error'] = f"Command timed out after {task.timeout}s"
        _cleanup_task(task.task_id)
    elif task.status == "killed":
        result['success'] = False
        result['error'] = "Command was killed"
        _cleanup_task(task.task_id)
    elif task.status == "error":
        result['success'] = False
        result['error'] = task.error
        _cleanup_task(task.task_id)

    if warnings:
        result['warnings'] = warnings

    return result


async def poll_task(task_id: str) -> dict:
    """Get current output from a running task."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]
    return _build_task_response(task)


async def send_to_task(task_id: str, input_text: str) -> dict:
    """Send input to a task's stdin (pipe or PTY)."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]

    if task.status != "running":
        return {'error': f'Task not running (status: {task.status})', 'success': False}

    try:
        data = input_text if input_text.endswith('\n') else input_text + '\n'

        if task.is_pty:
            # Write to PTY master
            os.write(task.pty_fd, data.encode('utf-8'))
        else:
            # Write to process stdin pipe
            if not task.process.stdin:
                return {'error': 'No stdin available for this task', 'success': False}
            task.process.stdin.write(data.encode('utf-8'))
            await task.process.stdin.drain()

        return {'success': True, 'message': f'Sent {len(input_text)} chars to task {task_id}'}
    except Exception as e:
        return {'error': f'Failed to send input: {e}', 'success': False}


async def kill_task(task_id: str) -> dict:
    """Kill a running task."""
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]

    if task.status != "running":
        return {'error': f'Task not running (status: {task.status})', 'success': False}

    try:
        if task.is_pty:
            # Kill PTY process by PID
            os.kill(task.process, 9)  # SIGKILL
            # Non-blocking reap - don't wait for zombie
            try:
                os.waitpid(task.process, os.WNOHANG)
            except ChildProcessError:
                pass
        else:
            task.process.kill()
            # Don't block forever waiting for process to die
            try:
                await asyncio.wait_for(task.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # Process didn't die cleanly, but we tried

        task.status = "killed"
        _cleanup_task(task_id)
        return {'success': True, 'message': f'Task {task_id} killed'}
    except Exception as e:
        return {'error': f'Failed to kill task: {e}', 'success': False}


def list_tasks() -> dict:
    """List all active tasks."""
    tasks = []
    for tid, task in live_tasks.items():
        tasks.append({
            'task_id': tid,
            'command': task.command[:50] + ('...' if len(task.command) > 50 else ''),
            'status': task.status,
            'elapsed_seconds': round(time.time() - task.started_at, 1),
            'output_bytes': len(task.output_buffer)
        })
    return {'tasks': tasks, 'count': len(tasks)}


# =============================================================================
# MCP Server (using official SDK)
# =============================================================================

server = Server("zsh-tool")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="zsh",
            description="Execute a zsh command with yield-based oversight. Returns after yield_after seconds with partial output if still running. Use zsh_poll to continue collecting output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The zsh command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Max execution time in seconds (default: {NEVERHANG_TIMEOUT_DEFAULT}, max: {NEVERHANG_TIMEOUT_MAX})"
                    },
                    "yield_after": {
                        "type": "number",
                        "description": f"Return control after this many seconds if still running (default: {YIELD_AFTER_DEFAULT})"
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what this command does"
                    },
                    "pty": {
                        "type": "boolean",
                        "description": "Use PTY (pseudo-terminal) mode for full terminal emulation. Enables proper handling of interactive prompts, colors, and programs that require a TTY."
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="zsh_poll",
            description="Get more output from a running task. Call repeatedly until status is not 'running'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned from zsh command"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="zsh_send",
            description="Send input to a running task's stdin. Use for interactive commands that need input.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID of the running command"
                    },
                    "input": {
                        "type": "string",
                        "description": "Text to send to stdin (newline added automatically)"
                    }
                },
                "required": ["task_id", "input"]
            }
        ),
        Tool(
            name="zsh_kill",
            description="Kill a running task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to kill"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="zsh_tasks",
            description="List all active tasks with their status.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_health",
            description="Get health status of zsh-tool including NEVERHANG and A.L.A.N. status",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_alan_stats",
            description="Get A.L.A.N. learning database statistics",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_alan_query",
            description="Query A.L.A.N. for insights about a command pattern",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to query pattern stats for"
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="zsh_neverhang_status",
            description="Get NEVERHANG circuit breaker status",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="zsh_neverhang_reset",
            description="Reset NEVERHANG circuit breaker to closed state",
            inputSchema={"type": "object", "properties": {}}
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    import json

    # Protect against MCP abort - wrap entire handler
    try:
        return await _handle_tool_call(name, arguments)
    except asyncio.CancelledError:
        # MCP aborted - return graceful error instead of propagating
        return [TextContent(
            type="text",
            text=json.dumps({
                'success': False,
                'error': 'MCP call was cancelled',
                'hint': 'Use zsh_tasks to check for running tasks'
            }, indent=2)
        )]


async def _handle_tool_call(name: str, arguments: dict) -> list[TextContent]:
    """Internal tool call handler."""
    import json

    if name == "zsh":
        use_pty = arguments.get("pty", False)
        if use_pty:
            result = await execute_zsh_pty(
                command=arguments["command"],
                timeout=arguments.get("timeout", NEVERHANG_TIMEOUT_DEFAULT),
                yield_after=arguments.get("yield_after", YIELD_AFTER_DEFAULT),
                description=arguments.get("description")
            )
        else:
            result = await execute_zsh_yielding(
                command=arguments["command"],
                timeout=arguments.get("timeout", NEVERHANG_TIMEOUT_DEFAULT),
                yield_after=arguments.get("yield_after", YIELD_AFTER_DEFAULT),
                description=arguments.get("description")
            )
        return _format_task_output(result)
    elif name == "zsh_poll":
        result = await poll_task(arguments["task_id"])
        return _format_task_output(result)
    elif name == "zsh_send":
        result = await send_to_task(arguments["task_id"], arguments["input"])
    elif name == "zsh_kill":
        result = await kill_task(arguments["task_id"])
    elif name == "zsh_tasks":
        result = list_tasks()
    elif name == "zsh_health":
        result = {
            'status': 'healthy',
            'neverhang': circuit_breaker.get_status(),
            'alan': alan.get_stats(),
            'active_tasks': len(live_tasks)
        }
    elif name == "zsh_alan_stats":
        result = alan.get_stats()
    elif name == "zsh_alan_query":
        result = alan.get_pattern_stats(arguments["command"])
    elif name == "zsh_neverhang_status":
        result = circuit_breaker.get_status()
    elif name == "zsh_neverhang_reset":
        circuit_breaker.state = CircuitState.CLOSED
        circuit_breaker.failures = []
        circuit_breaker.opened_at = None
        result = {'success': True, 'message': 'Circuit breaker reset to CLOSED state'}
    else:
        result = {'error': f'Unknown tool: {name}'}

    return [TextContent(
        type="text",
        text=json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    )]


def _format_task_output(result: dict) -> list[TextContent]:
    """Format task-based execution output cleanly."""
    parts = []

    # Output first - clean
    output = result.get('output', '')
    if output:
        parts.append(output.rstrip('\n'))

    # Error message if present
    error = result.get('error')
    if error:
        parts.append(f"[error] {error}")

    # Status line
    status = result.get('status', 'unknown')
    task_id = result.get('task_id', '')
    elapsed = result.get('elapsed_seconds', 0)

    if status == "running":
        has_stdin = result.get('has_stdin', False)
        parts.append(f"[RUNNING task_id={task_id} elapsed={elapsed}s stdin={'yes' if has_stdin else 'no'}]")
        parts.append("Use zsh_poll to continue, zsh_send to input, zsh_kill to stop.")
    elif status == "completed":
        exit_code = result.get('exit_code', 0)
        if exit_code == 0:
            parts.append(f"[COMPLETED task_id={task_id} elapsed={elapsed}s exit=0]")
        else:
            parts.append(f"[COMPLETED task_id={task_id} elapsed={elapsed}s exit={exit_code}]")
    elif status == "timeout":
        parts.append(f"[TIMEOUT task_id={task_id} elapsed={elapsed}s]")
    elif status == "killed":
        parts.append(f"[KILLED task_id={task_id} elapsed={elapsed}s]")
    elif status == "error":
        parts.append(f"[ERROR task_id={task_id} elapsed={elapsed}s]")

    if result.get('warnings'):
        parts.append(f"[warnings: {result['warnings']}]")

    return [TextContent(type="text", text='\n'.join(parts) if parts else "(no output)")]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except* anyio.ClosedResourceError:
        # Graceful shutdown when stdin closes (normal for MCP stdio transport)
        pass
    except* Exception as eg:
        # Log unexpected errors but don't crash
        import sys
        print(f"zsh-tool: unexpected error: {eg}", file=sys.stderr)


def run():
    """Entry point for CLI."""
    asyncio.run(main())


if __name__ == '__main__':
    run()