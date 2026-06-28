#!/bin/bash
# macOS 保持唤醒服务：防止 Mac 空闲 / 磁盘 / 系统睡眠，
# 让网关 7×24 在线、长回复不被睡眠从中途打断。
# 安装一个 launchd 常驻服务跑 caffeinate（开机自启、退出自动拉起）。幂等，可重复执行。
#
# ⚠️ 重要限制（务必知道）：
#   - caffeinate 只能阻止「空闲睡眠」；其中 -s（阻止系统睡眠）仅在【插电】时生效。
#   - caffeinate 无法阻止【合盖睡眠 / clamshell】。
#   → 所以要真正 7×24 稳定：**插上电源 + 保持开盖**。
#     合盖或纯电池仍会进入维护睡眠，导致手机端「能收到但回复到一半就断」。
#   - 若必须合盖运行，需手动并自担过热风险：  sudo pmset disablesleep 1
#
# 用法：bash scripts/macos-keep-awake.sh        # 安装/重装并启动
#       bash scripts/macos-keep-awake.sh stop   # 卸载

set -e
LABEL="com.claude.caffeinate"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$1" = "stop" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "🛑 防睡眠服务已卸载（$LABEL）"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-i</string>
        <string>-m</string>
        <string>-s</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ 防睡眠服务已安装并启动（$LABEL）"
echo "ℹ️  记住：要真正 7×24，请【插电 + 开盖】；合盖/纯电池仍会睡。"
