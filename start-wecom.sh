#!/bin/bash
# 企业微信渠道多角色启动器(对标 start-bot.sh,跑 claude-wecom.py)。
# 用法: bash start-wecom.sh <role>   会读取同目录 .env.wecom-<role>
#   例: bash start-wecom.sh research
# .env.wecom-<role> 含 WECOM_BOT_ID/SECRET + CLAUDE_CWD + 人设,已被 .gitignore 忽略。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROLE="$1"
if [ -z "$ROLE" ]; then
    echo "用法: bash start-wecom.sh <role>   (读取 .env.wecom-<role>)"
    exit 1
fi

ENV_FILE="$SCRIPT_DIR/.env.wecom-$ROLE"
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ 找不到 $ENV_FILE"
    echo "   请从 .env.wecom.role.example 复制一份为 .env.wecom-$ROLE 并填好企微机器人凭证。"
    exit 1
fi

LOG_FILE="$SCRIPT_DIR/bot-wecom-$ROLE.log"

set -a
source "$ENV_FILE"
set +a

PYTHON="$SCRIPT_DIR/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

"$PYTHON" "$SCRIPT_DIR/claude-wecom.py" >> "$LOG_FILE" 2>&1
