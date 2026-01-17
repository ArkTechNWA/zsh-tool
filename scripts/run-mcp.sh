#!/usr/bin/env bash
# zsh-tool MCP server launcher
# Auto-creates venv and installs on first run

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PLUGIN_ROOT}/.venv"
PYTHON="${VENV_DIR}/bin/python"

# Handle ALAN_DB_PATH for both local dev and marketplace installs
# If ALAN_DB_PATH contains unexpanded variables or isn't set, use default
if [[ -z "$ALAN_DB_PATH" || "$ALAN_DB_PATH" == *'${'* ]]; then
    export ALAN_DB_PATH="${PLUGIN_ROOT}/data/alan.db"
    echo "zsh-tool: Using default ALAN_DB_PATH: $ALAN_DB_PATH" >&2
fi

# Ensure data directory exists
mkdir -p "$(dirname "$ALAN_DB_PATH")"

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
