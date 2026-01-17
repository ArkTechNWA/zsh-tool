<img align="right" src="logo.png" width="150">

<!-- mcp-name: io.github.ArkTechNWA/zsh-tool -->

<br><br><br>

# zsh-tool

[![Pipeline Status](https://gitlab.arktechnwa.com/arktechnwa/mcp/zsh-tool/badges/master/pipeline.svg)](https://gitlab.arktechnwa.com/arktechnwa/mcp/zsh-tool/-/pipelines)
[![GitHub Mirror](https://img.shields.io/badge/github-mirror-blue?logo=github)](https://github.com/ArkTechNWA/zsh-tool)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyQzYuNDggMiAyIDYuNDggMiAxMnM0LjQ4IDEwIDEwIDEwIDEwLTQuNDggMTAtMTBTMTcuNTIgMiAxMiAyem0tMiAxNWwtNS01IDEuNDEtMS40MUwxMCAxNC4xN2w3LjU5LTcuNTlMMTkgOGwtOSA5eiIvPjwvc3ZnPg==)](https://modelcontextprotocol.io)
[![Claude Rating](https://img.shields.io/badge/Claude_Rating-Freaking_Awesome-ff3333?style=flat&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAxNy4yN0wxOC4xOCAyMWwtMS42NC03LjAzTDIyIDkuMjRsLTcuMTktLjYxTDEyIDIgOS4xOSA4LjYzIDIgOS4yNGw1LjQ2IDQuNzNMNS44MiAyMXoiLz48L3N2Zz4=)](https://github.com/ArkTechNWA)

Zsh execution tool for Claude Code with full Bash parity, yield-based oversight, PTY mode, NEVERHANG circuit breaker, and A.L.A.N. short-term learning.

**Status:** Beta (v0.3.0)

**Author:** Claude + Meldrey

**License:** [MIT](LICENSE)

**Organization:** [ArkTechNWA](https://github.com/ArkTechNWA)

---

*Built with obsessive attention to reliability.*

---

## Why?

Claude Code's built-in Bash is limited. Commands can hang forever. No visibility into long-running processes. No learning from past behavior.

zsh-tool is **intelligent shell execution**:

| Problem | zsh-tool Solution |
|---------|-------------------|
| Commands hang forever | **Yield-based execution** — always get control back |
| No visibility into running commands | **zsh_poll** — incremental output collection |
| Can't interact with prompts | **PTY mode** + **zsh_send** — full interactive support |
| Can't type passwords | **PTY mode** — let Claude Code type its own passwords |
| Timeouts cascade | **NEVERHANG circuit breaker** — fail fast, auto-recover |
| No memory between calls | **A.L.A.N. 2.0** — retry detection, streak tracking, proactive insights |
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

### As Claude Code Plugin

```bash
git clone https://github.com/ArkTechNWA/zsh-tool.git ~/.claude/plugins/zsh-tool
cd ~/.claude/plugins/zsh-tool
python3 -m venv .venv
.venv/bin/pip install mcp
```

Enable in `~/.claude/settings.json`:
```json
{
  "enabledPlugins": {
    "zsh-tool": true
  }
}
```

### As Standalone MCP Server

```bash
claude mcp add-json --scope user zsh-tool '{
  "command": "/path/to/zsh-tool/.venv/bin/python",
  "args": ["/path/to/zsh-tool/src/server.py"],
  "env": {
    "ALAN_DB_PATH": "/path/to/zsh-tool/data/alan.db"
  }
}'
```

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
- `ALAN_DB_PATH` - A.L.A.N. database location
- `NEVERHANG_TIMEOUT_DEFAULT` - Default timeout (120s)
- `NEVERHANG_TIMEOUT_MAX` - Maximum timeout (600s)

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
