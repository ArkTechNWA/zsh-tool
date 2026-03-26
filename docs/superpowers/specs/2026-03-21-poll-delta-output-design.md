# Poll Delta Output with Line Numbers

**Date:** 2026-03-21
**Status:** Approved
**Scope:** `zsh_poll` tool — delta output, line numbering, `full_output` parameter

## Problem

When polling a running task with 800+ lines of output, `zsh_poll` returns the entire output buffer on every call. This wastes tokens and makes it hard to identify what's new.

Delta byte tracking exists (`last_poll_offset` computes `new_bytes`) but `last_poll_offset` is only used as a byte-count bookmark for the `new_bytes` metric — the `output` field always returns the full `output_buffer` via `truncate_output()`.

## Design

### Behavior Change

- **Default:** return only new output since last poll (delta from `last_poll_offset`)
- **Optional:** `full_output: bool` parameter to request the entire buffer
- **Line numbers:** prepend global line numbers to each line in the output (e.g., `801: content`)
- **Consistency:** completed tasks follow the same rules — delta by default, full if asked

### New Input Parameter

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `full_output` | bool | false | Return entire output buffer instead of delta |

### Response Shape

```json
{
  "task_id": "abc",
  "status": "running",
  "output": "801: line content\n802: line content\n...",
  "from_line": 801,
  "to_line": 850,
  "new_bytes": 1234,
  "elapsed_seconds": 5.2,
  "has_stdin": true,
  "insights": []
}
```

Changes from current response:
- `output` — delta with line numbers (was: full buffer, no line numbers)
- `from_line` — first line number in this delta (new)
- `to_line` — last line number in this delta (new)
- `new_bytes` — unchanged, still reports byte delta

### Edge Cases

- **First poll:** delta from offset 0 = full output. No special case needed.
- **No new output:** empty `output`, `from_line` and `to_line` are null/absent, `new_bytes: 0`.
- **`full_output: true`:** returns entire buffer with line numbers starting at 1, `from_line: 1`, `to_line: N`. If truncation is active, `to_line` reflects the last line actually returned, not the total line count.
- **Completion during poll:** `finalize_task` path (line 797) must also apply delta/line-number logic. Without this, a poll that catches task completion would return unnumbered full output while prior polls returned numbered deltas.
- **Re-poll of completed task:** returns the final delta (output since last poll before completion). Subsequent re-polls return empty delta + completed status. `last_poll_line` persists on `TaskInfo` for this.
- **Partial final line:** if the delta ends without a trailing newline (process hasn't flushed), the partial line still gets a line number. It's a real line of output.

### Out of Scope

- **Initial `zsh` running response:** when `zsh` yields a still-running task, it returns unnumbered output. This is the `handle_zsh` yield path, not the poll path. Adding line numbers there is a separate change — the initial response establishes the task, poll handles ongoing observation.

### Implementation Scope

**Files to modify:**
- `zsh-tool-rs/src/serve/tools.rs` — add `full_output` parameter to tool definition
- `zsh-tool-rs/src/serve/mod.rs`:
  - `TaskInfo` struct — add `last_poll_line: usize` field
  - `handle_zsh()` — initialize `last_poll_line: 0` at TaskInfo creation (line ~674)
  - `handle_poll()` running path — return delta with line numbers instead of full buffer
  - `handle_poll()` already-completed path (lines 729-742) — apply delta logic with persisted `last_poll_line`
  - `finalize_task()` — apply delta/line-number logic (called from handle_poll when task completes during poll)

**New state on TaskInfo:**
- `last_poll_line: usize` — global line count at last poll offset, initialized to 0

**Order of operations for output construction:**
1. Slice delta from `output_buffer[last_poll_offset..]` (or full buffer if `full_output`)
2. Count and prepend line numbers to each `\n`-delimited segment
3. Apply `truncate_output` to the numbered result
4. Set `to_line` from actual lines returned (post-truncation), not pre-truncation count

**No changes needed:**
- `format_task_output()` — renders status line from result map; new fields (`from_line`, `to_line`) pass through without breaking existing key matching

### What Does NOT Change

- `last_poll_offset` byte tracking — stays, used for `new_bytes` calculation
- `output_buffer` accumulation — stays, full buffer preserved internally
- `truncate_output` — still applied, but after line numbering (see order of operations above)
