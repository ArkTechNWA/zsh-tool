# zsh-tool

Zsh execution tool for Claude Code with full Bash parity, yield-based oversight, PTY mode, NEVERHANG circuit breaker, and A.L.A.N. short-term learning.

**For Johnny5. For us.**

## Features

### `zsh` Tool
Full-parity zsh command execution with yield-based oversight:
- `command` (required) - The zsh command to execute
- `timeout` (optional) - Timeout in seconds (default: 120, max: 600)
- `yield_after` (optional) - Return control after this many seconds if still running (default: 7.0)
- `description` (optional) - Human-readable description of what this command does
- `pty` (optional) - Use PTY mode for full terminal emulation

### Yield-Based Execution
Commands return after `yield_after` seconds with partial output if still running:
- No more hanging - you always get control back
- Incremental output collection with `zsh_poll`
- Interactive input with `zsh_send`
- Task management with `zsh_kill` and `zsh_tasks`

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
- States: CLOSED (normal) → OPEN (blocking) → HALF_OPEN (testing)

### A.L.A.N. (As Long As Necessary)
Short-term learning database with temporal decay:
- Records command patterns, exit codes, durations
- Warns about commands that historically timeout
- Exponential decay (half-life: 24 hours)
- Auto-prunes entries below 1% weight

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

## Installation

### As Claude Code Plugin

Clone to plugins directory:
```bash
git clone ssh://git@gitlab.arktechnwa.com:2222/johnny5/zsh-tool.git ~/.claude/plugins/zsh-tool
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

Add to `~/.claude.json` via:
```bash
claude mcp add-json --scope user zsh-tool '{
  "command": "/path/to/zsh-tool/.venv/bin/python",
  "args": ["/path/to/zsh-tool/src/server.py"],
  "env": {
    "ALAN_DB_PATH": "/path/to/zsh-tool/data/alan.db"
  }
}'
```

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
│   └── alan.db        # A.L.A.N. SQLite database (created automatically)
├── .venv/             # Python virtual environment (create on install)
└── README.md
```

## Configuration

Environment variables (set in .mcp.json):
- `ALAN_DB_PATH` - A.L.A.N. database location
- `NEVERHANG_TIMEOUT_DEFAULT` - Default timeout (120s)
- `NEVERHANG_TIMEOUT_MAX` - Maximum timeout (600s)

## Disabling Bash (Optional)

To use zsh as the only shell, add to `~/.claude/settings.json`:
```json
{
  "permissions": {
    "deny": ["Bash"]
  }
}
```

## Version

0.2.0-beta.1 - Yield-based execution, PTY mode, interactive input support

## Changelog

### 0.2.0-beta.1
- Yield-based execution with live oversight (Issue #1)
- PTY mode for full terminal emulation
- Interactive input support via `zsh_send`
- Task management: `zsh_poll`, `zsh_kill`, `zsh_tasks`
- Fixed stdin blocking with subprocess.PIPE

### 0.1.0
- Initial release
- NEVERHANG circuit breaker
- A.L.A.N. learning database

## License

MIT
