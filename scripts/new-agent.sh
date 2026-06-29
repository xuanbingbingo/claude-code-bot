#!/bin/bash
# 一键创建一个「角色 bot」：生成 .env.<role> + launchd 自启配置 + 启动。
# 把同一份 claude-feishu.py 以「不同凭证 + 不同人设」跑成一个新的独立飞书 bot。
#
# ⚠️ 唯一不能自动的一步：先在飞书开放平台为这个角色建一个【企业自建应用】
#    （机器人能力 + 4 个 im 权限 + 长连接事件 im.message.receive_v1 + 发布），拿到 App ID/Secret。
#    飞书不开放用 API 建应用，这步必须你在网页完成。
#
# 用法：
#   bash scripts/new-agent.sh <role> <App_ID> <App_Secret> [人设md路径] [显示名]
# 例：
#   bash scripts/new-agent.sh researcher cli_xxx secret_yyy ~/.claude/agents/quant-research.md 研究分析师
#
# 可选环境变量：
#   CLAUDE_CWD=/path   该 bot 的 Claude 工作目录（默认 $HOME）
#   START=0            只生成配置、不启动（默认启动）

set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"        # 仓库根目录
ROLE="$1"; APP_ID="$2"; APP_SECRET="$3"; PERSONA="$4"; BOT_NAME="${5:-$1}"
CWD="${CLAUDE_CWD:-$HOME}"

if [ -z "$ROLE" ] || [ -z "$APP_ID" ] || [ -z "$APP_SECRET" ]; then
    echo "用法: bash scripts/new-agent.sh <role> <App_ID> <App_Secret> [人设md路径] [显示名]"
    echo "  先在飞书开放平台建好该角色的自建应用并发布，拿到 App ID/Secret 再跑本脚本。"
    exit 1
fi

# 1) 生成 .env.<role>（含密钥，已被 .gitignore 忽略，不入库）
ENV_FILE="$REPO/.env.$ROLE"
[ -e "$ENV_FILE" ] && { cp "$ENV_FILE" "$ENV_FILE.bak"; echo "⚠️  $ENV_FILE 已存在，备份为 .bak"; }
{
    echo "# 角色 bot：$BOT_NAME（new-agent.sh 生成）"
    echo "FEISHU_APP_ID=$APP_ID"
    echo "FEISHU_APP_SECRET=$APP_SECRET"
    echo "FEISHU_VERIFY_TOKEN="
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
