# Claude Code Telegram Gateway

通过 Telegram 远程操控本地 Claude Code，支持文字、语音、图片输入，终端实时显示执行过程，手机同步收到回复。

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
| `/resume <编号>` | 切换到指定会话继续对话 |
| `/new` | 下一条消息开启全新会话 |

## 快速开始

### 1. 安装依赖

```bash
pip install python-telegram-bot faster-whisper
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 Token 和 API Key
```

`.env` 文件内容：

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key
ANTHROPIC_BASE_URL=https://api.anthropic.com   # 可选，默认官方地址
CLAUDE_CWD=/path/to/your/projects              # Claude 执行时的工作目录
```

### 3. 启动

```bash
bash start-claude-telegram.sh
```

## 依赖

- [Claude Code CLI](https://claude.ai/code) — 已安装并可在终端使用 `claude` 命令
- [python-telegram-bot](https://python-telegram-bot.org/) >= 20
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音转写（首次运行自动下载模型）

## 注意事项

- 语音识别使用 `faster-whisper base` 模型，首次启动会自动下载（约 150MB）
- 如需使用代理，在 `claude-telegram.py` 的 `main()` 函数中配置 `proxy` 参数
- Bot Token 和 API Key 严禁提交到代码仓库，统一通过 `.env` 管理
