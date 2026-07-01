---
name: new-feishu-bot
description: 在飞书里新增一个 Claude 角色机器人（claude-code-bot / claude-gateway 底座，扫码自动建应用）。当用户说「在飞书加个机器人 / 建个飞书 bot / 把某个 agent 做成飞书机器人 / 加个飞书角色 bot / 飞书上加个 XX 助手」时激活。流程：确认角色三要素 → 后台跑 new-agent.sh 扫码建应用（二维码存 PNG）→ 打开给用户手机飞书扫 → 盯日志确认长连接上线 → 汇报。
---

# 在飞书新增一个 Claude 角色机器人 · 一键剧本

把「往 claude-code-bot 加一个独立飞书角色 bot」的全流程固化下来。底座是本仓库的
`scripts/new-agent.sh` + `scripts/feishu_setup.py`（飞书设备码扫码建应用），**不用手动进飞书开放平台配任何权限**。

> 关键机制：`scripts/feishu_setup.py` 调飞书账号中心的设备码注册端点，扫码授权后自动创建自建应用、
> 预配机器人能力/权限/长连接事件订阅，直接返回 App ID/Secret。`new-agent.sh` 拿到凭证后生成
> `.env.<role>` + launchd 自启 + 启动 bot。

## 第 0 步：前置检查（缺则先补）

1. **定位仓库根目录**：本 skill 的命令都在 claude-code-bot 仓库根目录下跑。
   - 先确认当前目录或用户指定目录下有 `scripts/new-agent.sh`；没有就问用户仓库克隆在哪，`cd` 过去。
   - 下文用 `$REPO` 指代该目录。
2. **依赖就绪**：确认跑过 `./install.sh`（装了 httpx/qrcode/pillow 到 venv）且 `claude` CLI 可用。
   没跑过就先 `./install.sh`。
3. **飞书 App 登录态**：提醒用户手机 / 桌面飞书处于登录状态，稍后要扫码。

## 第 1 步：确定角色三要素（缺则问用户）

1. **role（英文小写 slug）**：如 `researcher`，用于 `.env.<role>`、launchd label、日志名。
2. **显示名（中文）**：飞书里搜到的名字，如「研究分析师」。
3. **人设文件（可选）**：一个 Claude 子代理格式的 `.md`（frontmatter + 正文＝系统提示），
   如 `~/.claude/agents/xxx.md`。没有就留空（通用无人设），或帮用户临时写一份。

## 第 2 步：后台跑扫码建应用（二维码存 PNG）

**后台**执行（会阻塞等扫码，最长 ~600s），并让二维码另存 PNG（终端 ASCII 在代跑时用户看不到）：

```bash
cd "$REPO"
rm -f "/tmp/qr-<role>.png"
QR_IMAGE="/tmp/qr-<role>.png" bash scripts/new-agent.sh <role> [人设md路径] [显示名]
```

用 `run_in_background: true` 跑这条命令。

## 第 3 步：把二维码打开给用户扫

`begin` 成功后几秒内 PNG 就生成了。轮询等 PNG 出现，然后打开它并提示用户扫码：

```bash
# 等 PNG 生成（最多等 ~20s）
for i in $(seq 1 20); do [ -f "/tmp/qr-<role>.png" ] && break; sleep 1; done
open "/tmp/qr-<role>.png"        # macOS；Linux 用 xdg-open
```

告诉用户：**用手机飞书扫这张二维码 → 授权即自动建应用**。然后等第 2 步那个后台任务跑完。

## 第 4 步：验收上线

后台任务成功结束后（脚本打印「✅ 已启动」），核对：

```bash
cd "$REPO"
grep -aE '^FEISHU_APP_ID' ".env.<role>"                        # 凭证已写入
launchctl list 2>/dev/null | grep "com.claude.bot.<role>"       # launchd 已加载（macOS）
tail -15 "bot-<role>.log"                                        # 应见「飞书长连接启动」+ connected to wss://msg-frontier.feishu.cn
```

看到长连接 `connected to ... msg-frontier.feishu.cn` = 上线成功。

## 第 5 步：收尾 + 汇报

```bash
rm -f "/tmp/qr-<role>.png"
```

- 提示用户：在飞书搜「<显示名>」即可私聊 / 拉群；搜不到就去飞书工作台确认该应用对自己可见。
- 想让它在群里 @ 队友接力：在 `.env.<role>` 加 `BOT_RELAY=1` / `BOT_TEAMMATES`（见 `.env.role.example`）。
- 汇报：完成内容 / 新增文件（`.env.<role>`、launchd plist）/ 需关注 / 下一步。

## 坑位速查

- 扫码页会显示飞书 OpenClaw 生态的注册模板文案，属正常，不影响拿到凭证。
- 该注册端点非飞书公开承诺稳定的 API，若某天失效：退回手动路径
  `bash scripts/new-agent.sh <role> <App_ID> <App_Secret>`（先在飞书开放平台手动建应用拿凭证）。
- 二维码有效期约 1 小时；超时或用户拒绝授权脚本会报错退出，重跑即可。
- 企业微信无对应的扫码/API 建应用能力，本 skill 只管飞书。
