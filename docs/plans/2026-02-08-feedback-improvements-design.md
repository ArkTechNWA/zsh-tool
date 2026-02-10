# Feedback Improvements Design

Date: 2026-02-08
Status: Approved

## Context

zsh-tool's feedback output has several problems confirmed by testing:
- Silent commands (`true`, `pip install -q`) return nothing — user and LLM both see void
- ALAN emits all insights as `[warnings: [...]]` regardless of severity — boy who cried wolf
- Exit codes are buried in a verbose `exit=[cmd:code,cmd:code]` format that's hard to parse
- No color — status, exit codes, ALAN output all look the same
- `&&` chains produce exit -1 and broken parsing
- ALAN doesn't understand that `grep` exit 1 is normal

## Scope

Five changes, one release. All changes are in the response formatting layer — no protocol changes, no new tools, no breaking API.

### Files to modify

| File | Changes |
|---|---|
| `zsh_tool/alan.py` | Insight classification (tuples), `_extract_pipestatus()` cleanup, command awareness dict, silent command detection |
| `zsh_tool/tasks.py` | `_build_task_response()` new exit format, COMPLETED/FAILED logic, insight tuple handling |
| `zsh_tool/server.py` | `_format_result()` ANSI coloring, annotation insurance, `(no output)` for silent commands |

### Files to create

None. Color constants go in server.py (6 lines, not worth a module).

---

## 1. Silent Command Awareness

### Problem
Commands that exit 0 with no stdout/stderr produce no feedback at all.

### Solution
Two signals, belt and suspenders:

**A. Synthetic line in server.py** — when command output is empty and exit=0, the response starts with `(no output)` instead of blank space. This already happens as a fallback in `_format_result()` line 280 but only when `parts` is completely empty. Extend it to fire when command output is empty but status lines exist.

**B. ALAN insight** — `get_insights()` checks if the command produced output. If not and exit=0, emit:
```python
("info", "No output produced.")
```

### Result
Before:
```
[COMPLETED task_id=703e0fec elapsed=0.3s exit=[true:0]]
[warnings: ['A.L.A.N.: New pattern. No history yet.']]
```

After:
```
(no output)
[COMPLETED task_id=703e0fec elapsed=0.3s exit=0]
[info: A.L.A.N.: No output produced. | New pattern.]
```

---

## 2. ALAN Insight Classification

### Problem
Every insight goes into `warnings` regardless of severity.

### Solution
`get_insights()` returns `list[tuple[str, str]]` instead of `list[str]`. Each tuple is `(level, message)` where level is `"info"` or `"warning"`.

### Classification map

| Insight | Level |
|---|---|
| New pattern. No history yet. | info |
| Retry #N. Previous N succeeded. | info |
| Streak: N successes in a row. Solid. | info |
| Reliable pattern: N% success | info |
| Usually takes ~Ns. | info |
| Similar to 'X' - N/N succeeded | info |
| No output produced. | info |
| grep exit 1 = no match (normal) | info |
| Retry #N. Previous N all failed. Different approach? | warning |
| Failing streak: N. Same approach? | warning |
| N% timeout rate for this pattern. | warning |
| Host 'X' has N% connection failure rate | warning |
| Remote command 'X' fails often | warning |
| command not found (exit 127) | warning |
| pipe segment N exited non-zero (masked by downstream) | warning |

### Integration in tasks.py

```python
# Before (line 288-289):
insights = alan.get_insights(command, timeout)
warnings = [f"A.L.A.N.: {i}" for i in insights]

# After:
insights = alan.get_insights(command, timeout)  # returns [(level, msg), ...]
```

### Formatting in server.py

```python
# Before (line 276-277):
if result.get('warnings'):
    parts.append(f"[warnings: {result['warnings']}]")

# After:
for level, messages in result.get('insights', {}).items():
    joined = " | ".join(messages)
    parts.append(f"[{level}: A.L.A.N.: {joined}]")
```

Multiple insights at the same level join with ` | ` on one line. Different levels get separate lines. Omit entirely when empty.

---

## 3. ANSI Coloring

### Problem
No visual hierarchy. Status, exit codes, ALAN output all look identical.

### Decision
Hybrid approach. Command output untouched (PTY preserves color for user). zsh-tool metadata gets ANSI coloring.

### Color constants (server.py)

```python
C_GREEN  = "\033[32m"
C_RED    = "\033[31m"
C_YELLOW = "\033[33m"
C_CYAN   = "\033[36m"
C_DIM    = "\033[2m"
C_RESET  = "\033[0m"
```

### Status line

| Status | Color | When |
|---|---|---|
| `[COMPLETED]` | green | All exit codes 0 |
| `[FAILED]` | red | Any exit code non-zero |
| `[RUNNING]` | cyan | Yielded, still running |
| `[TIMEOUT]` | yellow | Hit timeout limit |
| `[KILLED]` | red | Manually killed |
| `[ERROR]` | red | Internal error |

### Exit code coloring

| Exit | Color | Meaning |
|---|---|---|
| 0 | green | Success |
| 127 | red | Command not found |
| 126 | red | Permission denied |
| 255 | yellow | SSH connection failed |
| 128+N | yellow | Killed by signal |
| Other non-zero | red | Failure |

### ALAN output coloring

| Level | Color |
|---|---|
| info | dim (`\033[2m`) |
| warning | yellow (`\033[33m`) |

### MCP Annotations (forward-compatible insurance)

Add annotations to `TextContent` even though Claude Code ignores them today (v2.1.37). Zero cost, lights up automatically when support ships.

```python
TextContent(
    type="text",
    text='\n'.join(parts),
    annotations={"audience": ["user", "assistant"], "priority": priority}
)
```

Priority values: warning insights → 1.0, info insights → 0.3, normal output → 0.7.

---

## 4. Pipestatus Format Cleanup

### Problem
Current format: `exit=[echo "hello world":0,grep nope:1]`
- Command names embedded with exit codes
- `&&` chains produce exit -1
- Hard to parse for humans and LLMs

### New format

```
exit=0                          # single command, success
exit=1                          # single command, failure
exit=0 pipestatus=[0,0]         # pipe, all success
exit=1 pipestatus=[0,1]         # pipe, right failed
exit=1                          # && chain, final exit only
```

### Rules
- `exit=N` is always the overall exit code (rightmost pipe segment, or `$?` for chains)
- `pipestatus=[...]` only when 2+ pipe segments
- No command names in exit display
- No more -1 exit codes

### Status word logic

```python
overall_exit = exit_codes[-1]  # rightmost segment
word = "COMPLETED" if overall_exit == 0 else "FAILED"
```

### Changes to _extract_pipestatus (alan.py)
Return `list[int]` instead of the `cmd:code` string format. The string formatting moves to the response layer.

### Changes to _build_task_response (tasks.py)
Format the new `exit=N pipestatus=[...]` string. Pass raw exit code list to the response for COMPLETED/FAILED determination.

---

## 5. Command Awareness (bounded)

### Problem
ALAN treats `grep` exit 1 the same as `rm` exit 1. It doesn't know that some exit codes are normal for specific commands.

### Solution
Small, fixed dict. Not a growing lookup table.

```python
KNOWN_EXIT_CODES = {
    "grep":  {1: "no match"},
    "diff":  {1: "files differ"},
    "test":  {1: "condition false"},
    "[":     {1: "condition false"},
    "cmp":   {1: "files differ"},
}

UNIVERSAL_EXIT_CODES = {
    126: "permission denied",
    127: "command not found",
    255: "SSH connection failed",
}
```

### Integration in get_insights()

```python
base_cmd = command.split()[0].split("/")[-1]
exit_code = pipestatus[-1]

if exit_code in UNIVERSAL_EXIT_CODES:
    insights.append(("warning", f"{UNIVERSAL_EXIT_CODES[exit_code]} (exit {exit_code})"))
elif base_cmd in KNOWN_EXIT_CODES and exit_code in KNOWN_EXIT_CODES[base_cmd]:
    meaning = KNOWN_EXIT_CODES[base_cmd][exit_code]
    insights.append(("info", f"{base_cmd} exit {exit_code} = {meaning} (normal)"))
```

### Pipe awareness

```python
if len(pipestatus) > 1:
    for i, code in enumerate(pipestatus[:-1]):
        if code != 0 and code not in (141,):  # 141=SIGPIPE, normal in pipes
            insights.append(("warning", f"pipe segment {i+1} exited {code} (masked by downstream)"))
```

Exit 141 (SIGPIPE) excluded — normal when right side closes early (`cat bigfile | head -1`).

### Boundary
This dict is the complete scope. Adding a command later = one line. But we don't design for extensibility.

---

## Verification

### Manual testing

```bash
# Silent command — should show (no output) + info
true

# Pipe with failure — should show FAILED + pipestatus
echo test | grep nope

# Color rendering — user should see colored status line
echo hello

# PTY color passthrough — user sees colors, metadata also colored
echo hi | lolcat  (pty=true)

# Command awareness — grep exit 1 should be info not warning
echo test | grep nope

# Pipe masking — left side failure should warn
false | echo "masked"

# && chain — no more exit -1
echo ok && false
```

### Automated tests

- Existing test suite must pass (331 tests)
- Add tests for:
  - Insight classification: each insight type returns correct `(level, message)` tuple
  - Exit format: single command, pipe, && chain produce correct `exit=N` format
  - Silent detection: empty output triggers `(no output)` and insight
  - Command awareness: grep/diff/test exit 1 → info, exit 127 → warning
  - Pipe masking: left-side non-zero (excluding 141) → warning
  - ANSI presence: status lines contain expected escape codes
