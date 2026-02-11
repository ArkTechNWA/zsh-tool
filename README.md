<img align="right" src="logo.png" width="150">

<!-- mcp-name: io.github.ArkTechNWA/zsh-tool -->

<br><br><br>

# zsh-tool

[![CI/CD](https://img.shields.io/gitlab/pipeline-status/arktechnwa%2Fmcp%2Fzsh-tool?branch=master&gitlab_url=https%3A%2F%2Fgitlab.arktechnwa.com&label=CI%2FCD)](https://gitlab.arktechnwa.com/arktechnwa/mcp/zsh-tool/-/pipelines)
[![coverage](https://img.shields.io/gitlab/pipeline-coverage/arktechnwa%2Fmcp%2Fzsh-tool?branch=master&gitlab_url=https%3A%2F%2Fgitlab.arktechnwa.com)](https://gitlab.arktechnwa.com/arktechnwa/mcp/zsh-tool/-/pipelines)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/)

Zsh execution tool for Claude Code with full Bash parity, yield-based oversight, PTY mode, NEVERHANG circuit breaker, and A.L.A.N. short-term learning.

**Status:** Beta (v0.5.0)

**Author:** Claude + Meldrey

**License:** [MIT](LICENSE)

**Organization:** [ArkTechNWA](https://github.com/ArkTechNWA)

---

*Built with obsessive attention to reliability.*

---

## Why?

**The #1 reason:** If you use zsh, Claude Code's Bash tool causes quotation mismatches and shell confusion. Every debug loop costs tokens. zsh-tool eliminates this instantly and permanently.

**The token math:** One avoided debug spiral = 30+ seconds saved, hundreds of tokens preserved.

zsh-tool is **intelligent shell execution**:

| Problem | zsh-tool Solution |
|---------|-------------------|
| Bash/zsh quotation confusion | **Native zsh** — no shell mismatch, no debug loops |
| Commands hang forever | **Yield-based execution** — always get control back |
| No visibility into running commands | **zsh_poll** — incremental output collection |
| Can't interact with prompts | **PTY mode** + **zsh_send** — full interactive support |
| Can't type passwords | **PTY mode** — let Claude Code type its own passwords |
| Timeouts cascade | **NEVERHANG circuit breaker** — fail fast, auto-recover |
| No memory between calls | **A.L.A.N. 2.0** — retry detection, streak tracking, proactive insights |
| Polling wastes tokens | **Intelligent polling** — 2s listen window, adaptive suggestions, duration estimates |
| Blind kills, no learning | **Kill-aware A.L.A.N.** — classifies impatience vs genuine hangs |
| Retrying with wrong flags | **manopt** — auto-surfaces command options on repeated failures |
| No task management | **zsh_tasks**, **zsh_kill** — full control |

This is the difference between "run commands" and "intelligent shell integration."

---

## Features

### Yield-Based Execution
Commands return after `yield_after` seconds with partial output if still running:
- **No more hanging** — you always get control back
- **Incremental output** — collect with `zsh_poll`
- **Interactive input** — send with `zsh_send`
- **Task management** — `zsh_kill` and `zsh_tasks`

### PTY Mode
Full pseudo-terminal emulation for interactive programs:
```bash
# Enable with pty: true
zsh(command="pass insert mypass", pty=true)
# See prompts, send input with zsh_send
```
- Proper handling of interactive prompts
- Programs that require a TTY
- Color output and terminal escape sequences
- Full stdin/stdout/stderr merging

### NEVERHANG Circuit Breaker
Prevents hanging commands from blocking sessions:
- Tracks timeout patterns per command hash
- Opens circuit after 3 timeouts in rolling 1-hour window
- Auto-recovers after 5 minutes
- States: `CLOSED` (normal) → `OPEN` (blocking) → `HALF_OPEN` (testing)

### A.L.A.N. 2.0 (As Long As Necessary)
Intelligent short-term learning — *"Maybe you're fuckin' up, maybe you're doing it right."*

- **Retry Detection** — warns when you're repeating failed commands
- **Streak Tracking** — celebrates success streaks, warns on failure streaks
- **Fuzzy Matching** — `git push origin feature-1` → `git push origin *`
- **Proactive Insights** — contextual feedback before you run commands
- **Session Memory** — 15-minute rolling window tracks recent activity
- **Temporal Decay** — exponential decay (24h half-life), auto-prunes
- **SSH Intelligence** — separates host connectivity from remote command success
- **Pipeline Segment Tracking** — when `cat foo | grep -badopts | sort` fails, A.L.A.N. knows *which* segment failed

#### Intelligent Polling
`zsh_poll` performs a **2-second listen window** before returning. If output arrives within 2s, it comes back immediately. If not, poll metadata tells the agent what's happening:

| Field | What it tells you |
|-------|-------------------|
| `polls_since_output` | How many empty polls in a row |
| `elapsed_since_last_output_s` | Idle time since last output |
| `alan_estimate` | A.L.A.N.'s duration prediction based on command history |
| `suggestion` | Adaptive advice: space out polls, check soon, or consider killing |

Suggestions are advisory only — the agent always decides. A 2-minute `pip install` no longer generates 40 empty round-trips.

#### Kill-Aware A.L.A.N.
When the agent kills a command, A.L.A.N. records it as a `KILLED` outcome and classifies *why*:

| Category | Meaning | Example |
|----------|---------|---------|
| `EARLY_KILL` | Killed well before median completion | *"Killed at 30s. Median is 120s. Needs more time."* |
| `LATE_KILL` | Ran way past expected duration | *"Killed after 180s. Median is 45s. Something is wrong."* |
| `PATTERN_PROBLEM` | Template gets killed >50% of the time | *"This pattern may need a different approach entirely."* |

Kill classification compares `kill_elapsed / median_duration` to distinguish impatience from genuine hangs.

#### manopt — Man Page Options on Failure
When a command fails repeatedly, A.L.A.N. surfaces its available options:

- **1st failure** — normal feedback, no manopt
- **2nd failure** — triggers async `manopt` lookup in background (2s timeout)
- **3rd+ failure** — presents cached option table in A.L.A.N. insight

Parsed from local man pages. Cached in SQLite. On by default (`ALAN_MANOPT_ENABLED=1`).

#### SSH Tracking
A.L.A.N. treats SSH commands specially, recording two separate observations:

| Observation | What it tracks | Example insight |
|-------------|----------------|-----------------|
| **Host connectivity** | Can we connect to this host? | *"Host 'vps' has 67% connection failure rate"* |
| **Remote command** | Does this command work across hosts? | *"Remote command 'git pull' reliable across 3 hosts"* |

Exit code classification:
- `0` — Success (connected AND command succeeded)
- `255` — Connection failed (SSH couldn't connect)
- `1-254` — Command failed (connected but remote command failed)

This means when `ssh host3 'git pull'` fails with exit 255, A.L.A.N. knows the *host* was unreachable—not that `git pull` is broken.

---

## Tools

| Tool | Purpose |
|------|---------|
| `zsh` | Execute command with yield-based oversight |
| `zsh_poll` | Get more output from running task |
| `zsh_send` | Send input to task's stdin |
| `zsh_kill` | Kill a running task |
| `zsh_tasks` | List all active tasks |
| `zsh_health` | Overall health status |
| `zsh_alan_stats` | A.L.A.N. database statistics |
| `zsh_alan_query` | Query pattern insights for a command |
| `zsh_neverhang_status` | Circuit breaker state |
| `zsh_neverhang_reset` | Reset circuit to CLOSED |

---

## Installation

### From Marketplace (Recommended)

Add the ArkTechNWA marketplace to Claude Code:
```
ArkTechNWA/claude-plugins
```

Then install: `/plugin install arktechnwa/zsh-tool`

**That's it.** The plugin auto-installs dependencies on first run.

### Manual Installation

```bash
git clone https://github.com/ArkTechNWA/zsh-tool.git ~/.claude/plugins/zsh-tool
```

Enable in `~/.claude/settings.json`:
```json
{
  "enabledPlugins": {
    "zsh-tool": true
  }
}
```

The bundled `scripts/run-mcp.sh` creates a venv and installs automatically.

### Local Development

For local development/testing, the wrapper script automatically detects when `CLAUDE_PLUGIN_ROOT` isn't expanded and uses the calculated plugin root directory instead. No configuration changes needed.

Alternatively, create a `.mcp.local.json` with absolute paths:
```json
{
  "mcpServers": {
    "zsh-tool": {
      "type": "stdio",
      "command": "/path/to/zsh-tool/scripts/run-mcp.sh",
      "env": {
        "NEVERHANG_TIMEOUT_DEFAULT": "120",
        "NEVERHANG_TIMEOUT_MAX": "600"
      }
    }
  }
}
```

The `ALAN_DB_PATH` will be automatically set to `{plugin_root}/data/alan.db` if not explicitly provided.

---

## Architecture

```
zsh-tool/
├── .claude-plugin/
│   ├── plugin.json
│   └── CLAUDE.md
├── .mcp.json
├── src/
│   └── server.py      # MCP server
├── data/
│   └── alan.db        # A.L.A.N. SQLite database
├── .venv/             # Python virtual environment
└── README.md
```

---

## Configuration

Environment variables (set in .mcp.json):
- `ALAN_DB_PATH` — A.L.A.N. database location
- `NEVERHANG_TIMEOUT_DEFAULT` — Default timeout (120s)
- `NEVERHANG_TIMEOUT_MAX` — Maximum timeout (600s)
- `ALAN_MANOPT_ENABLED` — Enable man-page option hints on failure (default: `1`)
- `ALAN_MANOPT_TIMEOUT` — Max seconds to wait for manopt parsing (default: `2.0`)
- `ALAN_MANOPT_FAIL_TRIGGER` — Fail count to trigger async lookup (default: `2`)
- `ALAN_MANOPT_FAIL_PRESENT` — Fail count to present cached options (default: `3`)

### Disabling Bash (Optional)

To use zsh as the only shell, add to `~/.claude/settings.json`:
```json
{
  "permissions": {
    "deny": ["Bash"]
  }
}
```

---

## Changelog

### 0.5.0
**A.L.A.N. v2 Upgrade** — *Intelligent polling, kill awareness, manopt*
- **Intelligent polling**: 2s listen window in `zsh_poll` reduces empty round-trips; poll metadata with duration estimates and adaptive suggestions
- **Kill-aware A.L.A.N.**: `KILLED` outcome type with elapsed tracking; classifies early kills (impatience), late kills (genuine hangs), and pattern problems (wrong approach)
- **manopt integration**: Async man-page option parsing on repeated command failures; cached in SQLite; presented on 3rd+ failure for the same command template
- New `outcome_type` and `kill_elapsed_ms` columns on observations
- New `manopt_cache` table for persistent man-page option storage
- ENV vars: `ALAN_MANOPT_ENABLED`, `ALAN_MANOPT_TIMEOUT`, `ALAN_MANOPT_FAIL_TRIGGER`, `ALAN_MANOPT_FAIL_PRESENT`

### 0.4.90
**Feedback Improvements** — *Better signal, less noise*
- ALAN insights now classified as info/warning tuples
- Command awareness: grep exit 1 = "no match" (info), exit 127 = "command not found" (warning)
- Post-execution insights: silent detection, pipe masking warnings, SIGPIPE exclusion
- ANSI coloring on metadata lines (green=success, red=failure, cyan=running, yellow=timeout)
- COMPLETED/FAILED status word based on exit code
- Raw pipestatus lists replace formatted `[cmd:code]` strings
- Grouped insight display: `[info: A.L.A.N.: ...]` and `[warning: A.L.A.N.: ...]`

### 0.4.83
**Python 3.14 Support** — *Future-proofing*
- Added Python 3.14 classifier and badge
- Removed deprecated `asyncio.DefaultEventLoopPolicy` fixture (slated for removal in 3.16)
- All 331 tests pass on Python 3.14.2

### 0.4.81
**Pipestatus Marker Leak Fix** — *Data integrity*
- Fixed race condition where `___ZSH_PIPESTATUS_MARKER___` could leak into output
- Marker now stripped in `_build_task_response()` before returning to caller
- Prevents corrupted file content when output is captured mid-execution
- CI: Runner switched to docker executor, added PEP 668 compliance

### 0.4.80
**Per-Segment Exit Codes** — *Know exactly which command failed*
- Exit codes now show `[cmd1:0,cmd2:1,cmd3:0]` format instead of single integer
- Each pipeline segment paired with its actual exit status from zsh `$pipestatus`
- A.L.A.N. learning receives accurate per-command outcomes
- Self-documenting output for human and AI analysis
- Fixes bug where all commands reported `exit=0` regardless of actual status

### 0.4.79
**Server Modular Refactoring** — *Cleaner architecture*
- Extracted MCP server into `zsh_tool/server.py` module
- Centralized configuration in `zsh_tool/config.py`
- Fixed plugin.json version sync with package version

### 0.4.75
**Pipeline Intelligence** — *Know which segment of your pipeline is failing*
- A.L.A.N. now captures zsh's `$pipestatus` array for every pipeline
- Each segment recorded as independent observation with its own exit code
- When `cat foo | grep -badopts | sort` fails, you know *grep* was the problem
- Quote/escape-aware pipeline parsing handles complex commands correctly
- Backwards compatible: full pipeline still recorded alongside segments
- 248 new test lines covering segment tracking and edge cases

### 0.4.6
**Configuration & Polish** — *User-configurable defaults, 91% coverage*
- User config file (`~/.config/zsh-tool/config.yaml`) for custom yield_after
- Test coverage improved: 303 tests, 91% coverage
- Fixed null-check bug in task cleanup
- Logo files consolidated and fixed

### 0.4.5
**Bundled Plugin** — *Zero-friction marketplace install*
- Auto-install wrapper (`scripts/run-mcp.sh`) creates venv on first run
- Portable `.mcp.json` using `${CLAUDE_PLUGIN_ROOT}`
- ArkTechNWA marketplace support
- No manual pip install required

### 0.4.0
**Test Suite & CI** — *290 tests, 89% coverage*
- Comprehensive test suite covering all modules
- CI pipeline with test and lint stages
- Dynamic pipeline and coverage badges
- Gentle test runner (`run_tests.sh`) with nice and sleep between files
- Fixed deprecation warnings and lint errors
- Added pytest-asyncio for async test support

### 0.3.1
**SSH Intelligence** — *Separate host connectivity from remote command success*
- SSH commands now record dual observations (host + remote command)
- Exit code classification: 0=success, 255=connection_failed, 1-254=command_failed
- New `ssh_observations` table for SSH-specific tracking
- `get_ssh_host_stats()` — per-host connection/command success rates
- `get_ssh_command_stats()` — per-command stats across all hosts
- SSH-specific insights: flaky hosts, reliable hosts, failing commands
- 31 new tests for SSH tracking

### 0.3.0
**A.L.A.N. 2.0** — *"Maybe you're fuckin' up, maybe you're doing it right."*
- Retry detection: warns when repeating failed commands
- Streak tracking: celebrates success, warns on failure
- Fuzzy template matching: similar commands grouped
- Proactive insights: contextual feedback before execution
- Session memory: 15-minute rolling window
- New database tables: `recent_commands`, `streaks`

### 0.2.0
- Yield-based execution with live oversight
- PTY mode for full terminal emulation
- Interactive input support via `zsh_send`
- Task management: `zsh_poll`, `zsh_kill`, `zsh_tasks`
- Fixed stdin blocking with subprocess.PIPE

### 0.1.0
- Initial release
- NEVERHANG circuit breaker
- A.L.A.N. learning database

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

<p align="center">
  <b>For Johnny5. For us.</b><br>
  <i>ArkTechNWA</i>
</p>
