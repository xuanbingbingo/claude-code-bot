#!/bin/zsh
# 重启全部 claude-gateway bot。
# 2026-10-28 起转发到 `gw restart --all`：角色清单由 gw 从 .env.* + LaunchAgents 自动发现，
# 不再在这里写死 label（旧版只写了 9 个，已与实际 26 个漂移）。
# 透传参数：bash _restart_all.sh --dry-run / --json
DIR="${0:A:h}"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY=python3
[[ " $* " == *" --dry-run "* ]] || sleep 5   # 保留旧行为：给调用方（常是某个 bot 自己）留时间先回消息
echo "=== 开始重启 $(date '+%F %T') ===" >&2
"$PY" "$DIR/cli/gw" restart --all --wait 12 "$@"
rc=$?
echo "=== 完成 $(date '+%F %T') rc=$rc ===" >&2
exit $rc
