#!/bin/bash
# 一键创建一个「角色 bot」：生成 .env.<role> + launchd 自启配置 + 启动。
# 把同一份 claude-feishu.py 以「不同凭证 + 不同人设」跑成一个新的独立飞书 bot。
#
# 建飞书应用有两种方式：
#   1) 扫码自动建（推荐）：不传 App ID/Secret，脚本会弹出二维码，用手机飞书扫码即在
#      账号中心一键建好应用（预配机器人能力/权限/事件订阅），自动拿到 App ID/Secret。
#   2) 手动填凭证：已在飞书开放平台建好应用的，把 App ID/Secret 传进来走原路径。
#
# 用法：
#   扫码建应用： bash scripts/new-agent.sh <role> [人设md路径] [显示名]
#   手动填凭证： bash scripts/new-agent.sh <role> <App_ID> <App_Secret> [人设md路径] [显示名]
# 例：
#   bash scripts/new-agent.sh researcher ~/.claude/agents/quant-research.md 研究分析师
#   bash scripts/new-agent.sh researcher cli_xxx secret_yyy ~/.claude/agents/quant-research.md 研究分析师
#
# 可选环境变量：
#   CLAUDE_CWD=/path   该 bot 的 Claude 工作目录（默认 $HOME）
#   START=0            只生成配置、不启动（默认启动）

set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"        # 仓库根目录
ROLE="$1"
CWD="${CLAUDE_CWD:-$HOME}"
OWNER_OPEN_ID=""

if [ -z "$ROLE" ]; then
    echo "用法:"
    echo "  扫码建应用（推荐）: bash scripts/new-agent.sh <role> [人设md路径] [显示名]"
    echo "  手动填凭证:        bash scripts/new-agent.sh <role> <App_ID> <App_Secret> [人设md路径] [显示名]"
    exit 1
fi

if [[ "$2" == cli_* ]]; then
    # —— 手动路径：显式传了 App ID（cli_ 开头）+ Secret ——
    APP_ID="$2"; APP_SECRET="$3"; PERSONA="$4"; BOT_NAME="${5:-$1}"
    if [ -z "$APP_SECRET" ]; then
        echo "❌ 传了 App ID 但缺 App Secret。"; exit 1
    fi
else
    # —— 扫码路径：没传凭证，走设备码流扫码自动建应用 ——
    PERSONA="$2"; BOT_NAME="${3:-$1}"
    echo "🔨 未提供 App ID/Secret，启动扫码建应用（手机飞书扫码）..."
    # 优先用仓库 venv 的 python（httpx/qrcode 装在这儿），没有再退系统 python3
    PYBIN="$REPO/venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="python3"
    CREDS="$("$PYBIN" "$REPO/scripts/feishu_setup.py" --role "$ROLE")" || {
        echo "❌ 扫码建应用失败或取消，已中止。"; exit 1
    }
    read -r APP_ID APP_SECRET OWNER_OPEN_ID <<< "$CREDS"
    if [ -z "$APP_ID" ] || [ -z "$APP_SECRET" ]; then
        echo "❌ 扫码建应用未拿到凭证，已中止。"; exit 1
    fi
fi

# 1) 生成 .env.<role>（含密钥，已被 .gitignore 忽略，不入库）
ENV_FILE="$REPO/.env.$ROLE"
[ -e "$ENV_FILE" ] && { cp "$ENV_FILE" "$ENV_FILE.bak"; echo "⚠️  $ENV_FILE 已存在，备份为 .bak"; }
{
    echo "# 角色 bot：$BOT_NAME（new-agent.sh 生成）"
    echo "FEISHU_APP_ID=$APP_ID"
    echo "FEISHU_APP_SECRET=$APP_SECRET"
    echo "FEISHU_VERIFY_TOKEN="
    [ -n "$OWNER_OPEN_ID" ] && echo "FEISHU_SENDER_OPEN_ID=$OWNER_OPEN_ID  # 扫码人 open_id，发文件兜底收件人"
    echo
    echo "CLAUDE_CWD=$CWD"
    [ -n "$PERSONA" ] && echo "BOT_PERSONA_FILE=$PERSONA"
    echo "BOT_NAME=$BOT_NAME"
} > "$ENV_FILE"
echo "✅ 已生成 $ENV_FILE"

# 2) 生成 launchd 自启 plist（开机自启、退出自拉起）
LABEL="com.claude.bot.$ROLE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
NODE_DIR="$( [ -n "$CLAUDE_BIN" ] && dirname "$CLAUDE_BIN" || echo /usr/local/bin )"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/start-bot.sh</string>
        <string>$ROLE</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$NODE_DIR:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
PLISTEOF
echo "✅ 已生成自启配置 $PLIST"

# 3) 启动
if [ "${START:-1}" = "1" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    sleep 3
    echo "✅ 已启动（$LABEL）。日志：$REPO/bot-$ROLE.log"
else
    echo "ℹ️  未启动（START=0）。手动启动：launchctl bootstrap gui/\$(id -u) \"$PLIST\""
fi
echo "🎉 完成。去飞书搜到「$BOT_NAME」这个应用即可私聊 / 拉群。"
echo "ℹ️  要让它能在群里 @ 队友协作，再在 .env.$ROLE 里加 BOT_RELAY=1 / BOT_MAX_HOPS / BOT_TEAMMATES（见 .env.role.example）。"
