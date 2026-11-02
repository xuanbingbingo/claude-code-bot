#!/bin/bash
# 通用多 bot 启动脚本：同一份 claude-feishu.py 以不同「角色/人设 + 凭证」跑成多个独立 bot。
# 用法: bash start-bot.sh <role>
#   例如: bash start-bot.sh research
# 会读取同目录下的 .env.<role>（含该 bot 的飞书 App ID/Secret + CLAUDE_CWD + 人设），
# 然后用本仓库 venv（没有则回退系统 python3）启动网关。
# .env.<role> 含私有凭证，已被 .gitignore 忽略，不会入库；模板见 .env.role.example。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROLE="$1"
if [ -z "$ROLE" ]; then
    echo "用法: bash start-bot.sh <role>   (会读取 .env.<role>)"
    exit 1
fi

ENV_FILE="$SCRIPT_DIR/.env.$ROLE"
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ 找不到 $ENV_FILE"
    echo "   请从 .env.role.example 复制一份为 .env.$ROLE 并填好该 bot 的飞书凭证。"
    exit 1
fi

LOG_FILE="$SCRIPT_DIR/bot-$ROLE.log"

# set -a 让 source 进来的变量自动 export，确保 CLAUDE_CWD 在 python 导入前就生效
set -a
source "$ENV_FILE"
export BOT_PLATFORM="${BOT_PLATFORM:-feishu}"      # 默认飞书;.env 可覆盖
set +a

# 飞书/企微/钉钉是国内域名,一律直连:macOS 系统代理指向 17891(0dcloud),它没开时
# 飞书 SDK 读到系统代理会连不上,所有 bot 掉线(2026-11-03 踩过)
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,.feishu.cn,.feishu.net,.larksuite.com,.larkoffice.com,.qq.com,.weixin.qq.com,.dingtalk.com"
export no_proxy="$NO_PROXY"

# 优先用仓库内 venv，没有则回退系统 python3
export PYTHONUNBUFFERED=1
PYTHON="$SCRIPT_DIR/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

# 新分层架构统一入口(按 BOT_PLATFORM × BOT_BACKEND 装配)
"$PYTHON" "$SCRIPT_DIR/run.py" >> "$LOG_FILE" 2>&1
