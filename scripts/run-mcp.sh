#!/usr/bin/env bash
# zsh-tool MCP server launcher
# Auto-creates venv and installs on first run

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PLUGIN_ROOT}/.venv"
PYTHON="${VENV_DIR}/bin/python"

# Create venv if missing
if [ ! -f "$PYTHON" ]; then
    echo "zsh-tool: Creating virtual environment..." >&2
    python3 -m venv "$VENV_DIR"
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e "$PLUGIN_ROOT"
    echo "zsh-tool: Installation complete." >&2
fi

# Run the MCP server
exec "$PYTHON" -c "from zsh_tool.server import run; run()"
