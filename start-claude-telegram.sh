#!/bin/bash
# 加载 .env（如果存在）
if [ -f "$(dirname "$0")/.env" ]; then
    set -a
    source "$(dirname "$0")/.env"
    set +a
fi
python3 "$(dirname "$0")/claude-telegram.py"
