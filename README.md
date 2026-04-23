# Claude Code Gateway

支持多渠道（Telegram + 飞书）远程操控本地 Claude Code，支持文字、语音、图片输入，终端实时显示执行过程，手机同步收到回复。

## 功能

- **文字消息** → 直接发给 Claude Code 执行
- **语音消息** → Whisper 本地转写后执行
- **图片消息** → 支持附加文字说明，多模态处理
- **会话管理** → 列出历史会话、切换/恢复指定会话、开启新会话

## 命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看帮助 |
| `/sessions` | 列出最近 10 条历史会话 |
| `/resume <编号\|sessionId\|标题>` | 切换到指定会话继续对话（飞书） |
| `/rename <新名称>` | 重命名当前会话（飞书） |
| `/rename <sessionId> <新名称>` | 重命名指定会话（飞书） |
| `/new` | 下一条消息开启全新会话 |

## 快速开始

### 1. 安装依赖

```bash
pip install python-telegram-bot faster-whisper httpx lark-oapi
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 Token
```

`.env` 文件内容：

```env
# Telegram 渠道
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# 飞书渠道
FEISHU_APP_ID=your_feishu_app_id
FEISHU_APP_SECRET=your_feishu_app_secret
FEISHU_VERIFY_TOKEN=your_feishu_verify_token

CLAUDE_CWD=/path/to/your/projects
```

### 3. 启动

**Telegram：**
```bash
bash start-claude-telegram.sh
```

**飞书（长连接模式）：**
```bash
bash start-claude-feishu.sh
```

## 飞书配置指南

飞书机器人使用 **长连接（WebSocket）** 模式接收消息，本地直接连接飞书服务器，无需公网地址、nginx 或 SSH 隧道。

### 飞书开放平台配置

1. 在 [飞书开放平台](https://open.feishu.cn/app/) 创建「企业自建应用」
2. 开启「机器人」能力
3. **事件订阅 → 订阅方式** → 选择 **「使用长连接接收事件」（推荐）**
4. 订阅事件：`im.message.receive_v1`
5. 发布版本，将应用添加到你的飞书群组或私聊

### 本地启动

直接运行即可，无需任何服务器配置：

```bash
bash start-claude-feishu.sh
```

首次启动会自动建立 WebSocket 连接，连接成功后飞书平台状态显示为「已连接」。

## 目录结构

```
claude-gateway/
├── claude_core.py              # 核心逻辑（会话管理、调用 claude CLI、流式解析）
├── claude-telegram.py          # Telegram bot
├── claude-feishu.py            # 飞书 bot（长连接模式）
├── start-claude-telegram.sh    # Telegram 启动脚本
├── start-claude-feishu.sh      # 飞书启动脚本
├── .env                        # 环境变量
├── .env.example                # 环境变量模板
└── README.md
```

## 依赖

- [Claude Code CLI](https://claude.ai/code) — 已安装并可在终端使用 `claude` 命令
- [python-telegram-bot](https://python-telegram-bot.org/) >= 20
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音转写（首次运行自动下载模型）
- [httpx](https://www.python-httpx.org/) — 异步 HTTP 客户端
- [lark-oapi](https://github.com/larksuite/lark-samples) — 飞书开放平台 SDK（长连接模式）

## 注意事项

- 语音识别使用 `faster-whisper base` 模型，首次启动会自动下载（约 150MB）
- Telegram 使用 socks5 代理（代码中硬编码 `socks5://127.0.0.1:53542`），如需修改在 `claude-telegram.py` 的 `main()` 函数中调整
- 飞书凭证严禁提交到代码仓库，统一通过 `.env` 管理
- 两个渠道独立管理会话状态，互不影响
- 飞书长连接模式下，**无需服务器、无需 nginx、无需 SSH 隧道**，本地直接连接飞书
