# 给 claude-gateway 加「飞书扫码自动建应用」

## Context

`claude-gateway`（开源仓库 `claude-code-bot`）现在建一个新的飞书角色 bot，唯一不能自动的一步是**手动进飞书开放平台建自建应用、配权限、发布、抄 App ID/Secret**（见 `scripts/new-agent.sh` 头部注释第 5-7 行）。对标的 cc-connect 用「手机扫码一键建应用」把这步干掉了。

调研已确认（源码级）：cc-connect 的扫码建应用不是自研，而是调飞书账号中心的**设备码注册端点** `accounts.feishu.cn/oauth/v1/app/registration`（飞书给 OpenClaw/lark-cli 生态开的 `PersonalAgent` 注册模板），三段式 `init → begin → poll`，扫码授权后直接返回 `client_id`(App ID)/`client_secret`(App Secret)，并**预配好权限与事件订阅**。这套端点不在 lark-oapi SDK 里，用 `httpx` 手写几个 form-urlencoded 请求即可复刻。目标：让 `bash scripts/new-agent.sh <role>` 不传凭证时自动扫码建应用，一条命令建出角色 bot。

企微无对应能力（判 C），本次不做。

## 方案（用户已选：增强 new-agent.sh）

### 1. 新建 `scripts/feishu_setup.py` — 设备码流核心（纯 httpx，不依赖 lark-oapi）

照 cc-connect `cmd/cc-connect/feishu.go` 的 `runRegistrationFlow`（534-674 行）1:1 翻成 Python：

- 端点：`POST {base}/oauth/v1/app/registration`，`Content-Type: application/x-www-form-urlencoded`
  - base 默认 `https://accounts.feishu.cn`；poll 中若 `user_info.tenant_brand == "lark"` 切到 `https://accounts.larksuite.com` 继续轮询
- **init**：`action=init` → 校验 `supported_auth_methods` 含 `client_secret`
- **begin**：`action=begin&archetype=PersonalAgent&auth_method=client_secret&request_user_info=open_id` → 拿 `device_code`、`verification_uri_complete`、`interval`(默认5)、`expire_in`(默认600)
- 出二维码：用 `qrcode` 把 `verification_uri_complete` 渲染成**终端 ASCII 码**（`qr.print_ascii()`），同时打印原始 URL 兜底
- **poll**：`action=poll&device_code=...`，按 `interval` 轮询；错误分支照抄——`authorization_pending`/空=继续，`slow_down`→interval+=5，`access_denied`→拒绝退出，`expired_token`→过期退出，超 `min(expire_in, timeout)` → 超时退出
- 成功（`client_id`且`client_secret`非空）：**把凭证打到 stdout 一行** `<app_id> <app_secret> <open_id>`，供 shell 捕获；所有提示/二维码/进度一律打到 **stderr**（不污染 stdout）
- 失败：非0退出码 + stderr 错误信息
- 参数：`--role <name>`（仅提示显示）、`--platform feishu|lark`（默认 feishu）、`--timeout <秒>`（默认 600）

### 2. 改 `scripts/new-agent.sh` — 加「无凭证则扫码」分支（向后兼容）

现有位置参数 `ROLE APP_ID APP_SECRET PERSONA BOT_NAME` 保持不变。在参数解析处（第 20 行附近）加判断：

- 若 `$2` 以 `cli_` 开头 → **原手动路径**，一字不改（`APP_ID=$2 APP_SECRET=$3 PERSONA=$4 BOT_NAME=${5:-$1}`）
- 否则 → **扫码路径**：把 `$2` 当 persona、`$3` 当显示名，调用
  `read APP_ID APP_SECRET OWNER_OPEN_ID < <(python3 "$REPO/scripts/feishu_setup.py" --role "$ROLE")`
  拿到凭证后继续走原有的「写 .env.<role> + 生成 plist + 启动」三步（29-83 行完全复用，不动）
- 拿到的 `OWNER_OPEN_ID`（扫码人本人 open_id）顺手写进 `.env.<role>` 的 `FEISHU_SENDER_OPEN_ID=`（给发文件工具兜底收件人用，可选）

用法变化：
- `new-agent.sh researcher cli_xxx sec_yyy persona.md 名` → 手动（原样）
- `new-agent.sh researcher` → 扫码建应用，无人设
- `new-agent.sh researcher ~/.claude/agents/x.md 研究员` → 扫码 + 人设 + 名

同步更新脚本头部注释（第 5-7 行「唯一不能自动」那段）：改成「不传凭证则自动扫码建应用」。

### 3. `requirements.txt` 加 `qrcode>=7.4`

`qrcode` 的 `print_ascii()` 不强依赖 Pillow，体积小，进核心依赖。`install.sh` 无需改（已 `pip install -r requirements.txt`）。

### 4. 文档

- `README.md` 多角色章节：加「扫码建应用」用法块，说明 `new-agent.sh <role>` 一条命令扫码即建
- `.env.role.example` 顶部注释：补一句「可用 new-agent.sh <role> 扫码自动建应用，免手动进开放平台」

## 关键文件

| 文件 | 改动 |
|------|------|
| `scripts/feishu_setup.py` | **新建**，设备码流核心 |
| `scripts/new-agent.sh` | 加无凭证扫码分支（29-83 行复用不动） |
| `requirements.txt` | +`qrcode>=7.4` |
| `README.md` / `.env.role.example` | 文档说明 |

## 验证

1. **端点连通（不需扫码，先跑）**：`python3 scripts/feishu_setup.py --role test` 跑到 init+begin，确认
   - init 返回 `supported_auth_methods` 含 `client_secret`（验证 `PersonalAgent` 模板对本账号开放）
   - begin 返回 `device_code` + 终端出二维码 + 打印 URL
   若 init/begin 就报错 → 说明该端点/模板对当前飞书账号环境不可用，需回报并止步（这是最大不确定点）。
2. **完整闭环（真扫一次）**：`bash scripts/new-agent.sh scan-test` → 手机飞书扫码授权 → 确认返回 app_id/secret、生成 `.env.scan-test`、launchd 起来、`bot-scan-test.log` 无凭证错误、能在飞书搜到应用并私聊收到回复（验证扫码建的应用确实预配了机器人能力 + im 事件订阅）。
3. 回归：`new-agent.sh x cli_aaa sec_bbb` 手动路径行为不变。
4. 验证通过后：commit + push 到 `claude-code-bot`（开源仓库同步）。

## 备注

- 该注册端点非飞书 server-docs 公开承诺稳定的 API，属 OpenClaw 生态，飞书可能改；扫码页会带 OpenClaw 品牌文案（不影响拿凭证）。README 里注明。
- 实施前把本 plan 复制到 `aiProjects/claude-gateway/plans/`（遵守项目 plan 落盘规范）。
