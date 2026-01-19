"""
Configuration for zsh-tool MCP server.

All constants and user config loading.
"""

import os
from pathlib import Path

# =============================================================================
# Configuration
# =============================================================================

ALAN_DB_PATH = Path(os.environ.get("ALAN_DB_PATH", "~/.claude/plugins/zsh-tool/data/alan.db")).expanduser()
NEVERHANG_TIMEOUT_DEFAULT = int(os.environ.get("NEVERHANG_TIMEOUT_DEFAULT", 3600))  # 1 hour - effectively never auto-kills
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

# A.L.A.N. 2.0 settings
ALAN_RECENT_WINDOW_SIZE = 50      # Track last N commands
ALAN_RECENT_WINDOW_MINUTES = 10   # For retry detection
ALAN_STREAK_THRESHOLD = 3         # Min streak to report

# Pipeline segment tracking (Issue #20)
PIPESTATUS_MARKER = "___ZSH_PIPESTATUS_MARKER_f9a8b7c6___"

# User config file
CONFIG_PATH = Path("~/.config/zsh-tool/config.yaml").expanduser()


def _load_user_config() -> dict:
    """
    Load user configuration from ~/.config/zsh-tool/config.yaml.

    Returns dict with config values, empty dict if file doesn't exist.
    Uses simple parsing to avoid yaml dependency.
    """
    config = {}
    if not CONFIG_PATH.exists():
        return config

    try:
        content = CONFIG_PATH.read_text()
        for line in content.splitlines():
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Simple key: value parsing
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                # Parse numeric values
                if key == 'yield_after':
                    try:
                        config[key] = float(value)
                    except ValueError:
                        pass  # Invalid value, skip
    except Exception:
        pass  # Config read failed, use defaults

    return config


# Load user config at startup
_user_config = _load_user_config()

# Derived config values
YIELD_AFTER_DEFAULT = _user_config.get('yield_after', 2.0)  # From ~/.config/zsh-tool/config.yaml or default
