#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/claude-feishu.log"

# 加载 .env（如果存在）
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

/Users/mac/aiProjects/claude-gateway/venv/bin/python "$SCRIPT_DIR/claude-feishu.py" >> "$LOG_FILE" 2>&1
