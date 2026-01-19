# Issue #19: Refactor server.py into Modular Components

## Current State Analysis

**server.py: 1861 lines**

| Section | Lines | Target Module |
|---------|-------|---------------|
| Configuration | 35-98 | `config.py` |
| Pipeline helpers | 98-150 | `alan.py` (internal) |
| ALAN class | 150-1063 | `alan.py` (~913 lines) |
| CircuitBreaker | 1063-1134 | `neverhang.py` (~70 lines) |
| Task management | 1134-1588 | `tasks.py` (~454 lines) |
| MCP handlers | 1588-1861 | `server.py` (~273 lines) |

**Tests already modular:**
- `test_alan_*.py` (5 files, 1900 lines)
- `test_neverhang.py` (303 lines)
- `test_task_*.py` (3 files, 1080 lines)
- `test_config.py` (117 lines)
- `test_mcp_handlers.py` (464 lines)

---

## Target Structure

```
zsh_tool/
├── __init__.py          # Version, public exports
├── server.py            # MCP registration + handlers (~300 lines)
├── tasks.py             # LiveTask, execute_zsh_*, poll, send, kill (~500 lines)
├── alan.py              # ALAN class + pipeline helpers (~950 lines)
├── neverhang.py         # CircuitState, CircuitBreaker (~100 lines)
└── config.py            # All constants + user config loading (~100 lines)
```

---

## Execution Plan

### Phase 1: Extract config.py
**Checkpoint: PHASE-1-CONFIG**

1. Create `zsh_tool/config.py`
   - Move all constants (ALAN_*, NEVERHANG_*, CONFIG_PATH, etc.)
   - Move `_load_user_config()` function
   - Move `_user_config` variable

2. Update `server.py` imports
   - `from .config import ...`

3. Run tests: `pytest tests/test_config.py -v`

4. **Comment checkpoint on Issue #19**

---

### Phase 2: Extract neverhang.py
**Checkpoint: PHASE-2-NEVERHANG**

1. Create `zsh_tool/neverhang.py`
   - Move `CircuitState` enum
   - Move `CircuitBreaker` class
   - Import constants from config

2. Update `server.py`
   - `from .neverhang import CircuitState, CircuitBreaker`
   - Keep `circuit_breaker` instance in server.py (or tasks.py)

3. Run tests: `pytest tests/test_neverhang.py -v`

4. **Comment checkpoint on Issue #19**

---

### Phase 3: Extract alan.py
**Checkpoint: PHASE-3-ALAN**

1. Create `zsh_tool/alan.py`
   - Move `_wrap_for_pipestatus()` and `_extract_pipestatus()`
   - Move `ALAN` class
   - Import constants from config

2. Update `server.py`
   - `from .alan import ALAN, _wrap_for_pipestatus, _extract_pipestatus`
   - Keep `alan` instance in server.py (or tasks.py)

3. Run tests: `pytest tests/test_alan_*.py -v`

4. **Comment checkpoint on Issue #19**

---

### Phase 4: Extract tasks.py
**Checkpoint: PHASE-4-TASKS**

1. Create `zsh_tool/tasks.py`
   - Move `LiveTask` dataclass
   - Move `live_tasks` dict
   - Move `_cleanup_task()`
   - Move `_output_collector()`, `_pty_output_collector()`
   - Move `execute_zsh_yielding()`, `execute_zsh_pty()`
   - Move `_build_task_response()`
   - Move `poll_task()`, `send_to_task()`, `kill_task()`, `list_tasks()`
   - Import from alan, neverhang, config

2. Update `server.py`
   - `from .tasks import execute_zsh_yielding, execute_zsh_pty, ...`

3. Run tests: `pytest tests/test_task_*.py -v`

4. **Comment checkpoint on Issue #19**

---

### Phase 5: Clean up server.py
**Checkpoint: PHASE-5-SERVER**

1. server.py now contains only:
   - MCP Server creation
   - `list_tools()` handler
   - `call_tool()` handler
   - `_handle_tool_call()`
   - `_format_task_output()`
   - `main()` and `run()` entry points
   - Instance creation (`alan`, `circuit_breaker`)

2. Verify all imports work correctly

3. Run full test suite: `pytest tests/ -v --cov=zsh_tool`

4. **Comment checkpoint on Issue #19**

---

### Phase 6: Final Verification
**Checkpoint: PHASE-6-COMPLETE**

1. Run full test suite with coverage
2. Verify coverage >= 89% (current baseline)
3. Manual smoke test: start MCP server, run commands
4. Update `__init__.py` exports if needed

5. **Final comment on Issue #19 with results**

---

## Dependencies Between Modules

```
config.py          <- standalone, no internal deps
    ↑
neverhang.py       <- imports config
    ↑
alan.py            <- imports config
    ↑
tasks.py           <- imports config, alan, neverhang
    ↑
server.py          <- imports tasks, alan, neverhang, config
```

---

## Risk Mitigation

- **Circular imports**: Follow dependency order above
- **Instance sharing**: `alan` and `circuit_breaker` instances created in server.py, passed to tasks
- **Test breakage**: Tests import from `zsh_tool.server` currently; may need updates
- **Coverage drop**: Run coverage after each phase

---

## Rollback Plan

Each phase is a separate commit. If tests fail:
1. `git stash` or `git reset --hard HEAD~1`
2. Fix issue
3. Re-attempt

---

## Commit Message Template

```
refactor(phase-N): extract MODULE.py from server.py

- Move CLASS/FUNCTION to MODULE.py
- Update imports in server.py
- Tests passing: X/X

Part of #19

Co-Authored-By: AI <mod+ai_dev@arktechnwa.com>
```
