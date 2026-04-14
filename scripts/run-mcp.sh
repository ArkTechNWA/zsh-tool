#!/usr/bin/env bash
# zsh-tool MCP server launcher
# Builds and runs the Rust binary in serve mode

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"
BINARY="${PLUGIN_ROOT}/zsh-tool-rs/target/release/zsh-tool-exec"

# Handle ALAN_DB_PATH for both local dev and marketplace installs
if [[ -z "$ALAN_DB_PATH" || "$ALAN_DB_PATH" == *'${'* ]]; then
    export ALAN_DB_PATH="${PLUGIN_ROOT}/data/alan.db"
    echo "zsh-tool: Using default ALAN_DB_PATH: $ALAN_DB_PATH" >&2
fi

# Ensure data directory exists
mkdir -p "$(dirname "$ALAN_DB_PATH")"

# Build if binary missing or older than source/Cargo.toml
NEEDS_BUILD=false
if [ ! -f "$BINARY" ]; then
    NEEDS_BUILD=true
elif [ "$(find "${PLUGIN_ROOT}/zsh-tool-rs/src" "${PLUGIN_ROOT}/zsh-tool-rs/Cargo.toml" -newer "$BINARY" 2>/dev/null | head -1)" ]; then
    NEEDS_BUILD=true
fi

if [ "$NEEDS_BUILD" = true ]; then
    echo "zsh-tool: Source changed, rebuilding..." >&2
    cd "${PLUGIN_ROOT}/zsh-tool-rs"
    cargo build --release --quiet 2>&1 >&2
    touch "$BINARY" 2>/dev/null || true
    echo "zsh-tool: Build complete." >&2
fi

LOGFILE="/tmp/zsh-tool-mcp.log"
echo "--- $(date) --- PID $$ ---" >> "$LOGFILE"
echo "PLUGIN_ROOT=$PLUGIN_ROOT" >> "$LOGFILE"
echo "BINARY=$BINARY" >> "$LOGFILE"
echo "ALAN_DB_PATH=$ALAN_DB_PATH" >> "$LOGFILE"
exec "$BINARY" serve 2>> "$LOGFILE"
