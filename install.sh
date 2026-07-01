#!/usr/bin/env bash
# claude-gateway 一键安装：Python venv + 核心依赖 + .env 模板
# 用法：
#   ./install.sh                装核心依赖（飞书 + Telegram）
#   ./install.sh --with-voice   额外装语音转写依赖 faster-whisper
set -euo pipefail
cd "$(dirname "$0")"

info(){ printf '\033[36m%s\033[0m\n' "$*"; }
ok(){   printf '\033[32m✅ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m⚠️  %s\033[0m\n' "$*"; }
err(){  printf '\033[31m❌ %s\033[0m\n' "$*"; }

# ---------- 1. Python 3.10+ ----------
if ! command -v python3 >/dev/null 2>&1; then
  err "未找到 python3，请先安装 Python 3.10+（https://www.python.org）"; exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
if ! python3 -c 'import sys;exit(0 if sys.version_info[:2]>=(3,10) else 1)'; then
  err "Python 版本过低（当前 $PYV），需要 3.10+"; exit 1
fi
ok "Python $PYV"

# ---------- 2. venv + 核心依赖 ----------
if [ ! -d venv ]; then
  info "创建虚拟环境 venv ..."
  python3 -m venv venv
fi
info "安装核心依赖（httpx / lark-oapi / python-telegram-bot / websockets）..."
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
ok "核心依赖就绪"

# ---------- 3. 可选：语音转写 ----------
if [ "${1:-}" = "--with-voice" ]; then
  info "安装语音转写依赖 faster-whisper（较大，首次运行会下模型）..."
  ./venv/bin/pip install -q faster-whisper && ok "faster-whisper 就绪"
else
  warn "未装语音转写。要收发语音消息时重跑： ./install.sh --with-voice"
fi

# ---------- 4. .env 模板 ----------
if [ ! -f .env ]; then
  cp .env.example .env
  ok "已生成 .env（从 .env.example 复制）—— 去填飞书/Telegram 凭证"
else
  warn ".env 已存在，跳过（不覆盖你现有凭证）"
fi

# ---------- 5. Claude Code CLI 检查（执行引擎，绕不开） ----------
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI 已安装"
else
  warn "未检测到 claude CLI —— 这是实际执行引擎，必须装：https://docs.claude.com/en/docs/claude-code"
fi

echo ""
ok "安装完成。下一步："
echo "  1. 编辑 .env 填凭证（飞书三项 / Telegram 两项，见 README）"
echo "  2. 启动： 飞书 ./start-claude-feishu.sh   ｜   Telegram ./start-claude-telegram.sh"
echo "  3. 加多角色 bot： bash scripts/new-agent.sh <role>（见 README「多角色」章节）"
