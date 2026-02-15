# Phase 2: ALAN Rust Port — Design

## Goal

Move ALAN recording (the write path) into the Rust executor binary. Python becomes read-only for insights. Clean separation: Rust writes, Python reads, SQLite DB is the contract.

## Architecture

The `zsh-tool-exec` binary gains two new flags:

- `--db <path>` — path to ALAN SQLite database
- `--session-id <id>` — 8-char session identifier (passed from Python)

After command execution completes, the executor:

1. Opens the SQLite DB (creates tables if needed)
2. Hashes and templates the command
3. Writes to `observations`, `recent_commands`, `streaks`
4. If pipeline (multiple pipestatus values), parses segments and writes per-segment records
5. Prunes old `recent_commands` entries
6. Writes the JSON meta-file
7. Exits with the command's exit code

## Data Flow

```
Python (tasks.py)                    Rust (zsh-tool-exec)
-----------------                    --------------------
spawn executor with:
  --db <alan.db path>
  --session-id <uuid8>
  --meta /tmp/zsh-meta-XXX.json
  --timeout 120
  -- "command"
                                     execute command (pipe or pty)
                                     |
                                     open alan.db (rusqlite)
                                     write observations
                                     write recent_commands
                                     update streaks
                                     write pipeline segments (if multi-pipe)
                                     prune old recent_commands
                                     |
                                     write meta-file (JSON)
                                     exit with command's exit code

read meta-file (pipestatus, timing)
generate insights (read-only DB queries)
format response for MCP
```

## Scope: Core Only

### Moves to Rust (Phase 2)

- `_hash_command()` — SHA256 normalization
- `_template_command()` — fuzzy template generation
- `_parse_pipeline()` — pipe-aware command splitting
- `_update_streak()` — streak table updates
- `record()` core path — observations + recent_commands + streaks + pipeline segments
- Recent commands cleanup (prune old entries)
- DB schema initialization (all 6 tables, for forward compatibility)

### Stays in Python (Phase 2) — TODO(phase3)

- `get_insights()` — pre-execution insight generation
- `get_post_insights()` — post-execution exit code meanings, pipe masking
- `_get_ssh_insights()` — SSH host/command analysis
- SSH recording (only triggers for `ssh` commands)
- `_trigger_manopt_async()` / `_run_manopt()` — man page caching
- All stats/query methods (`get_stats`, `get_pattern_stats`, etc.)

Each gets a `# TODO(phase3): port to Rust MCP server` comment.

## New Rust Modules

```
zsh-tool-rs/src/
  main.rs          (add --db, --session-id flags)
  executor.rs      (unchanged)
  meta.rs          (unchanged)
  alan/
    mod.rs         (ALAN struct, record(), schema init)
    hash.rs        (command hashing + templating)
    pipeline.rs    (pipeline parsing)
    streak.rs      (streak tracking)
```

## Python Changes

- `tasks.py` `_rust_output_collector`: stop calling `alan.record()` for core tables (Rust already recorded). Still call for SSH recording and manopt triggering.
- `config.py`: `ALAN_DB_PATH` passed to executor as `--db`.
- `alan.py` `record()`: guard — if Rust executor active, skip core recording, still do SSH + manopt.
- All insight/query methods: unchanged (read-only, same DB).

## Schema Compatibility

Rust creates all 6 tables with `CREATE TABLE IF NOT EXISTS` — identical DDL to Python. The DB is backward-compatible: downgrading to Python-only still works.

## Dependencies

- `rusqlite` with `bundled` feature (statically links SQLite, no system dependency)

## Decisions

- **Recording in executor (not separate binary)**: simplest path, no extra IPC, executor has all data at the right moment
- **Core only (not SSH/manopt)**: SSH is conditional side-path, manopt is async fire-and-forget needing an event loop. Both port naturally in Phase 3.
- **Insights stay in Python**: they run before/after execution, need access to the full DB. Move in Phase 3 with the MCP server.
