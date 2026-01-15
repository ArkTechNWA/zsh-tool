#!/usr/bin/env python3
"""
Zsh Tool MCP Server
===================
Full-parity zsh execution with NEVERHANG circuit breaker and A.L.A.N. learning.

For Johnny5. For us.
"""

import anyio
import asyncio
import hashlib
import os
import re
import sqlite3
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
NEVERHANG_TIMEOUT_DEFAULT = int(os.environ.get("NEVERHANG_TIMEOUT_DEFAULT", 120))
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
# A.L.A.N. - As Long As Necessary
# =============================================================================

class ALAN:
    """Short-term learning database with temporal decay."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
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

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)

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
        """Create a hash for command pattern matching."""
        # Normalize: collapse whitespace, remove specific paths/values
        normalized = re.sub(r'\s+', ' ', command.strip())
        # Remove quoted strings (they're often variable)
        normalized = re.sub(r'"[^"]*"', '""', normalized)
        normalized = re.sub(r"'[^']*'", "''", normalized)
        # Remove numbers (often variable)
        normalized = re.sub(r'\b\d+\b', 'N', normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def record(self, command: str, exit_code: int, duration_ms: int,
               timed_out: bool = False, stdout: str = "", stderr: str = ""):
        """Record a command execution for learning."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO observations
                (id, command_hash, command_preview, exit_code, duration_ms,
                 timed_out, output_snippet, error_snippet, weight, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
            """, (
                str(uuid.uuid4()),
                self._hash_command(command),
                command[:200],  # Preview only
                exit_code,
                duration_ms,
                1 if timed_out else 0,
                stdout[:500] if stdout else None,
                stderr[:500] if stderr else None,
                datetime.utcnow().isoformat()
            ))

    def get_pattern_stats(self, command: str) -> dict:
        """Get statistics for a command pattern."""
        command_hash = self._hash_command(command)
        with self._connect() as conn:
            # Apply decay before querying
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
                return {'known': False}

            return {
                'known': True,
                'observations': row['total'],
                'weighted_total': row['weighted_total'] or 0,
                'timeout_rate': (row['timeout_weight'] or 0) / (row['weighted_total'] or 1),
                'success_rate': (row['success_weight'] or 0) / (row['weighted_total'] or 1),
                'avg_duration_ms': row['avg_duration'],
                'max_duration_ms': row['max_duration']
            }

    def _apply_decay(self, conn):
        """Apply temporal decay to all weights."""
        # Calculate decay factor: weight = initial * (0.5 ^ (hours / half_life))
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

            # Remove entries below threshold
            conn.execute("DELETE FROM observations WHERE weight < ?",
                        (ALAN_PRUNE_THRESHOLD,))

            # Enforce max entries (keep highest weight)
            conn.execute("""
                DELETE FROM observations
                WHERE id NOT IN (
                    SELECT id FROM observations
                    ORDER BY weight DESC
                    LIMIT ?
                )
            """, (ALAN_MAX_ENTRIES,))

            # Update last prune time
            conn.execute("""
                INSERT OR REPLACE INTO meta (key, value)
                VALUES ('last_prune', ?)
            """, (datetime.utcnow().isoformat(),))

    def maybe_prune(self):
        """Prune if enough time has passed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_prune'"
            ).fetchone()

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

            return {
                'total_observations': row['total_observations'],
                'unique_patterns': row['unique_patterns'],
                'total_weight': row['total_weight'] or 0,
                'oldest': row['oldest'],
                'newest': row['newest']
            }


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

# Background tasks tracking
background_tasks: dict[str, dict] = {}


# =============================================================================
# Zsh Execution
# =============================================================================

async def execute_zsh(
    command: str,
    timeout: int = NEVERHANG_TIMEOUT_DEFAULT,
    description: Optional[str] = None,
    run_in_background: bool = False
) -> dict:
    """Execute a zsh command with NEVERHANG and A.L.A.N. integration."""

    # Validate timeout
    timeout = min(timeout, NEVERHANG_TIMEOUT_MAX)

    # Check A.L.A.N. for pattern insights
    pattern_stats = alan.get_pattern_stats(command)
    warnings = []

    if pattern_stats.get('known'):
        if pattern_stats['timeout_rate'] > 0.5:
            warnings.append(f"A.L.A.N.: This command pattern has {pattern_stats['timeout_rate']*100:.0f}% timeout rate")
        if pattern_stats.get('max_duration_ms', 0) > timeout * 1000 * 0.8:
            warnings.append(f"A.L.A.N.: Similar commands took up to {pattern_stats['max_duration_ms']/1000:.1f}s")

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

    # Background execution
    if run_in_background:
        task_id = str(uuid.uuid4())[:8]
        output_file = Path(f"/tmp/zsh-tool-{task_id}.out")

        # Start background process
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable='/bin/zsh'
        )

        background_tasks[task_id] = {
            'pid': proc.pid,
            'command': command[:100],
            'started_at': time.time(),
            'output_file': str(output_file),
            'process': proc
        }

        # Fire and forget - collect output in background
        async def collect_output():
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output_file.write_text(stdout.decode('utf-8', errors='replace'))
                background_tasks[task_id]['completed'] = True
                background_tasks[task_id]['exit_code'] = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                output_file.write_text("[TIMEOUT]")
                background_tasks[task_id]['timed_out'] = True

        asyncio.create_task(collect_output())

        return {
            'success': True,
            'background': True,
            'task_id': task_id,
            'output_file': str(output_file),
            'message': f"Background task started. Check output with: cat {output_file}",
            'warnings': warnings if warnings else None
        }

    # Foreground execution
    start_time = time.time()
    timed_out = False

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable='/bin/zsh'
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            timed_out = True
            stdout = b""
            stderr = f"Command timed out after {timeout}s".encode()
            circuit_breaker.record_timeout(alan._hash_command(command))

        duration_ms = int((time.time() - start_time) * 1000)
        exit_code = proc.returncode if not timed_out else -1

        stdout_str = stdout.decode('utf-8', errors='replace')
        stderr_str = stderr.decode('utf-8', errors='replace')

        # Record in A.L.A.N.
        alan.record(command, exit_code, duration_ms, timed_out, stdout_str, stderr_str)
        alan.maybe_prune()

        # Update circuit breaker on success
        if not timed_out:
            circuit_breaker.record_success()

        # Truncate output if needed (match Bash tool behavior)
        if len(stdout_str) > TRUNCATE_OUTPUT_AT:
            stdout_str = stdout_str[:TRUNCATE_OUTPUT_AT] + f"\n\n[OUTPUT TRUNCATED - {len(stdout_str)} chars total]"
        if len(stderr_str) > TRUNCATE_OUTPUT_AT:
            stderr_str = stderr_str[:TRUNCATE_OUTPUT_AT] + f"\n\n[OUTPUT TRUNCATED - {len(stderr_str)} chars total]"

        result = {
            'success': exit_code == 0 and not timed_out,
            'exit_code': exit_code,
            'stdout': stdout_str,
            'stderr': stderr_str if stderr_str else None,
            'duration_ms': duration_ms,
            'timed_out': timed_out
        }

        if warnings:
            result['warnings'] = warnings

        return result

    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'warnings': warnings if warnings else None
        }


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
            description="Execute a zsh command with NEVERHANG circuit breaker and A.L.A.N. learning. Full parity with Bash tool.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The zsh command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds (default: {NEVERHANG_TIMEOUT_DEFAULT}, max: {NEVERHANG_TIMEOUT_MAX})"
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what this command does"
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Run command in background, returns task_id for later retrieval"
                    }
                },
                "required": ["command"]
            }
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

    if name == "zsh":
        result = await execute_zsh(
            command=arguments["command"],
            timeout=arguments.get("timeout", NEVERHANG_TIMEOUT_DEFAULT),
            description=arguments.get("description"),
            run_in_background=arguments.get("run_in_background", False)
        )
        # Format zsh output cleanly - stdout first, then metadata
        return _format_zsh_output(result)
    elif name == "zsh_health":
        result = {
            'status': 'healthy',
            'neverhang': circuit_breaker.get_status(),
            'alan': alan.get_stats(),
            'background_tasks': len(background_tasks)
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


def _format_zsh_output(result: dict) -> list[TextContent]:
    """Format zsh execution output cleanly."""
    parts = []

    # stdout first - clean, no escaping
    stdout = result.get('stdout', '')
    if stdout:
        parts.append(stdout.rstrip('\n'))

    # stderr if present
    stderr = result.get('stderr')
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip()}")

    # Error message if failed
    if not result.get('success', True):
        error = result.get('error')
        if error:
            parts.append(f"[error] {error}")

    # Metadata line for non-success or special conditions
    meta = []
    if result.get('timed_out'):
        meta.append("TIMEOUT")
    if result.get('exit_code', 0) != 0:
        meta.append(f"exit={result.get('exit_code')}")
    if result.get('warnings'):
        meta.append(f"warnings={result['warnings']}")
    if result.get('background'):
        meta.append(f"task_id={result.get('task_id')}")
        meta.append(f"output_file={result.get('output_file')}")

    if meta:
        parts.append(f"[{', '.join(meta)}]")

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


if __name__ == '__main__':
    asyncio.run(main())