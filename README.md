# Claude Code Gateway

用飞书远程操控本地 Claude Code —— 手机上发消息、发图、发语音，本地 Claude 执行，回复实时流式推回到手机。

> **渠道说明**：目前仅维护 **飞书渠道**（功能完整）。Telegram 渠道代码保留但暂停维护，不建议新用户配置。

## 功能

- **文字消息** → 直接发给 Claude Code 执行
- **语音消息** → 本地 Whisper 转写后执行
- **图片消息** → 多模态，支持附加文字说明
- **会话管理** → 列表 / 切换 / 重命名 / 新建，按当前工作目录过滤
- **流式回复** → Claude 一边生成，一边更新手机上的消息（Telegram 用 edit_text，飞书用 interactive 卡片 PATCH）
- **工作目录切换**（飞书） → `/cwd` 交互式导航，每个用户独立 cwd，切换后自动接续该目录最近会话
- **运行控制**（飞书） → `/stop` 中断任务，`/model` 切模型，`/mode` 切权限模式（bypass / plan / default / accept）
- **Subagent 透传**（飞书） → `/agents` 列出 Claude Code 自定义 subagent，`/agent <name> <任务>` 一键派发
- **Skills 透传**（飞书） → 未注册的 `/xxx` 直接作为 prompt 传给 `claude` CLI，可直接用官方 skill

两个渠道完全独立，可以只装一个、也可以两个都装。

---

## 前置要求

**所有渠道共同**：

1. **Claude Code CLI** 已安装并能在终端运行 `claude` 命令 → [安装指南](https://docs.claude.com/en/docs/claude-code)
2. **Python 3.10+**（`python3 --version` 确认）
3. **本仓库代码**
   ```bash
   git clone https://github.com/xuanbingbingo/claude-code-bot.git
   cd claude-code-bot
   ```

---

## 渠道一：飞书（Lark）

飞书渠道用 **长连接（WebSocket）** 模式，本地直接连飞书服务器，**不需要公网地址、nginx、SSH 隧道、ngrok**。

### 1. 在飞书开放平台创建自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app/)，用你的飞书账号登录
2. 点击 **「创建企业自建应用」**，填名称、描述、图标
3. 创建完成后进入应用后台

### 2. 开启「机器人」能力

1. 左侧菜单 **「添加应用能力」**
2. 找到 **「机器人」**，点击 **「添加」**

### 3. 开启权限（必须 4 项）

左侧菜单 **「权限管理」** → 依次开通：

| 权限标识 | 中文名 | 用途 |
|---------|--------|------|
| `im:message` | 获取与发送单聊、群组消息 | 通用收发 |
| `im:message:send_as_bot` | 以应用身份发消息 | 发卡片 / 文本 |
| `im:message:update` | 更新消息 | 流式 PATCH 卡片 |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 接收用户消息 |

**`im:message:update` 不开，卡片无法流式刷新**，实测会一直停在 "⏳ 处理中..."。

### 4. 订阅事件（长连接模式）

左侧菜单 **「事件与回调」** → **「事件配置」**：

1. **订阅方式** → 选择 **「使用长连接接收事件」**（**重点**：不要选 HTTP 回调）
2. **添加事件** → 搜索并添加：`im.message.receive_v1`（接收消息 v2.0）

### 5. 发布版本

左侧菜单 **「版本管理与发布」**：

1. 点击 **「创建版本」**，填版本号和说明
2. 提交审核（企业自建应用一般由管理员秒过）
3. 审核通过后状态变为「已启用」

**权限和事件变更必须发布新版本才会生效**，已开通 ≠ 已生效。

### 6. 获取 App ID / App Secret

左侧菜单 **「凭证与基础信息」**：

- 复制 **App ID**（`cli_xxxxxxxxxx`）
- 复制 **App Secret**

### 7. 把机器人拉进对话

- 私聊：在飞书里搜索你应用的名字 → 点击进入 → 直接开聊
- 群聊：群设置 → 群机器人 → 添加机器人 → 选择你的应用

### 8. 安装飞书渠道依赖

```bash
pip install httpx lark-oapi faster-whisper
```

### 9. 配置 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，**飞书渠道填这三个**：

```env
FEISHU_APP_ID=cli_xxxxxxxxxx             # 第 6 步 App ID
FEISHU_APP_SECRET=xxxxxxxxxxxxx          # 第 6 步 App Secret
FEISHU_VERIFY_TOKEN=                     # 长连接模式留空即可

CLAUDE_CWD=/Users/you/projects           # Claude 执行时的工作目录
```

`CLAUDE_CWD` 是 Claude 启动后 `cwd`，Claude 读写文件都以这个目录为根。

> 长连接模式下 `FEISHU_VERIFY_TOKEN` 不校验，可以留空。

### 10. 启动

```bash
bash start-claude-feishu.sh
```

看到：

```
🤖 Claude Code Feishu Gateway 启动中（长连接模式）...
   App ID: cli_xxxxx...
   等待飞书消息...
[Lark] [xxxx] [INFO] connected to wss://msg-frontier.feishu.cn/...
```

就连上了。日志路径 `./claude-feishu.log`。

### 11. 测试

在飞书里给你的 bot 发：

- 一段文字 → 卡片从 "⏳ 处理中..." 实时刷新到 Claude 回复
- 一张图片（可带文字说明）→ 多模态
- 一段语音 → Whisper 转写后执行（首次下载模型）

### 12. 命令

#### 会话

| 命令 | 说明 |
|------|------|
| `/start` | 显示帮助和当前状态 |
| `/status` | 查看当前 cwd / 模型 / 模式 / sessionId / 任务状态 |
| `/sessions` | 列出当前工作目录下最近 10 条历史会话 |
| `/resume <编号>` | 按 `/sessions` 里的编号切换 |
| `/resume <sessionId>` | 按完整 sessionId 切换 |
| `/resume <标题>` | 按 `/sessions` 显示的标题切换（精确匹配） |
| `/rename <新名称>` | 重命名当前会话 |
| `/rename <sessionId> <新名称>` | 重命名指定会话 |
| `/new` | 下一条消息开启全新会话 |

启动时会自动接续当前工作目录下最近一条可用会话；如果该会话历史数据不兼容（API 400），第一次消息会自动回退新会话模式，你重发即可。

#### 运行控制

| 命令 | 说明 |
|------|------|
| `/stop` | 中断当前正在运行的 Claude 任务（新消息也会自动中断旧任务） |
| `/model` | 查看当前模型 |
| `/model opus \| sonnet \| haiku` | 切换模型（下一条消息生效） |
| `/model default` | 重置为 Claude CLI 默认 |
| `/mode` | 查看当前权限模式 |
| `/mode bypass` | 跳过所有确认（默认，对应 `--dangerously-skip-permissions`） |
| `/mode plan` | 只规划不执行（Plan 模式） |
| `/mode default` | 每次工具调用需确认 |
| `/mode accept` | 自动接受文件编辑 |

#### 工作目录（每个用户独立）

| 命令 | 说明 |
|------|------|
| `/cwd` | 查看当前目录 + 列出子目录（带编号） |
| `/cwd <编号>` | 进入上一步列出的子目录 |
| `/cwd ..` | 返回上级 |
| `/cwd <相对/绝对/~路径>` | 跳到任意目录 |

切换目录时会**自动接续该目录下最近的可用会话**；如果该目录还没有历史会话，则下一条消息开启新会话。

#### Subagent

| 命令 | 说明 |
|------|------|
| `/agents` | 列出可用 subagent（`~/.claude/agents` + 项目 `.claude/agents`） |
| `/agents <关键词>` | 按名称或描述模糊过滤 |
| `/agent <name>` | 查看指定 agent 的详情 |
| `/agent <name> <任务描述>` | 让 Claude 调用该 subagent 完成任务 |

#### Skills 透传

未注册的 `/xxx` 命令会直接作为 prompt 传给 `claude` CLI，可以直接使用 Claude Code 官方 skill（如 `/commit`、`/review`、`/init` 等）。

---

## 渠道二：Telegram（暂停维护）

> ⚠️ Telegram 渠道目前暂停维护，代码保留供参考，不建议新用户配置。

### 1. 创建 Telegram Bot

1. 手机 / 桌面 Telegram 搜索 **@BotFather**，开始对话
2. 发送 `/newbot`
3. 按提示输入 bot 名字和 username（username 必须以 `bot` 结尾，例如 `my_claude_bot`）
4. BotFather 回复一串 Token，形如：
   ```
   123456789:AAE...xyz
   ```
   **保存好，下一步要用**
5. 在 BotFather 给你的链接里点击 **START**，或直接搜索你的 bot 并发送 `/start`，否则 bot 无法主动给你发消息

### 2. 安装 Telegram 渠道依赖

```bash
pip install python-telegram-bot faster-whisper
```

如果需要代理（下见第 4 步）：

```bash
pip install "python-telegram-bot[socks]"
```

### 3. 配置 `.env`

如果还没创建 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`，**Telegram 渠道只需要填这两个**：

```env
TELEGRAM_BOT_TOKEN=123456789:AAE...xyz   # 第 1 步 BotFather 给的 Token
CLAUDE_CWD=/Users/you/projects            # Claude 执行时的工作目录（绝对路径）
```

### 4. 代理配置（国内用户必看）

Telegram 在国内需要代理。代码里写死了 `socks5://127.0.0.1:53542`（位于 `claude-telegram.py` 的 `main()` 函数）。

- 如果你用 Clash / Shadowrocket 等，把 SOCKS5 端口改成它们的监听端口（Clash 默认 `7890` 是 HTTP，SOCKS5 通常另配一个端口）
- 不需要代理的用户：把 `.proxy(proxy)` 这一行删掉或注释

编辑方式：

```bash
vim claude-telegram.py   # 搜索 socks5://127.0.0.1:53542，改成你的
```

### 5. 启动

```bash
bash start-claude-telegram.sh
```

看到 `🤖 Claude Code Telegram Gateway 启动中...` 就成功了。

日志路径：`./claude-telegram.log`。

### 6. 测试

在 Telegram 给你的 bot 发一条消息，比如：

```
你好
```

bot 会先回复 `⏳ 处理中...`，然后流式改写成 Claude 的回复。

再试：

- 发条语音 → 首次会下载 Whisper base 模型（约 150MB）
- 发张图片（带文字说明）→ 多模态分析

### 7. 命令

在 Telegram 里直接输入：

| 命令 | 说明 |
|------|------|
| `/start` | 显示帮助 |
| `/sessions` | 列出最近 10 条历史会话 |
| `/resume <编号>` | 切换到指定会话继续对话 |
| `/new` | 下一条消息开启全新会话 |

---

## 命令对照表

| 命令 | 飞书 | Telegram |
|------|:--:|:--:|
| `/start` | ✅ | ✅ |
| `/status` | ✅ | ❌ |
| `/sessions` | ✅ | ✅ |
| `/resume <编号>` | ✅ | ✅ |
| `/resume <sessionId>` | ✅ | ❌ |
| `/resume <标题>` | ✅ | ❌ |
| `/rename ...` | ✅ | ❌ |
| `/new` | ✅ | ✅ |
| `/stop` | ✅ | ❌ |
| `/model ...` | ✅ | ❌ |
| `/mode ...` | ✅ | ❌ |
| `/cwd ...` | ✅ | ❌ |
| `/agents` / `/agent` | ✅ | ❌ |
| 未注册 `/xxx` 透传给 Claude | ✅ | ❌ |

> Telegram 渠道的命令集较精简，后续如有需要可迁移飞书的增强命令。

---

## 目录结构

```
claude-code-bot/
├── claude_core.py              # 共享核心：会话管理、调 claude CLI、流式解析、Whisper
├── claude-telegram.py          # Telegram 渠道
├── claude-feishu.py            # 飞书渠道（长连接）
├── start-claude-telegram.sh    # Telegram 启动脚本
├── start-claude-feishu.sh      # 飞书启动脚本
├── .env                        # 你的凭证（已在 .gitignore，不会提交）
├── .env.example                # 凭证模板
├── claude-telegram.log         # Telegram 运行日志（已在 .gitignore）
├── claude-feishu.log           # 飞书运行日志（已在 .gitignore）
└── README.md
```

## 依赖清单

| 包 | 用途 | 必需渠道 |
|----|------|---------|
| `claude` CLI | 实际执行引擎 | 两个都需要 |
| `python-telegram-bot >=20` | Telegram SDK | Telegram |
| `lark-oapi` | 飞书开放平台 SDK | 飞书 |
| `httpx` | 飞书 REST API 调用 | 飞书 |
| `faster-whisper` | 本地语音转写 | 语音消息 |

---

## 常见问题

**Q：飞书发消息后机器人不回复，日志只有 `[DEBUG] 收到消息`**
权限或事件订阅没发布版本生效。回到「版本管理与发布」重新发布；特别检查 `im:message:update` 是否已开通且已随版本发布。

**Q：飞书卡片一直停在 "⏳ 处理中..." 不刷新**
几乎 100% 是 `im:message:update` 权限没开或没发布版本。重启 gateway 前先在飞书后台确认该权限状态为「已开通」且版本已发布。

**Q：`/resume` 报 "未找到 session"**
- 确认输入的是 `/sessions` 里显示的 `🔖 sessionId` 或编号；输入标题时必须完全一致
- 如果 session 文件已被清理（`~/.claude/projects/<project>/<sessionId>.jsonl` 不存在）则无法恢复

**Q：飞书恢复会话后报 `API Error: 400 ... tool_use.id`**
历史会话 jsonl 里存了不合法的 tool_use id（多见于跨 provider / model 的旧会话）。gateway 已经做了兜底：这条错误会推给你并自动切到新会话模式，**重发一次消息**即可。

**Q：Telegram 连不上 / 一直超时**
代理没配对。检查 `claude-telegram.py` 里 `socks5://127.0.0.1:53542`，改成你本机代理的 SOCKS5 端口；或完全去掉 `.proxy(proxy)` 行（如果不需要代理）。

**Q：首次发语音卡很久**
Whisper `base` 模型首次使用会下载 ~150MB，之后常驻内存。下载完再发一条语音就秒响。

**Q：两个渠道的会话列表会相互干扰吗？**
共享 `CLAUDE_CWD`，所以 `/sessions` 看到的是同一份历史；但两个渠道各自维护 "当前会话"（飞书按 open_id 分，Telegram 是全局单会话）。一边 `/new` 不会影响另一边。

---

## 注意事项

- **凭证保护**：`.env` 已在 `.gitignore`，任何情况下不要把 Token / Secret 提交到仓库
- **后台运行**：`bash start-claude-xxx.sh` 是前台脚本；想常驻建议 `nohup bash start-claude-xxx.sh > /dev/null 2>&1 &`
- **停止服务**：`pkill -f claude-telegram` 或 `pkill -f claude-feishu`
- **自定义 Whisper 模型**：编辑 `claude_core.py` 的 `WhisperModel("base", ...)`，可改成 `small` / `medium`（更大更准，但更慢）
- **飞书长连接不需要公网**：本地启动就能工作，适合开发者本机部署
