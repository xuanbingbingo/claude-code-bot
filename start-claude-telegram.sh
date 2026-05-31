#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/claude-telegram.log"

# 加载 .env（如果存在）
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/claude-telegram.py" >> "$LOG_FILE" 2>&1
