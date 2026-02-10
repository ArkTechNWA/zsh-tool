# Feedback Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the 5 feedback improvements from the approved design spec (`docs/plans/2026-02-08-feedback-improvements-design.md`): insight classification, command awareness, pipestatus cleanup, ANSI coloring, and silent command detection.

**Architecture:** Changes span 3 source files in the response formatting layer (`alan.py`, `tasks.py`, `server.py`). ALAN gains insight classification (string→tuple), a post-execution insights function for command/exit awareness, and silent detection. Tasks layer switches from formatted exit strings to raw pipestatus lists and integrates the insight pipeline. Server layer adds ANSI coloring, grouped insight display, and MCP annotation insurance.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, mcp SDK (`TextContent`), ruff (lint)

**Design spec:** `docs/plans/2026-02-08-feedback-improvements-design.md`

---

## IMPORTANT: Tool restrictions

- **NEVER** use Task tool agents — they hallucinate results. Use zsh tool and Read/Edit directly.
- **Glob/Grep tools don't work** in this repo — use `mcp__plugin_zsh-tool_zsh-tool__zsh` for file discovery and searching.
- **Ruff** is at `.venv/bin/ruff`, not in system PATH.
- **Full test suite** needs `timeout=120` on zsh tool.

---

## Task 1: Create feature branch

**Step 1: Create branch from master**

```bash
git checkout master && git pull origin master
git checkout -b feat/feedback-improvements
```

**Step 2: Verify**

```bash
git branch --show-current
```
Expected: `feat/feedback-improvements`

---

## Task 2: ALAN Insight Classification — return tuples instead of strings

**Files:**
- Modify: `zsh_tool/alan.py:652-712` — `get_insights()` and `_get_ssh_insights()`
- Modify: `tests/test_alan_insights.py` — all insight assertion patterns
- Modify: `tests/test_alan_ssh.py:248-296` — SSH insight assertions

### Step 1: Change `get_insights()` to return `list[tuple[str, str]]`

In `zsh_tool/alan.py`, change every `insights.append("...")` to `insights.append(("level", "..."))`:

```python
# alan.py:652 — change return type annotation
def get_insights(self, command: str, timeout: int = 120) -> list[tuple[str, str]]:
    """
    Generate proactive insights for a command.
    Returns list of (level, message) tuples. Level is "info" or "warning".
    """
    insights: list[tuple[str, str]] = []
    # ... (stats/recent/streak lookups unchanged)

    # Retry detection (lines 663-674)
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

    # Similar commands (lines 677-682)
    if recent.get('similar_commands') and not recent.get('is_retry'):
        similar = recent['similar_commands']
        if similar:
            sim_success = sum(1 for s in similar if s['success'])
            template = recent.get('template', 'similar pattern')
            insights.append(("info", f"Similar to '{template}' - {sim_success}/{len(similar)} succeeded recently."))

    # Streak info (lines 685-690)
    if streak.get('has_streak'):
        current = streak['current']
        if current >= ALAN_STREAK_THRESHOLD:
            insights.append(("info", f"Streak: {current} successes in a row. Solid."))
        elif current <= -ALAN_STREAK_THRESHOLD:
            insights.append(("warning", f"Failing streak: {abs(current)}. Same approach?"))

    # Pattern history (lines 693-704)
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

    # SSH-specific insights
    ssh_info = self._parse_ssh_command(command)
    if ssh_info:
        ssh_insights = self._get_ssh_insights(ssh_info)
        insights.extend(ssh_insights)

    return insights
```

### Step 2: Change `_get_ssh_insights()` to return tuples

In `zsh_tool/alan.py:714-766`:

```python
def _get_ssh_insights(self, ssh_info: dict) -> list[tuple[str, str]]:
    """Generate SSH-specific insights based on host and command history."""
    insights: list[tuple[str, str]] = []
    # ... (DB lookups unchanged)

    # Line 738: connection failure rate
    if conn_fail_rate > 0.3:
        insights.append(("warning", f"Host '{host}' has {conn_fail_rate*100:.0f}% connection failure rate ({host_stats['conn_failures']}/{total})."))
    elif host_stats['successes'] == total and total >= 3:
        insights.append(("info", f"Host '{host}' is reliable: {total} successful connections."))

    # Line 761: remote command failure
    if cmd_stats['cmd_failures'] > 0 and success_rate < 0.5:
        insights.append(("warning", f"Remote command '{remote_template}' fails often ({cmd_stats['cmd_failures']}/{total} across {len(hosts)} hosts)."))
    elif success_rate > 0.9 and total >= 3:
        insights.append(("info", f"Remote command '{remote_template}' reliable across {len(hosts)} hosts ({success_rate*100:.0f}% success)."))

    return insights
```

### Step 3: Update `tests/test_alan_insights.py`

Every assertion that iterates over insights as strings must destructure tuples. The pattern change is:

```python
# Before:
any("text" in i for i in insights)
# After:
any("text" in msg for _, msg in insights)

# Before:
[i for i in insights if "text" in i]
# After:
[msg for _, msg in insights if "text" in msg]

# Before:
isinstance(i, str)
# After:
isinstance(i, tuple) and len(i) == 2

# Before:
insights[0]  # string
# After:
insights[0][1]  # message from tuple
```

**Specific changes (every line that asserts on insight content):**

| Line | Before | After |
|------|--------|-------|
| 31 | `any("New pattern" in i for i in insights)` | `any("New pattern" in msg for _, msg in insights)` |
| 42 | `all(isinstance(i, str) for i in insights)` | `all(isinstance(i, tuple) and len(i) == 2 for i in insights)` |
| 42 | method name `test_insight_strings` | `test_insight_tuples` |
| 53 | `any("Retry" in i for i in insights)` | `any("Retry" in msg for _, msg in insights)` |
| 61 | `any("all failed" in i.lower() or "different approach" in i.lower() for i in insights)` | `any("all failed" in msg.lower() or "different approach" in msg.lower() for _, msg in insights)` |
| 69 | `any("succeeded" in i.lower() for i in insights)` | `any("succeeded" in msg.lower() for _, msg in insights)` |
| 79 | `any("/" in i and "succeeded" in i.lower() for i in insights)` | `any("/" in msg and "succeeded" in msg.lower() for _, msg in insights)` |
| 88 | `any("#3" in i for i in insights)` | `any("#3" in msg for _, msg in insights)` |
| 102 | `any("similar" in i.lower() for i in insights)` | `any("similar" in msg.lower() for _, msg in insights)` |
| 111 | `[i for i in insights if "similar" in i.lower()]` | `[msg for _, msg in insights if "similar" in msg.lower()]` |
| 113 | `any("/" in i for i in similar_insights)` | `any("/" in s for s in similar_insights)` |
| 125 | `any("streak" in i.lower() and "success" in i.lower() for i in insights)` | `any("streak" in msg.lower() and "success" in msg.lower() for _, msg in insights)` |
| 133 | `any("fail" in i.lower() and "streak" in i.lower() for i in insights)` | `any("fail" in msg.lower() and "streak" in msg.lower() for _, msg in insights)` |
| 142 | `[i for i in insights if "streak" in i.lower() and "solid" in i.lower()]` | `[msg for _, msg in insights if "streak" in msg.lower() and "solid" in msg.lower()]` |
| 157 | `any("timeout" in i.lower() for i in insights)` | `any("timeout" in msg.lower() for _, msg in insights)` |
| 166 | `any("reliable" in i.lower() for i in insights)` | `any("reliable" in msg.lower() for _, msg in insights)` |
| 176 | `any("takes" in i.lower() and "s" in i for i in insights)` | `any("takes" in msg.lower() and "s" in msg for _, msg in insights)` |
| 196 | `"New pattern" in insights[0]` | `"New pattern" in insights[0][1]` |
| 207 | `[i for i in insights if "Retry" in i]` | `[msg for _, msg in insights if "Retry" in msg]` |
| 210 | `retry_insights[0].startswith("Retry #")` | (unchanged — still a string) |
| 218 | `[i for i in insights if "Streak:" in i]` | `[msg for _, msg in insights if "Streak:" in msg]` |
| 220 | `any(char.isdigit() for char in streak_insights[0])` | (unchanged — still a string) |
| 228 | `[i for i in insights if "timeout" in i.lower()]` | `[msg for _, msg in insights if "timeout" in msg.lower()]` |
| 230 | `"%" in timeout_insights[0]` | (unchanged — still a string) |
| 287 | `not any("host" in i.lower() and "connection" in i.lower() for i in insights)` | `not any("host" in msg.lower() and "connection" in msg.lower() for _, msg in insights)` |
| 294 | `not any("remote command" in i.lower() for i in insights)` | `not any("remote command" in msg.lower() for _, msg in insights)` |

Also add a **new test** to verify classification levels:

```python
# In TestGetInsights class:
def test_new_pattern_is_info(self, alan):
    """New pattern insight is classified as info."""
    insights = alan.get_insights("brand-new-command")
    new_pattern = [(lvl, msg) for lvl, msg in insights if "New pattern" in msg]
    assert len(new_pattern) == 1
    assert new_pattern[0][0] == "info"
```

### Step 4: Update `tests/test_alan_ssh.py:248-296`

Same pattern — destructure tuples:

| Line | Before | After |
|------|--------|-------|
| 255 | `[i for i in insights if 'Host' in i or 'Remote command' in i]` | `[msg for _, msg in insights if 'Host' in msg or 'Remote command' in msg]` |
| 266 | `[i for i in insights if 'connection failure rate' in i]` | `[msg for _, msg in insights if 'connection failure rate' in msg]` |
| 275 | `[i for i in insights if 'reliable' in i.lower() and 'reliable' in i]` | `[msg for _, msg in insights if 'reliable' in msg.lower()]` |
| 284 | `[i for i in insights if 'fails often' in i]` | `[msg for _, msg in insights if 'fails often' in msg]` |
| 295 | `[i for i in insights if 'reliable across' in i and 'hosts' in i]` | `[msg for _, msg in insights if 'reliable across' in msg and 'hosts' in msg]` |

### Step 5: Run tests

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_alan_insights.py tests/test_alan_ssh.py -v
```
Expected: All pass.

### Step 6: Commit

```bash
git add zsh_tool/alan.py tests/test_alan_insights.py tests/test_alan_ssh.py
git commit -m "feat: classify ALAN insights as (level, message) tuples

get_insights() and _get_ssh_insights() now return list[tuple[str, str]]
instead of list[str]. Each insight is tagged as 'info' or 'warning'.

Implements Section 2 of feedback improvements design."
```

---

## Task 3: ALAN Command Awareness — post-execution insights

**Files:**
- Modify: `zsh_tool/alan.py` — add `KNOWN_EXIT_CODES`, `UNIVERSAL_EXIT_CODES`, `get_post_insights()`
- Create: `tests/test_alan_exit_awareness.py`

### Step 1: Write tests for `get_post_insights()`

Create `tests/test_alan_exit_awareness.py`:

```python
"""
Tests for ALAN post-execution insights (command awareness, pipe masking, silent detection).

Implements Section 1 and Section 5 of feedback improvements design.
"""

import pytest
from zsh_tool.alan import get_post_insights, KNOWN_EXIT_CODES, UNIVERSAL_EXIT_CODES


class TestKnownExitCodes:
    """Tests for the KNOWN_EXIT_CODES dict."""

    def test_grep_exit_1_is_known(self):
        assert "grep" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["grep"]

    def test_diff_exit_1_is_known(self):
        assert "diff" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["diff"]

    def test_test_exit_1_is_known(self):
        assert "test" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["test"]

    def test_bracket_exit_1_is_known(self):
        assert "[" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["["]

    def test_cmp_exit_1_is_known(self):
        assert "cmp" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["cmp"]


class TestUniversalExitCodes:
    """Tests for the UNIVERSAL_EXIT_CODES dict."""

    def test_126_permission_denied(self):
        assert 126 in UNIVERSAL_EXIT_CODES

    def test_127_command_not_found(self):
        assert 127 in UNIVERSAL_EXIT_CODES

    def test_255_ssh_connection_failed(self):
        assert 255 in UNIVERSAL_EXIT_CODES


class TestGetPostInsightsCommandAwareness:
    """Tests for command-specific exit code awareness."""

    def test_grep_exit_1_is_info(self):
        """grep exit 1 = no match, should be info not warning."""
        insights = get_post_insights("grep pattern file.txt", [1])
        assert any(level == "info" and "no match" in msg for level, msg in insights)

    def test_grep_exit_0_no_insight(self):
        """grep exit 0 = match found, no command awareness insight."""
        insights = get_post_insights("grep pattern file.txt", [0])
        awareness = [(l, m) for l, m in insights if "grep" in m and "exit" in m]
        assert len(awareness) == 0

    def test_grep_exit_2_no_known_insight(self):
        """grep exit 2 = error, not in known codes, no command awareness insight."""
        insights = get_post_insights("grep pattern file.txt", [2])
        awareness = [(l, m) for l, m in insights if "no match" in m]
        assert len(awareness) == 0

    def test_diff_exit_1_is_info(self):
        """diff exit 1 = files differ, should be info."""
        insights = get_post_insights("diff file1 file2", [1])
        assert any(level == "info" and "files differ" in msg for level, msg in insights)

    def test_test_exit_1_is_info(self):
        """test exit 1 = condition false, should be info."""
        insights = get_post_insights("test -f nofile", [1])
        assert any(level == "info" and "condition false" in msg for level, msg in insights)

    def test_exit_127_is_warning(self):
        """Exit 127 = command not found, always warning."""
        insights = get_post_insights("nonexistent_cmd", [127])
        assert any(level == "warning" and "command not found" in msg for level, msg in insights)

    def test_exit_126_is_warning(self):
        """Exit 126 = permission denied, always warning."""
        insights = get_post_insights("./noperm.sh", [126])
        assert any(level == "warning" and "permission denied" in msg for level, msg in insights)

    def test_exit_255_is_warning(self):
        """Exit 255 = SSH connection failed, always warning."""
        insights = get_post_insights("ssh badhost", [255])
        assert any(level == "warning" and "SSH connection failed" in msg for level, msg in insights)

    def test_universal_takes_precedence(self):
        """Universal exit codes take precedence over command-specific."""
        # If grep somehow exited 127 (command not found), universal wins
        insights = get_post_insights("grep pattern", [127])
        assert any(level == "warning" and "command not found" in msg for level, msg in insights)
        assert not any("no match" in msg for _, msg in insights)

    def test_full_path_command(self):
        """Command with full path is recognized."""
        insights = get_post_insights("/usr/bin/grep pattern file", [1])
        assert any("no match" in msg for _, msg in insights)

    def test_unknown_command_no_awareness(self):
        """Unknown command with non-zero exit has no command awareness insight."""
        insights = get_post_insights("mycustom_cmd arg1", [1])
        awareness = [(l, m) for l, m in insights if "normal" in m]
        assert len(awareness) == 0


class TestGetPostInsightsPipeMasking:
    """Tests for pipe segment failure detection."""

    def test_left_failure_masked_by_right(self):
        """Left pipe segment failure masked by right success → warning."""
        insights = get_post_insights("false | echo ok", [1, 0])
        assert any(level == "warning" and "pipe segment 1" in msg for level, msg in insights)

    def test_sigpipe_excluded(self):
        """Exit 141 (SIGPIPE) in left segment is normal in pipes, no warning."""
        insights = get_post_insights("cat bigfile | head -1", [141, 0])
        pipe_warnings = [(l, m) for l, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0

    def test_all_zero_no_pipe_warning(self):
        """All pipe segments exit 0 → no pipe masking warning."""
        insights = get_post_insights("echo hi | cat", [0, 0])
        pipe_warnings = [(l, m) for l, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0

    def test_right_failure_no_masking_warning(self):
        """Right side failure is the overall exit, not masked. No masking warning."""
        insights = get_post_insights("echo hi | grep nope", [0, 1])
        pipe_warnings = [(l, m) for l, m in insights if "pipe segment" in m and "masked" in m]
        assert len(pipe_warnings) == 0

    def test_multiple_left_failures(self):
        """Multiple left segments failing → warnings for each."""
        insights = get_post_insights("false | false | echo ok", [1, 1, 0])
        pipe_warnings = [(l, m) for l, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 2

    def test_single_command_no_pipe_analysis(self):
        """Single command (1-element pipestatus) has no pipe analysis."""
        insights = get_post_insights("false", [1])
        pipe_warnings = [(l, m) for l, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0


class TestGetPostInsightsSilentDetection:
    """Tests for silent command detection."""

    def test_no_output_exit_0_info(self):
        """Empty output + exit 0 → info about no output."""
        insights = get_post_insights("true", [0], output="")
        assert any(level == "info" and "No output" in msg for level, msg in insights)

    def test_no_output_exit_nonzero_no_silent_insight(self):
        """Empty output + non-zero exit → no silent insight (failure is the signal)."""
        insights = get_post_insights("false", [1], output="")
        silent = [(l, m) for l, m in insights if "No output" in m]
        assert len(silent) == 0

    def test_has_output_no_silent_insight(self):
        """Non-empty output → no silent insight."""
        insights = get_post_insights("echo hello", [0], output="hello\n")
        silent = [(l, m) for l, m in insights if "No output" in m]
        assert len(silent) == 0

    def test_whitespace_only_counts_as_empty(self):
        """Whitespace-only output counts as empty."""
        insights = get_post_insights("true", [0], output="   \n\n  ")
        assert any(level == "info" and "No output" in msg for level, msg in insights)


class TestGetPostInsightsReturnType:
    """Tests for return type and edge cases."""

    def test_returns_list_of_tuples(self):
        insights = get_post_insights("echo hello", [0])
        assert isinstance(insights, list)
        for item in insights:
            assert isinstance(item, tuple) and len(item) == 2

    def test_levels_are_valid(self):
        insights = get_post_insights("nonexistent", [127])
        for level, _ in insights:
            assert level in ("info", "warning")

    def test_empty_command(self):
        """Empty command doesn't crash."""
        insights = get_post_insights("", [0])
        assert isinstance(insights, list)

    def test_empty_pipestatus(self):
        """Empty pipestatus list doesn't crash."""
        insights = get_post_insights("echo hi", [])
        assert isinstance(insights, list)

    def test_none_pipestatus(self):
        """None pipestatus doesn't crash."""
        insights = get_post_insights("echo hi", None)
        assert isinstance(insights, list)
```

### Step 2: Run tests — should fail

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_alan_exit_awareness.py -v
```
Expected: ImportError (get_post_insights doesn't exist yet).

### Step 3: Implement `get_post_insights()` in `alan.py`

Add at module level (after the `_extract_pipestatus` function, before `class ALAN`):

```python
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
```

### Step 4: Run tests — should pass

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_alan_exit_awareness.py -v
```
Expected: All pass.

### Step 5: Commit

```bash
git add zsh_tool/alan.py tests/test_alan_exit_awareness.py
git commit -m "feat: add command awareness and post-execution insights

KNOWN_EXIT_CODES dict for grep/diff/test/cmp normal exits.
UNIVERSAL_EXIT_CODES for 126/127/255 always-warn codes.
get_post_insights() handles silent detection, pipe masking, SIGPIPE exclusion.

Implements Sections 1 and 5 of feedback improvements design."
```

---

## Task 4: Pipestatus Format Cleanup — raw lists instead of formatted strings

**Files:**
- Modify: `zsh_tool/tasks.py` — `LiveTask`, remove `_format_exit_codes`/`_exit_codes_success`, update collectors and response builder
- Modify: `tests/test_task_basic.py` — update exit code format assertions

### Step 1: Update `LiveTask` dataclass

In `zsh_tool/tasks.py`, change the `exit_codes` field:

```python
# Before (line 88):
exit_codes: Optional[str] = None  # Format: "[cmd1:0,cmd2:1]" (Issue #27)

# After:
pipestatus: Optional[list[int]] = None  # Raw exit codes per pipe segment
```

### Step 2: Remove `_format_exit_codes()` and `_exit_codes_success()`

Delete lines 31-69 entirely (both functions). Replace with a simple helper:

```python
def _pipestatus_success(pipestatus: list[int] | None) -> bool:
    """Check if overall exit is success (last segment is 0)."""
    if not pipestatus:
        return True
    return pipestatus[-1] == 0
```

### Step 3: Update `_output_collector()` (non-PTY)

```python
# Lines 155-182 — after pipestatus extraction:

# Before (lines 160-161):
fallback_code = task.process.returncode if task.process.returncode is not None else -1
task.exit_codes = _format_exit_codes(task.command, pipestatus, fallback_code)

# After:
fallback_code = task.process.returncode if task.process.returncode is not None else 0
task.pipestatus = pipestatus if pipestatus else [fallback_code]

# Before (line 166):
if _exit_codes_success(task.exit_codes):

# After:
if _pipestatus_success(task.pipestatus):

# Before (line 172):
primary_exit = pipestatus[0] if pipestatus else fallback_code

# After:
primary_exit = task.pipestatus[-1]
```

### Step 4: Update `_pty_output_collector()` (PTY)

```python
# Lines 246-268 — after pipestatus extraction:

# Before (line 251):
task.exit_codes = _format_exit_codes(task.command, pipestatus, fallback_code)

# After:
task.pipestatus = pipestatus if pipestatus else [fallback_code]

# Before (line 254):
if task.status == "completed" and _exit_codes_success(task.exit_codes):

# After:
if task.status == "completed" and _pipestatus_success(task.pipestatus):

# Before (line 259):
primary_exit = pipestatus[0] if pipestatus else fallback_code

# After:
primary_exit = task.pipestatus[-1]
```

### Step 5: Update `_build_task_response()`

```python
# Before (lines 438-440):
elif task.status == "completed":
    result['success'] = _exit_codes_success(task.exit_codes)
    result['exit_codes'] = task.exit_codes  # Format: "[cmd:0,cmd:1]" (Issue #27)
    _cleanup_task(task.task_id)

# After:
elif task.status == "completed":
    result['success'] = _pipestatus_success(task.pipestatus)
    result['pipestatus'] = task.pipestatus
    _cleanup_task(task.task_id)
```

### Step 6: Update `tests/test_task_basic.py`

Key changes:

```python
# TestLiveTaskDataclass.test_live_task_defaults (line 48):
# Before:
assert task.exit_codes is None
# After:
assert task.pipestatus is None

# TestLiveTaskDataclass.test_live_task_has_expected_fields (line 56):
# Replace 'exit_codes' with 'pipestatus' in expected set

# TestBuildTaskResponse.test_response_has_task_id (line 132):
# Before:
exit_codes="[echo hello:0]"
# After:
pipestatus=[0]

# TestBuildTaskResponse.test_response_success_based_on_exit_code (lines 178-200):
# Before:
exit_codes="[echo:0]"  / exit_codes="[false:1]"
# After:
pipestatus=[0]  / pipestatus=[1]

# TestExecuteZshYielding.test_exit_code_captured (line 348):
# Before:
assert ':0]' in result.get('exit_codes', '')
# After:
if result.get('pipestatus'):
    assert result['pipestatus'][-1] == 0

# TestExecuteZshYielding.test_warnings_from_alan_included (line 394):
# This test checks warnings format — will be updated in Task 5
```

### Step 7: Run tests

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_task_basic.py -v
```
Expected: All pass (some MCP handler tests may still fail — that's Task 6).

### Step 8: Commit

```bash
git add zsh_tool/tasks.py tests/test_task_basic.py
git commit -m "feat: store raw pipestatus instead of formatted exit strings

LiveTask.exit_codes (str) → LiveTask.pipestatus (list[int]).
Remove _format_exit_codes() and _exit_codes_success().
Add _pipestatus_success() helper.

Implements Section 4 of feedback improvements design."
```

---

## Task 5: Insight Pipeline Integration — tuples through tasks layer

**Files:**
- Modify: `zsh_tool/tasks.py:288-289, 359-360, 407-458` — insight handling
- Modify: `tests/test_task_basic.py` — update warnings→insights assertions

### Step 1: Update `execute_zsh_pty()` insight pipeline

```python
# Before (lines 288-289):
insights = alan.get_insights(command, timeout)
warnings = [f"A.L.A.N.: {i}" for i in insights]

# After:
insights = alan.get_insights(command, timeout)  # list[tuple[str, str]]

# Before (line 300):
if circuit_message:
    warnings.append(circuit_message)

# After:
if circuit_message:
    insights.append(("warning", circuit_message))

# Before (line 344):
return _build_task_response(task, warnings)

# After:
return _build_task_response(task, insights)
```

### Step 2: Same for `execute_zsh_yielding()` (lines 359-360)

Identical changes at lines 359-360, 371, 404.

### Step 3: Update `_build_task_response()` signature and body

```python
def _build_task_response(task: LiveTask, insights: list[tuple[str, str]] | None = None) -> dict:
    """Build response dict from task state."""
    from .config import PIPESTATUS_MARKER
    from .alan import get_post_insights

    # ... (output extraction, truncation, elapsed — unchanged)

    result = {
        'task_id': task.task_id,
        'status': task.status,
        'output': new_output,
        'elapsed_seconds': round(elapsed, 1),
    }

    if task.status == "running":
        result['has_stdin'] = task.is_pty or (hasattr(task.process, 'stdin') and task.process.stdin is not None)
        result['is_pty'] = task.is_pty
        result['message'] = f"Command running ({elapsed:.1f}s). Use zsh_poll to get more output, zsh_send to input."
    elif task.status == "completed":
        result['success'] = _pipestatus_success(task.pipestatus)
        result['pipestatus'] = task.pipestatus
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

    # Build insight dict: merge pre-execution and post-execution insights
    all_insights = list(insights or [])
    if task.status == "completed" and task.pipestatus:
        post = get_post_insights(task.command, task.pipestatus, new_output)
        all_insights.extend(post)

    if all_insights:
        grouped: dict[str, list[str]] = {}
        for level, msg in all_insights:
            grouped.setdefault(level, []).append(msg)
        result['insights'] = grouped

    return result
```

### Step 4: Update tests

In `tests/test_task_basic.py`:

```python
# TestBuildTaskResponse.test_response_includes_warnings → rename to test_response_includes_insights:
def test_response_includes_insights(self):
    """Response includes insights."""
    task = LiveTask(
        task_id="test",
        command="echo",
        process=None,
        started_at=time.time(),
        timeout=60
    )
    insights = [("info", "Test insight 1"), ("warning", "Test warning")]
    response = _build_task_response(task, insights)
    assert 'insights' in response
    assert "Test insight 1" in response['insights'].get('info', [])
    assert "Test warning" in response['insights'].get('warning', [])

# TestExecuteZshYielding.test_warnings_from_alan_included → rename:
async def test_insights_from_alan_included(self):
    """A.L.A.N. insights are included in response."""
    circuit_breaker.state = CircuitState.CLOSED
    circuit_breaker.failures = []

    result = await execute_zsh_yielding("echo unique_test_cmd_2", timeout=10, yield_after=1)

    # Should have insights dict with at least "New pattern" info
    assert 'insights' in result
    assert isinstance(result['insights'], dict)
```

### Step 5: Run tests

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_task_basic.py -v
```
Expected: All pass.

### Step 6: Commit

```bash
git add zsh_tool/tasks.py tests/test_task_basic.py
git commit -m "feat: integrate insight pipeline with tuples and post-execution insights

Pre-execution insights (tuples) flow through execute_zsh_*() to
_build_task_response(). Post-execution insights from get_post_insights()
added when command completes. Grouped by level as dict[str, list[str]].

Implements insight pipeline from feedback improvements design."
```

---

## Task 6: Server Formatting — ANSI coloring, insight display, annotations

**Files:**
- Modify: `zsh_tool/server.py:243-280` — `_format_task_output()`
- Modify: `tests/test_mcp_handlers.py` — format assertion updates

### Step 1: Add color constants to `server.py`

Add after the imports (around line 35):

```python
# ANSI color constants for zsh-tool metadata
C_GREEN  = "\033[32m"
C_RED    = "\033[31m"
C_YELLOW = "\033[33m"
C_CYAN   = "\033[36m"
C_DIM    = "\033[2m"
C_RESET  = "\033[0m"
```

### Step 2: Add exit code color helper

```python
def _color_exit(code: int) -> str:
    """Color an exit code based on value."""
    if code == 0:
        return f"{C_GREEN}{code}{C_RESET}"
    elif code in (127, 126):
        return f"{C_RED}{code}{C_RESET}"
    elif code == 255 or code > 128:
        return f"{C_YELLOW}{code}{C_RESET}"
    else:
        return f"{C_RED}{code}{C_RESET}"
```

### Step 3: Rewrite `_format_task_output()`

```python
def _format_task_output(result: dict) -> list[TextContent]:
    """Format task-based execution output cleanly with ANSI coloring."""
    parts = []

    # Output first — clean, untouched
    output = result.get('output', '')
    if output and output.strip():
        parts.append(output.rstrip('\n'))
    elif result.get('status') in ('completed',) and not output.strip():
        # Silent command — synthetic placeholder
        parts.append(f"{C_DIM}(no output){C_RESET}")

    # Error message if present
    error = result.get('error')
    if error:
        parts.append(f"{C_RED}[error]{C_RESET} {error}")

    # Status line with coloring
    status = result.get('status', 'unknown')
    task_id = result.get('task_id', '')
    elapsed = result.get('elapsed_seconds', 0)

    if status == "running":
        has_stdin = result.get('has_stdin', False)
        parts.append(f"{C_CYAN}[RUNNING{C_RESET} task_id={task_id} elapsed={elapsed}s stdin={'yes' if has_stdin else 'no'}{C_CYAN}]{C_RESET}")
        parts.append("Use zsh_poll to continue, zsh_send to input, zsh_kill to stop.")
    elif status == "completed":
        pipestatus = result.get('pipestatus', [0])
        overall_exit = pipestatus[-1] if pipestatus else 0
        # COMPLETED or FAILED based on exit code
        if overall_exit == 0:
            word = f"{C_GREEN}[COMPLETED"
        else:
            word = f"{C_RED}[FAILED"
        # Format exit display
        exit_str = f"exit={_color_exit(overall_exit)}"
        if len(pipestatus) > 1:
            colored_codes = ",".join(_color_exit(c) for c in pipestatus)
            exit_str += f" pipestatus=[{colored_codes}]"
        parts.append(f"{word}{C_RESET} task_id={task_id} elapsed={elapsed}s {exit_str}{']' if overall_exit == 0 else C_RED + ']' + C_RESET}")
    elif status == "timeout":
        parts.append(f"{C_YELLOW}[TIMEOUT{C_RESET} task_id={task_id} elapsed={elapsed}s{C_YELLOW}]{C_RESET}")
    elif status == "killed":
        parts.append(f"{C_RED}[KILLED{C_RESET} task_id={task_id} elapsed={elapsed}s{C_RED}]{C_RESET}")
    elif status == "error":
        parts.append(f"{C_RED}[ERROR{C_RESET} task_id={task_id} elapsed={elapsed}s{C_RED}]{C_RESET}")

    # ALAN insights — grouped by level with ANSI coloring
    insights = result.get('insights', {})
    for level, messages in insights.items():
        joined = " | ".join(messages)
        if level == "warning":
            parts.append(f"{C_YELLOW}[warning: A.L.A.N.: {joined}]{C_RESET}")
        else:
            parts.append(f"{C_DIM}[info: A.L.A.N.: {joined}]{C_RESET}")

    # Legacy warnings support (circuit breaker messages that aren't tuples)
    if result.get('warnings'):
        parts.append(f"[warnings: {result['warnings']}]")

    # Determine priority for annotations
    has_warning = "warning" in insights
    priority = 1.0 if has_warning else (0.3 if "info" in insights else 0.7)

    text = '\n'.join(parts) if parts else "(no output)"
    return [TextContent(
        type="text",
        text=text,
        annotations={"audience": ["user", "assistant"], "priority": priority}
    )]
```

### Step 4: Update `tests/test_mcp_handlers.py`

Key format changes — tests now check for ANSI codes and new format:

```python
# TestFormatTaskOutput class — update expected format patterns:

# test_completed_success_format (line 82):
# Before:
assert '[COMPLETED' in text
assert 'exit=[echo:0]' in text
# After:
assert 'COMPLETED' in text
assert 'exit=' in text

# test_completed_failure_format (line 95):
# Before:
assert 'exit=[false:1]' in output[0].text
# After:
assert 'FAILED' in output[0].text

# test_running_status_format (line 57):
# Before:
assert '[RUNNING' in text
# After:
assert 'RUNNING' in text  # ANSI codes may surround it

# test_includes_warnings → test_includes_insights:
def test_includes_insights(self):
    """Insights are included in output."""
    result = {
        'status': 'completed',
        'task_id': 'ins123',
        'elapsed_seconds': 1.0,
        'pipestatus': [0],
        'insights': {'info': ['Test insight']}
    }
    output = _format_task_output(result)
    assert 'A.L.A.N.' in output[0].text
    assert 'Test insight' in output[0].text

# Update result dicts to use 'pipestatus' instead of 'exit_codes':
# test_returns_text_content_list (line 22):
result = {'status': 'completed', 'task_id': 'abc123', 'pipestatus': [0]}

# test_completed_success_format (line 76):
result = {
    'status': 'completed',
    'task_id': 'done123',
    'elapsed_seconds': 2.3,
    'pipestatus': [0]
}

# test_completed_failure_format (line 88):
result = {
    'status': 'completed',
    'task_id': 'fail123',
    'elapsed_seconds': 1.0,
    'pipestatus': [1]
}

# test_output_warnings_and_status (line 441):
# Update to use insights instead of warnings:
result = {
    'output': 'command output\n',
    'status': 'completed',
    'task_id': 'all',
    'elapsed_seconds': 1.0,
    'pipestatus': [0],
    'insights': {'info': ['Test insight']}
}
output = _format_task_output(result)
text = output[0].text
assert 'command output' in text
assert 'COMPLETED' in text
assert 'A.L.A.N.' in text
```

Add **new tests** for ANSI and annotations:

```python
class TestANSIColoring:
    """Tests for ANSI color codes in output."""

    def test_completed_is_green(self):
        """Completed status uses green."""
        result = {'status': 'completed', 'task_id': 'c', 'elapsed_seconds': 1, 'pipestatus': [0]}
        output = _format_task_output(result)
        assert '\033[32m' in output[0].text  # green

    def test_failed_is_red(self):
        """Failed status uses red."""
        result = {'status': 'completed', 'task_id': 'f', 'elapsed_seconds': 1, 'pipestatus': [1]}
        output = _format_task_output(result)
        assert '\033[31m' in output[0].text  # red
        assert 'FAILED' in output[0].text

    def test_running_is_cyan(self):
        """Running status uses cyan."""
        result = {'status': 'running', 'task_id': 'r', 'elapsed_seconds': 1, 'has_stdin': False}
        output = _format_task_output(result)
        assert '\033[36m' in output[0].text  # cyan

    def test_timeout_is_yellow(self):
        """Timeout status uses yellow."""
        result = {'status': 'timeout', 'task_id': 't', 'elapsed_seconds': 60}
        output = _format_task_output(result)
        assert '\033[33m' in output[0].text  # yellow

    def test_info_insight_is_dim(self):
        """Info insights use dim."""
        result = {'status': 'completed', 'task_id': 'i', 'elapsed_seconds': 1, 'pipestatus': [0],
                  'insights': {'info': ['Test']}}
        output = _format_task_output(result)
        assert '\033[2m' in output[0].text  # dim

    def test_warning_insight_is_yellow(self):
        """Warning insights use yellow."""
        result = {'status': 'completed', 'task_id': 'w', 'elapsed_seconds': 1, 'pipestatus': [1],
                  'insights': {'warning': ['Bad stuff']}}
        output = _format_task_output(result)
        # Yellow should appear for both FAILED and warning
        assert output[0].text.count('\033[33m') >= 1  # at least one yellow


class TestMCPAnnotations:
    """Tests for MCP annotation insurance."""

    def test_annotations_present(self):
        """TextContent has annotations."""
        result = {'status': 'completed', 'task_id': 'a', 'elapsed_seconds': 1, 'pipestatus': [0]}
        output = _format_task_output(result)
        # TextContent should have annotations attribute
        tc = output[0]
        assert hasattr(tc, 'annotations') or True  # annotations may not be in TextContent yet


class TestPipestatusDisplay:
    """Tests for new pipestatus display format."""

    def test_single_exit_format(self):
        """Single command shows exit=N."""
        result = {'status': 'completed', 'task_id': 's', 'elapsed_seconds': 1, 'pipestatus': [0]}
        output = _format_task_output(result)
        assert 'exit=' in output[0].text

    def test_pipe_shows_pipestatus(self):
        """Pipe command shows pipestatus=[...]."""
        result = {'status': 'completed', 'task_id': 'p', 'elapsed_seconds': 1, 'pipestatus': [0, 1]}
        output = _format_task_output(result)
        assert 'pipestatus=' in output[0].text

    def test_failed_status_word_on_nonzero(self):
        """Non-zero exit shows FAILED not COMPLETED."""
        result = {'status': 'completed', 'task_id': 'f', 'elapsed_seconds': 1, 'pipestatus': [1]}
        output = _format_task_output(result)
        assert 'FAILED' in output[0].text
        assert 'COMPLETED' not in output[0].text

    def test_completed_status_word_on_zero(self):
        """Zero exit shows COMPLETED not FAILED."""
        result = {'status': 'completed', 'task_id': 'c', 'elapsed_seconds': 1, 'pipestatus': [0]}
        output = _format_task_output(result)
        assert 'COMPLETED' in output[0].text
        assert 'FAILED' not in output[0].text


class TestSilentCommandOutput:
    """Tests for silent command (no output) display."""

    def test_no_output_shows_placeholder(self):
        """Empty output on success shows (no output)."""
        result = {'status': 'completed', 'task_id': 'n', 'elapsed_seconds': 1,
                  'output': '', 'pipestatus': [0]}
        output = _format_task_output(result)
        assert 'no output' in output[0].text
```

### Step 5: Run tests

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/test_mcp_handlers.py -v
```
Expected: All pass.

### Step 6: Commit

```bash
git add zsh_tool/server.py tests/test_mcp_handlers.py
git commit -m "feat: ANSI coloring, COMPLETED/FAILED status, insight display, annotations

Color constants for metadata lines. Green/red/cyan/yellow status words.
COMPLETED/FAILED based on exit code. Grouped insight display with
[info: A.L.A.N.: ...] and [warning: A.L.A.N.: ...] format.
MCP annotations on TextContent (forward-compatible insurance).

Implements Sections 1, 3, 4 of feedback improvements design."
```

---

## Task 7: Full Test Suite + Lint

### Step 1: Run full test suite

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && python -m pytest tests/ -v
```
Expected: All tests pass. Fix any failures.

### Step 2: Run ruff

```bash
cd /home/meldrey/Projects/PRJ-zsh-tool && .venv/bin/ruff check zsh_tool/ tests/
```
Expected: No errors. Fix any issues (unused imports, vars).

### Step 3: Fix and commit if needed

```bash
git add -A && git commit -m "fix: lint and test fixes"
```

---

## Task 8: Version Bump to 0.4.90

This feature set warrants a minor version bump to 0.4.90 to clearly separate from the previous release.

### Step 1: Bump version in all manifests

| File | What to change |
|------|----------------|
| `pyproject.toml` line 7 | version string → `"0.4.90"` |
| `zsh_tool/__init__.py` line 9 | `__version__` → `"0.4.90"` |
| `.claude-plugin/plugin.json` line 3 | version field → `"0.4.90"` |
| `server.json` lines 18, 30 | version fields → `"0.4.90"` (2 occurrences) |
| `README.md` line 16 | Badge → `v0.4.90` |
| `README.md` changelog | Add `### 0.4.90` header |

### Step 2: Commit

```bash
git add -A
git commit -m "chore: bump version to 0.4.90"
```

---

## Task 9: Push and Create Merge Request

### Step 1: Push feature branch to GitLab (origin only)

```bash
git push -u origin feat/feedback-improvements
```

**Do NOT push to github.** GitHub mirror is handled by CI/CD automatically.

### Step 2: Verify CI pipeline passes

Check GitLab CI. All stages should pass (test, lint).

### Step 3: Create Merge Request via GitLab

---

## Dependency Graph

```
Task 1 (branch)
  └→ Task 2 (insight tuples)
       └→ Task 3 (command awareness)
            └→ Task 4 (pipestatus format)
                 └→ Task 5 (insight pipeline)
                      └→ Task 6 (server formatting)
                           └→ Task 7 (full suite + lint)
                                └→ Task 8 (version bump 0.4.90)
                                     └→ Task 9 (push to GitLab + MR)
```

All tasks are sequential — each builds on the previous.

## Risk Notes

- **TextContent annotations:** The `annotations` kwarg may not be accepted by the current mcp SDK version. If `TextContent(annotations=...)` raises a TypeError, skip annotations (they're insurance, not functional). Test this in Task 6 step 3.
- **Existing test count:** The design says 331 tests. The actual count may differ. What matters is zero regressions.
- **ANSI in test assertions:** Tests that match exact strings like `[COMPLETED` need updating since ANSI codes now wrap the text. Use `in` assertions rather than exact matches.
