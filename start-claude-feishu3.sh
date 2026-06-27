#!/bin/bash
# 第二个飞书 bot（claude_gateway 复用同一份 claude-feishu.py，仅换 .env.feishu3 凭证）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/claude-feishu3.log"

# 先 source bot2 专用 env（set -a 导出后，claude-feishu.py 内置的 .env setdefault 不会覆盖）
if [ -f "$SCRIPT_DIR/.env.feishu3" ]; then
    set -a
    source "$SCRIPT_DIR/.env.feishu3"
    set +a
fi

/Users/libin/aiProjects/claude-gateway/venv/bin/python "$SCRIPT_DIR/claude-feishu.py" >> "$LOG_FILE" 2>&1
