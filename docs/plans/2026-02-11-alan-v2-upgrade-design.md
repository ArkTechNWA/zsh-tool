# A.L.A.N. v2 Upgrade — Consolidated Design

**Project:** zsh-tool
**Version target:** 0.5.0
**Date:** 2026-02-11
**Status:** Design

---

## Overview

Three interconnected enhancements to A.L.A.N.'s execution oversight:

| Feature | Summary | Touches |
|---------|---------|---------|
| **Intelligent Polling** | Listen window, poll metadata, adaptive suggestions | `tasks.py`, `alan.py`, `server.py` |
| **Kill-Aware A.L.A.N.** | KILLED outcome type, kill classification, kill insights | `tasks.py`, `alan.py`, schema |
| **manopt Integration** | Async man-page option hints on repeated failures | `tasks.py`, `alan.py`, `config.py`, `scripts/manopt` |

---

## 1. Intelligent Polling

### Problem

`zsh_poll` returns instantly. The agent calls it every ~3 seconds. A 2-minute `pip install` generates ~40 empty round-trips, each costing tokens. The tool has no mechanism to say "nothing's happening yet."

### 1.1 Brief Listen Window

`poll_task()` currently calls `_build_task_response(task)` and returns immediately (`tasks.py:435`).

**Change:** Before returning, wait up to **2 seconds** for new output to appear. If output arrives within the window, return it immediately. If not, return with empty output plus metadata. The agent is never trapped — 2s is well under any timeout.

```python
async def poll_task(task_id: str) -> dict:
    if task_id not in live_tasks:
        return {'error': f'Unknown task: {task_id}', 'success': False}

    task = live_tasks[task_id]

    # Listen window: wait up to 2s for new output
    if task.status == "running":
        start_pos = len(task.output_buffer)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if len(task.output_buffer) > start_pos or task.status != "running":
                break
            await asyncio.sleep(0.1)

    return _build_task_response(task)
```

### 1.2 LiveTask Polling State

Add fields to `LiveTask` dataclass to track polling cadence:

```python
@dataclass
class LiveTask:
    # ... existing fields ...
    last_output_time: float = 0.0        # When output last grew
    poll_count: int = 0                   # Total polls for this task
    polls_since_output: int = 0           # Polls since last new output
```

`last_output_time` gets updated in `_output_collector` / `_pty_output_collector` whenever new data arrives. `poll_count` and `polls_since_output` get incremented/reset in `poll_task()`.

### 1.3 Poll Metadata

Every `zsh_poll` response for a running task includes `poll_meta`:

```json
{
  "task_id": "abc123",
  "status": "running",
  "output": "",
  "poll_meta": {
    "polls_since_output": 5,
    "elapsed_since_last_output_s": 18.2,
    "total_elapsed_s": 34.7,
    "alan_estimate": {
      "command_template": "pip install *",
      "median_duration_s": 47,
      "p90_duration_s": 180,
      "completion_probability": 0.35,
      "sample_size": 12
    },
    "suggestion": null
  }
}
```

**`alan_estimate`** is fetched **once per task** (first poll) and cached on the `LiveTask`. It's a single SQLite query against the `observations` table for the command's template.

### 1.4 A.L.A.N. Duration Estimate Query

New method on `ALAN`:

```python
def get_duration_estimate(self, command: str) -> dict | None:
    """Get duration statistics for a command template."""
    command_template = self._template_command(command)

    with self._connect() as conn:
        rows = conn.execute("""
            SELECT duration_ms FROM observations
            WHERE command_template = ? AND duration_ms > 0
            ORDER BY duration_ms
        """, (command_template,)).fetchall()

    if len(rows) < 3:
        return None  # Not enough data

    durations = [r['duration_ms'] / 1000 for r in rows]
    n = len(durations)
    median = durations[n // 2]
    p90_idx = int(n * 0.9)
    p90 = durations[min(p90_idx, n - 1)]

    return {
        'command_template': command_template,
        'median_duration_s': round(median, 1),
        'p90_duration_s': round(p90, 1),
        'sample_size': n,
    }
```

**`completion_probability`** is computed dynamically per poll:

```python
probability = len([d for d in durations if d <= elapsed]) / len(durations)
```

### 1.5 Adaptive Suggestions

Built in `_build_task_response` when status is `running`. Logic:

| Condition | Suggestion |
|-----------|------------|
| New output just arrived | `null` — natural flow |
| 1–2 polls, no new output | `null` — too early |
| 3–5 polls idle, A.L.A.N. has estimate | `"No output for Xs. Median completion for '{template}' is Ys ({elapsed}s elapsed). Consider spacing polls."` |
| 3–5 polls idle, no A.L.A.N. data | `"No output for Xs. Consider spacing polls wider."` |
| 10+ polls idle | `"No output for Xs across N polls. Command may be hung. Consider zsh_kill."` |
| Elapsed approaching A.L.A.N. median (>80%) | `"Nearing typical completion — poll again soon."` |

Suggestions are advisory text only. The agent always decides.

### 1.6 Reset Behavior

- `polls_since_output` resets to 0 when new output arrives during the listen window or between polls
- Suggestion cadence resets with new output
- A.L.A.N. estimate is fetched once per task, cached on `LiveTask`

---

## 2. Kill-Aware A.L.A.N.

### Problem

`kill_task()` (`tasks.py:472`) is fire-and-forget. A.L.A.N. never records it. The agent kills a command and that decision — and when it was made relative to expected duration — is lost.

### 2.1 Outcome Type

Currently there's no `outcome_type` — A.L.A.N. uses `exit_code` + `timed_out` boolean. Add a proper outcome classification:

```
SUCCESS | FAILURE | TIMEOUT | KILLED
```

**Schema migration** — add two columns to `observations`:

```python
self._migrate_add_column(conn, 'observations', 'outcome_type', 'TEXT')
self._migrate_add_column(conn, 'observations', 'kill_elapsed_ms', 'INTEGER')
```

Also add to `recent_commands`:

```python
self._migrate_add_column(conn, 'recent_commands', 'outcome_type', 'TEXT')
```

Backward compatible — nullable columns, no migration needed for existing data. Existing observations with `timed_out=1` are implicitly `TIMEOUT`; others with `exit_code=0` are `SUCCESS`, rest are `FAILURE`.

### 2.2 Kill Signal Flow

Current `kill_task()` does:
1. Kill process
2. Set `task.status = "killed"`
3. `_cleanup_task()` — removes from registry
4. Return `{'success': True, 'message': ...}`

A.L.A.N. never sees it because `_cleanup_task` deletes the task before the output collector can record.

**Change:** Before cleanup, record to A.L.A.N.:

```python
async def kill_task(task_id: str) -> dict:
    # ... existing validation ...

    # Capture elapsed BEFORE killing
    elapsed_ms = int((time.time() - task.started_at) * 1000)

    # ... existing kill logic ...

    task.status = "killed"

    # Record to A.L.A.N. as KILLED outcome
    alan.record(
        task.command,
        exit_code=-9,          # Signal: SIGKILL
        duration_ms=elapsed_ms,
        timed_out=False,
        stdout=task.output_buffer[:500],
        stderr="",
        pipestatus=None,
        outcome_type="KILLED",
        kill_elapsed_ms=elapsed_ms,
    )

    # Get kill-specific insight for response
    kill_insight = alan.get_kill_insight(task.command, elapsed_ms)

    result = {'success': True, 'message': f'Task {task_id} killed'}
    if kill_insight:
        result['insight'] = kill_insight

    _cleanup_task(task_id)
    return result
```

### 2.3 `alan.record()` Changes

Add optional parameters:

```python
def record(self, command, exit_code, duration_ms, timed_out=False,
           stdout="", stderr="", pipestatus=None,
           outcome_type=None, kill_elapsed_ms=None):
```

If `outcome_type` is not explicitly provided, derive it:
```python
if outcome_type is None:
    if timed_out:
        outcome_type = "TIMEOUT"
    elif exit_code == 0:
        outcome_type = "SUCCESS"
    else:
        outcome_type = "FAILURE"
```

### 2.4 Kill Classification

New method on `ALAN`:

```python
def get_kill_insight(self, command: str, kill_elapsed_ms: int) -> dict | None:
    """Classify a kill and generate insight."""
    estimate = self.get_duration_estimate(command)
    template = self._template_command(command)

    # Get kill history for this template
    with self._connect() as conn:
        kill_stats = conn.execute("""
            SELECT COUNT(*) as kills FROM observations
            WHERE command_template = ? AND outcome_type = 'KILLED'
        """, (template,)).fetchone()

        total_stats = conn.execute("""
            SELECT COUNT(*) as total FROM observations
            WHERE command_template = ?
        """, (template,)).fetchone()

    total = total_stats['total'] if total_stats else 0
    kills = kill_stats['kills'] if kill_stats else 0
    kill_rate = kills / total if total > 0 else 0

    result = {'template': template, 'kill_elapsed_ms': kill_elapsed_ms}

    # Pattern-level problem overrides individual classification
    if kill_rate > 0.5 and kills >= 3:
        result['category'] = 'PATTERN_PROBLEM'
        result['message'] = (
            f"'{template}' gets killed {kill_rate*100:.0f}% of the time "
            f"({kills}/{total}). This pattern may need a different approach."
        )
        return result

    if estimate is None:
        result['category'] = 'UNKNOWN'
        result['message'] = f"Killed after {kill_elapsed_ms/1000:.1f}s. Not enough history to classify."
        return result

    median_ms = estimate['median_duration_s'] * 1000
    ratio = kill_elapsed_ms / median_ms if median_ms > 0 else 1.0

    if ratio < 0.5:
        result['category'] = 'EARLY_KILL'
        result['message'] = (
            f"Killed '{template}' at {kill_elapsed_ms/1000:.1f}s. "
            f"Median completion is {estimate['median_duration_s']}s. "
            f"This command likely needs more time."
        )
    elif ratio > 2.0:
        result['category'] = 'LATE_KILL'
        result['message'] = (
            f"Killed '{template}' after {kill_elapsed_ms/1000:.1f}s. "
            f"Median is {estimate['median_duration_s']}s. "
            f"Something is wrong — this isn't normal duration."
        )
    else:
        result['category'] = 'NORMAL_KILL'
        result['message'] = (
            f"Killed '{template}' at {kill_elapsed_ms/1000:.1f}s "
            f"(median: {estimate['median_duration_s']}s)."
        )

    return result
```

### 2.5 Kill ↔ Streak Interaction

Kills break success streaks. In `_update_streak`, a KILLED outcome is treated as `success=0`.

Consecutive kills on the same template are tracked via `outcome_type` in `recent_commands` — if 3+ kills appear for the same template in the session, the PATTERN_PROBLEM insight fires.

### 2.6 Kill Response Formatting

In `server.py`, `_format_task_output` already handles `status == "killed"`. The kill insight from `get_kill_insight()` gets formatted into the response by `kill_task()` directly (not through `_format_task_output`, since the task is already cleaned up).

Update the `zsh_kill` handler in `server.py` to format kill insights with ANSI coloring:

```python
elif name == "zsh_kill":
    result = await kill_task(arguments["task_id"])
    # Format kill insight if present
    if 'insight' in result:
        insight = result['insight']
        cat = insight.get('category', '')
        msg = insight.get('message', '')
        if cat in ('EARLY_KILL', 'PATTERN_PROBLEM'):
            result['formatted_insight'] = f"{C_YELLOW}[A.L.A.N.: {msg}]{C_RESET}"
        elif cat == 'LATE_KILL':
            result['formatted_insight'] = f"{C_RED}[A.L.A.N.: {msg}]{C_RESET}"
        else:
            result['formatted_insight'] = f"{C_DIM}[A.L.A.N.: {msg}]{C_RESET}"
```

---

## 3. manopt Integration

### Problem

When a command fails, the agent often retries with different flags blindly. The `scripts/manopt` script already parses man pages into compact option tables — but it's not connected to A.L.A.N.

### 3.1 Behavior

| Fail # (same template) | Action |
|------------------------|--------|
| 1st | Normal A.L.A.N. feedback. No manopt. |
| 2nd | Trigger `manopt` **async in background**. Don't wait. Don't present. |
| 3rd+ | Present cached manopt output in A.L.A.N. insight if available. |

**Rationale:**
- 1st fail could be transient, typo, environmental — not worth the overhead.
- 2nd fail signals a pattern — start loading proactively while the agent retries.
- 3rd fail — you clearly need help. Here's every flag the command supports.

### 3.2 Architecture

`manopt` stays as an external script. It's invoked via `asyncio.create_subprocess_exec` with a **2-second timeout**.

**Cache location:** A.L.A.N. SQLite, new table:

```sql
CREATE TABLE IF NOT EXISTS manopt_cache (
    base_command TEXT PRIMARY KEY,
    options_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

No decay, no pruning — man pages don't change between sessions. Cache persists until the entry is explicitly invalidated (e.g., package update changes man page). This is fine because the table will be small (one row per unique base command) and the data is static.

### 3.3 Fail Counting

A.L.A.N. already tracks recent failures per command hash/template in `recent_commands`. The fail count for manopt triggering uses `recent_commands` filtered to the current session and same `command_template`:

```python
def _get_template_fail_count(self, command: str) -> int:
    """Count consecutive recent failures for a command template."""
    template = self._template_command(command)
    with self._connect() as conn:
        rows = conn.execute("""
            SELECT success FROM recent_commands
            WHERE session_id = ? AND command_template = ?
            ORDER BY timestamp DESC LIMIT 10
        """, (self.session_id, template)).fetchall()

    count = 0
    for row in rows:
        if row['success']:
            break
        count += 1
    return count
```

### 3.4 manopt Trigger

Called from `alan.record()` when a failure is recorded:

```python
# In record(), after inserting into recent_commands:
if exit_code != 0 and not timed_out:
    fail_count = self._get_template_fail_count(command)
    if fail_count == 1:  # This is the 2nd fail (0-indexed after insert)
        self._trigger_manopt_async(command)
```

The async trigger:

```python
def _trigger_manopt_async(self, command: str):
    """Fire-and-forget async manopt lookup."""
    base_cmd = command.split()[0].split("/")[-1] if command.strip() else ""
    if not base_cmd:
        return

    # Skip if already cached
    with self._connect() as conn:
        cached = conn.execute(
            "SELECT 1 FROM manopt_cache WHERE base_command = ?",
            (base_cmd,)
        ).fetchone()
    if cached:
        return

    # Find manopt script
    manopt_path = self._find_manopt_script()
    if not manopt_path:
        return

    # Launch async — fire and forget
    asyncio.ensure_future(self._run_manopt(base_cmd, manopt_path))
```

```python
async def _run_manopt(self, base_cmd: str, manopt_path: str):
    """Run manopt with 2s timeout, cache result."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, manopt_path, base_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

        if proc.returncode == 0 and stdout:
            text = stdout.decode('utf-8', errors='replace')
            with self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO manopt_cache
                    (base_command, options_text, created_at)
                    VALUES (?, ?, ?)
                """, (base_cmd, text, datetime.now(timezone.utc).isoformat()))
    except (asyncio.TimeoutError, Exception):
        pass  # manopt too slow or failed — silently skip
```

### 3.5 manopt Presentation

On 3rd+ fail, `get_insights()` checks the cache:

```python
# In get_insights(), after retry detection:
if recent.get('recent_failures', 0) >= 2:
    base_cmd = command.split()[0].split("/")[-1] if command.strip() else ""
    if base_cmd:
        manopt_text = self._get_cached_manopt(base_cmd)
        if manopt_text:
            insights.append(("info", f"Options for {base_cmd}:\n{manopt_text}"))
```

### 3.6 manopt Script Location

The script lives at `scripts/manopt` in the repo. At install time (pip), it needs to be findable. Strategy:

1. **Package data**: Include `scripts/manopt` as package data in `pyproject.toml`
2. **Runtime lookup**: `importlib.resources` to find it, falling back to `PATH` lookup

```python
def _find_manopt_script(self) -> str | None:
    """Locate the manopt script."""
    # 1. Package resource
    try:
        from importlib.resources import files
        pkg_script = files('zsh_tool') / 'scripts' / 'manopt'
        if pkg_script.is_file():
            return str(pkg_script)
    except Exception:
        pass

    # 2. Relative to this file (dev mode)
    dev_path = Path(__file__).parent.parent / 'scripts' / 'manopt'
    if dev_path.exists():
        return str(dev_path)

    # 3. PATH lookup
    import shutil
    return shutil.which('manopt')
```

### 3.7 ENV Configuration

```python
# config.py
ALAN_MANOPT_ENABLED = os.environ.get("ALAN_MANOPT_ENABLED", "1").lower() not in ("0", "false", "no", "off")
ALAN_MANOPT_TIMEOUT = float(os.environ.get("ALAN_MANOPT_TIMEOUT", "2.0"))
ALAN_MANOPT_FAIL_TRIGGER = int(os.environ.get("ALAN_MANOPT_FAIL_TRIGGER", "2"))  # Async load on Nth fail
ALAN_MANOPT_FAIL_PRESENT = int(os.environ.get("ALAN_MANOPT_FAIL_PRESENT", "3"))  # Present on Nth fail
```

On by default. Disable with `ALAN_MANOPT_ENABLED=0`.

---

## Integration Points

### Polling ↔ A.L.A.N.

- `poll_task` queries `get_duration_estimate()` once per task (first poll), caches on `LiveTask`
- Poll count and idle duration are tracked on `LiveTask`, reported in `poll_meta`
- Duration estimates improve as more observations accumulate

### Kill ↔ Polling

- If polling detected extended idle before a kill, that context enriches the kill insight
- The kill response can reference polling history: "Command produced no output for 45s before kill"
- `LiveTask.polls_since_output` and `last_output_time` are available at kill time

### Kill ↔ Streaks

- Kills count as failures for streak tracking (`success=0`)
- Consecutive kills on same template trigger PATTERN_PROBLEM
- Kill streaks are visible through `outcome_type` queries on `recent_commands`

### manopt ↔ Kill

- If a command is killed (not failed), manopt does NOT trigger — kills aren't flag problems
- manopt only triggers on `FAILURE` outcomes (non-zero exit, not timeout, not killed)

### manopt ↔ Polling

- No direct interaction. manopt is command-failure-driven, polling is execution-time-driven.

---

## Files Changed

| File | Changes |
|------|---------|
| `zsh_tool/config.py` | Add `ALAN_MANOPT_*` env vars |
| `zsh_tool/alan.py` | `get_duration_estimate()`, `get_kill_insight()`, `_get_template_fail_count()`, `_trigger_manopt_async()`, `_run_manopt()`, `_find_manopt_script()`, `_get_cached_manopt()`, `manopt_cache` table, `outcome_type`/`kill_elapsed_ms` columns, `record()` signature change |
| `zsh_tool/tasks.py` | `LiveTask` new fields, `poll_task()` listen window + metadata, `kill_task()` A.L.A.N. recording + insight, `_build_task_response()` poll_meta generation |
| `zsh_tool/server.py` | `zsh_kill` handler formats kill insight, `zsh_poll` tool description updated |
| `scripts/manopt` | Move to `zsh_tool/scripts/manopt` (or include as package data) |
| `pyproject.toml` | Package data for manopt script, version bump to 0.5.0 |
| `tests/` | New tests for all three features |

---

## Schema Summary

### New Table
```sql
CREATE TABLE IF NOT EXISTS manopt_cache (
    base_command TEXT PRIMARY KEY,
    options_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

### New Columns (migrations via `_migrate_add_column`)
```sql
-- observations
ALTER TABLE observations ADD COLUMN outcome_type TEXT;
ALTER TABLE observations ADD COLUMN kill_elapsed_ms INTEGER;

-- recent_commands
ALTER TABLE recent_commands ADD COLUMN outcome_type TEXT;
```

### New Indexes
```sql
CREATE INDEX IF NOT EXISTS idx_outcome_type ON observations(outcome_type);
CREATE INDEX IF NOT EXISTS idx_recent_outcome ON recent_commands(outcome_type);
```

---

## Test Coverage

| Area | Cases |
|------|-------|
| **Poll listen window** | Output arrives within 2s; output arrives after 2s; no output; task completes during listen |
| **Poll metadata** | Generated with A.L.A.N. data; generated without; completion probability math |
| **Suggestions** | Each idle threshold; reset on new output; estimate-aware tightening |
| **Kill recording** | KILLED observation created; elapsed time captured; exit_code = -9 |
| **Kill classification** | EARLY_KILL, LATE_KILL, NORMAL_KILL, PATTERN_PROBLEM, insufficient data |
| **Kill insights** | Message text at each category; kill rate calculation |
| **Kill + streaks** | Kill breaks success streak; consecutive kills counted |
| **manopt trigger** | 1st fail = no trigger; 2nd fail = async trigger; already cached = skip |
| **manopt cache** | Cache hit; cache miss; script not found; script timeout |
| **manopt presentation** | 3rd fail shows cached; no cache = no insight; disabled via env |
| **Integration** | Poll → kill → A.L.A.N. chain; manopt only on FAILURE not KILLED/TIMEOUT |

---

## Non-Goals

- **No blocking polls.** The 2s listen window is I/O efficiency, not agent imprisonment.
- **No automatic interval enforcement.** Suggestions only. The agent decides.
- **No kill prevention.** A.L.A.N. advises, never blocks.
- **No external dependencies.** All estimates from local observation history.
- **No manopt on kill/timeout.** Flag hints are for command failures, not timing issues.

---

*"Maybe you're fuckin' up, maybe you're doing it right."*
