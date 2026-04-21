#!/usr/bin/env python3
"""
Claude Code Telegram Gateway
手机发消息/语音/图片 → 本地 Claude Code 执行 → 终端实时显示 + 手机收到回复
"""

import asyncio
import base64
import glob
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from telegram import Update, Message
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# 自动加载脚本同目录的 .env 文件
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    print("❌ 缺少 TELEGRAM_BOT_TOKEN，请在 .env 文件或环境变量中设置")
    sys.exit(1)
MAX_TG_LEN = 4000
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", os.path.expanduser("~/aiProjects"))

def _last_session_id() -> str | None:
    """从 history.jsonl 取 CLAUDE_CWD 目录下最新会话 ID"""
    path = os.path.expanduser("~/.claude/history.jsonl")
    last_sid = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("project") == CLAUDE_CWD:
                        last_sid = d.get("sessionId") or last_sid
                except Exception:
                    pass
    except Exception:
        pass
    return last_sid

_new_session = False
_resume_session_id = _last_session_id()  # 启动时自动续上全局最新会话
_whisper_model = None


# ── 会话列表工具 ──────────────────────────────────────────────


def _load_custom_titles() -> dict[str, str]:
    """扫描所有 jsonl，提取 /rename 写入的 custom-title"""
    titles = {}
    base = os.path.expanduser("~/.claude/projects")
    for f in glob.glob(f"{base}/**/*.jsonl", recursive=True):
        if "/subagents/" in f:
            continue
        try:
            with open(f) as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "custom-title":
                            sid = d.get("sessionId")
                            title = d.get("customTitle", "")
                            if sid and title:
                                titles[sid] = title
                    except Exception:
                        pass
        except Exception:
            pass
    return titles


def list_sessions(limit: int = 10) -> list[dict]:
    """从 history.jsonl 读取最近会话，优先用 /rename 设置的名字"""
    history_path = os.path.expanduser("~/.claude/history.jsonl")
    first: dict[str, dict] = {}
    latest: dict[str, int] = {}
    try:
        with open(history_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    sid = entry.get("sessionId")
                    if not sid:
                        continue
                    ts = entry.get("timestamp", 0)
                    if sid not in first:
                        first[sid] = entry
                    latest[sid] = ts
                except Exception:
                    pass
    except Exception:
        return []

    custom_titles = _load_custom_titles()

    sessions = [
        {
            "id": sid,
            "timestamp": latest[sid],
            "summary": custom_titles.get(sid) or first[sid].get("display", "")[:60],
            "proj": first[sid].get("project", ""),
        }
        for sid in first
    ]
    sessions.sort(key=lambda x: x["timestamp"], reverse=True)
    return sessions[:limit]


# ── Telegram 流式输出 ─────────────────────────────────────────

class TgStreamer:
    """把 Claude Code 的 stream-json 事件实时推到 Telegram。

    通过 edit_message_text 节流地更新同一条消息，超过长度自动另起一条。
    """

    MAX_LEN = 3500        # 单条 Telegram 消息长度上限（留余量）
    THROTTLE = 1.2        # 最小 edit 间隔（秒）

    def __init__(self, message: Message, prefix: str = ""):
        self.msg = message
        self.prefix = prefix
        self.text = ""
        self.status = ""              # 当前工具调用状态行，例如 "🔧 Read(xxx.py)"
        self.last_edit = 0.0
        self.pending: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.has_content = False      # 流程中是否真正输出过内容
        self.partial_mode = False     # 看见过 text_delta 后置 True，避免和 assistant 完整 text 重复
        self._hb_task: asyncio.Task | None = None
        self._started_at = time.monotonic()

    def start_heartbeat(self):
        """在 has_content 变 True 之前，定期把 '⏳ Xs...' 写进 status，让用户知道还在跑。"""
        if self._hb_task is None:
            self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        try:
            while not self.has_content:
                elapsed = int(time.monotonic() - self._started_at)
                # 工具/思考状态优先；只在 status 为空或也是心跳自设时覆盖
                if not self.status or self.status.startswith("⏳"):
                    await self.set_status(f"⏳ 处理中 {elapsed}s...")
                await asyncio.sleep(2)
            # 退出时清掉自己设的心跳状态
            if self.status.startswith("⏳"):
                await self.clear_status()
        except asyncio.CancelledError:
            return

    def _compose(self) -> str:
        parts = []
        if self.prefix:
            parts.append(self.prefix)
        body = self.text
        if self.status:
            body = (body + "\n" + self.status) if body else self.status
        if body:
            parts.append(body)
        if not parts:
            return "⏳ 处理中..."
        text = "\n\n".join(parts)
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 1] + "…"
        return text

    async def _do_edit(self):
        text = self._compose()
        try:
            await self.msg.edit_text(text)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                pass  # 忽略其他编辑错误，继续流
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", 1))
        except Exception:
            pass
        self.last_edit = time.monotonic()

    async def _schedule(self):
        async with self.lock:
            elapsed = time.monotonic() - self.last_edit
            if elapsed >= self.THROTTLE:
                await self._do_edit()
            elif self.pending is None or self.pending.done():
                self.pending = asyncio.create_task(self._delayed(self.THROTTLE - elapsed))

    async def _delayed(self, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self.lock:
            await self._do_edit()

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        budget = self.MAX_LEN - len(self.prefix) - len(self.status) - 32
        if len(self.text) + len(chunk) > budget:
            # 固化当前消息，新起一条
            await self.flush()
            try:
                self.msg = await self.msg.reply_text("⏳ 继续...")
            except Exception:
                pass
            self.text = ""
            self.prefix = ""  # 后续消息不重复 prefix
            self.status = ""
        self.text += chunk
        await self._schedule()

    async def set_status(self, line: str):
        if line == self.status:
            return
        self.status = line
        await self._schedule()

    async def clear_status(self):
        if self.status:
            self.status = ""
            await self._schedule()

    async def flush(self):
        if self.pending and not self.pending.done():
            self.pending.cancel()
        async with self.lock:
            await self._do_edit()

    async def finalize(self, fallback: str = ""):
        """流结束：确保最后一次 edit 落地；没有任何流式内容时用 fallback 兜底。"""
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        if self.pending and not self.pending.done():
            self.pending.cancel()
        self.status = ""
        if not self.has_content:
            # 流里没有 assistant text（可能直接出错或超时），用 fallback 完整发出
            text = (fallback or "").strip()
            if not text:
                try:
                    await self.msg.edit_text("✅ 完成（无文字输出）")
                except Exception:
                    pass
                return
            try:
                await self.msg.delete()
            except Exception:
                pass
            bot = self.msg.get_bot()
            chat_id = self.msg.chat_id
            for i in range(0, len(text), MAX_TG_LEN):
                try:
                    await bot.send_message(chat_id=chat_id, text=text[i:i + MAX_TG_LEN])
                except Exception:
                    pass
            return
        # 有内容：把 status 清掉后做最后一次 edit
        await self._do_edit()


def _format_tool_use(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    brief = ""
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        brief = os.path.basename(p) if p else ""
    elif name == "Bash":
        cmd = str(inp.get("command", ""))
        brief = cmd[:60] + ("…" if len(cmd) > 60 else "")
    elif name == "Grep":
        pat = str(inp.get("pattern", ""))
        brief = pat[:40] + ("…" if len(pat) > 40 else "")
    elif name == "Glob":
        brief = str(inp.get("pattern", ""))[:40]
    elif name == "Task":
        brief = str(inp.get("description", ""))[:40]
    elif name == "WebFetch":
        brief = str(inp.get("url", ""))[:60]
    elif name == "WebSearch":
        brief = str(inp.get("query", ""))[:40]
    else:
        for v in inp.values():
            if isinstance(v, str) and v:
                brief = v[:40]
                break
    return f"🔧 {name}({brief})" if brief else f"🔧 {name}"


async def _dispatch_event(event: dict, streamer: "TgStreamer | None"):
    if streamer is None:
        return
    t = event.get("type")
    if t == "stream_event":
        ev = event.get("event") or {}
        et = ev.get("type")
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            cbt = cb.get("type")
            if cbt == "tool_use":
                await streamer.set_status(_format_tool_use(cb))
            elif cbt == "thinking":
                await streamer.set_status("💭 思考中...")
            # text block 开始不额外设状态，等待 delta
        elif et == "content_block_delta":
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                streamer.partial_mode = True
                txt = delta.get("text") or ""
                if txt:
                    await streamer.append(txt)
        elif et == "content_block_stop":
            # 工具 / 思考 / 文本 block 结束都清状态
            await streamer.clear_status()
    elif t == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text" and not streamer.partial_mode:
                # 未开启 partial 模式时才用完整 text 兜底
                text = block.get("text") or ""
                if text:
                    await streamer.append(text)
            elif bt == "tool_use":
                # 拿到完整 input，刷一次更准确的状态行
                await streamer.set_status(_format_tool_use(block))
    elif t == "user":
        await streamer.clear_status()
    elif t == "system" and event.get("subtype") == "status":
        status = event.get("status", "")
        if status == "requesting" and not streamer.has_content:
            # 不直接设置可读的 "📡 调用模型中..."，避免和心跳打架；
            # 仅作为一个信号，心跳已经在跑了
            pass


# ── Claude 执行 ───────────────────────────────────────────────

def _build_session_flags() -> list[str]:
    if _resume_session_id:
        return ["--resume", _resume_session_id]
    if not _new_session:
        return ["--continue"]
    return []


async def run_claude(prompt: str, streamer: "TgStreamer | None" = None) -> str:
    global _new_session, _resume_session_id

    print(f"\n{'─' * 60}")
    print(f"📱  {prompt}")
    print(f"{'─' * 60}\n")
    sys.stdout.flush()

    session_flags = _build_session_flags()
    cmd = (
        ["claude", "--dangerously-skip-permissions"]
        + session_flags
        + [
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "-p", prompt,
        ]
    )
    _new_session = False
    _resume_session_id = None  # 用一次后清掉，后续靠 --continue

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=CLAUDE_CWD,
    )

    result_text = ""
    raw_tail = ""

    async def _read():
        nonlocal result_text, raw_tail
        async for line in proc.stdout:
            decoded = line.decode("utf-8", errors="replace")
            raw_tail = (raw_tail + decoded)[-4096:]
            print(decoded, end="", flush=True)
            s = decoded.strip()
            if not s:
                continue
            try:
                event = json.loads(s)
            except json.JSONDecodeError:
                continue
            await _dispatch_event(event, streamer)
            if event.get("type") == "result":
                result_text = event.get("result", "") or result_text

    try:
        await asyncio.wait_for(_read(), timeout=300)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print("\n⏰ 执行超时")
        if streamer:
            await streamer.clear_status()
        return "❌ 执行超时（超过5分钟）"

    print(f"\n{'─' * 60}")

    if "Request too large" in raw_tail or "max 32MB" in raw_tail:
        _new_session = True  # 强制下次开新会话
        return "❌ 该会话内容过大（超过 32MB API 限制），无法恢复。\n已自动切换为新会话模式，请重新发送消息。"

    return (result_text or "").strip()


async def run_claude_with_image(image_path: str, caption: str, streamer: "TgStreamer | None" = None) -> str:
    global _new_session, _resume_session_id

    prompt_text = caption or "请描述这张图片的内容"
    print(f"\n{'─' * 60}")
    print(f"🖼️  图片 + 文字：{prompt_text}")
    print(f"{'─' * 60}\n")
    sys.stdout.flush()

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower()
    media_type_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
    media_type = media_type_map.get(ext, "image/jpeg")

    message = {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
            {"type": "text", "text": prompt_text},
        ],
    }
    stdin_payload = json.dumps({"type": "user", "message": message}) + "\n"

    session_flags = _build_session_flags()
    cmd = (
        ["claude", "--dangerously-skip-permissions"]
        + session_flags
        + [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
    )
    _new_session = False
    _resume_session_id = None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=CLAUDE_CWD,
    )

    result_text = ""

    async def _stream():
        nonlocal result_text
        proc.stdin.write(stdin_payload.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        async for line in proc.stdout:
            decoded = line.decode("utf-8", errors="replace")
            print(decoded, end="", flush=True)
            s = decoded.strip()
            if not s:
                continue
            try:
                event = json.loads(s)
            except json.JSONDecodeError:
                continue
            await _dispatch_event(event, streamer)
            if event.get("type") == "result":
                result_text = event.get("result", "") or result_text

    try:
        await asyncio.wait_for(_stream(), timeout=300)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        if streamer:
            await streamer.clear_status()
        return "❌ 执行超时（超过5分钟）"

    print(f"\n{'─' * 60}")
    return (result_text or "").strip()


# ── Telegram handlers ─────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("⏳ 处理中...")
    streamer = TgStreamer(thinking)
    streamer.start_heartbeat()
    try:
        response = await run_claude(update.message.text, streamer)
    except Exception as e:
        await streamer.finalize(fallback=f"❌ 出错了：{e}")
        return
    await streamer.finalize(fallback=response)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("🎙️ 识别语音中...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(tmp_path)
        text = await transcribe_voice(tmp_path)
        print(f"\n🎙️  语音识别结果：{text}")
    except Exception as e:
        try:
            await thinking.edit_text(f"❌ 出错了：{e}")
        except Exception:
            pass
        os.unlink(tmp_path)
        return
    os.unlink(tmp_path)

    if not text:
        try:
            await thinking.edit_text("❌ 语音识别失败，请重试")
        except Exception:
            pass
        return

    streamer = TgStreamer(thinking, prefix=f"🎙️ 已识别：{text}")
    streamer.start_heartbeat()
    try:
        response = await run_claude(text, streamer)
    except Exception as e:
        await streamer.finalize(fallback=f"❌ 出错了：{e}")
        return
    await streamer.finalize(fallback=response)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("🖼️ 处理图片中...")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        await photo_file.download_to_drive(tmp_path)
    except Exception as e:
        try:
            await thinking.edit_text(f"❌ 出错了：{e}")
        except Exception:
            pass
        os.unlink(tmp_path)
        return

    caption = update.message.caption or ""
    streamer = TgStreamer(thinking, prefix=f"🖼️ 图片：{caption}" if caption else "🖼️ 图片")
    streamer.start_heartbeat()
    try:
        response = await run_claude_with_image(tmp_path, caption, streamer)
    except Exception as e:
        await streamer.finalize(fallback=f"❌ 出错了：{e}")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    await streamer.finalize(fallback=response)


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _new_session, _resume_session_id
    _new_session = True
    _resume_session_id = None
    await update.message.reply_text("🔄 下一条消息将开启全新对话")


async def on_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions = list_sessions(10)
    if not sessions:
        await update.message.reply_text("❌ 没有找到历史会话")
        return
    lines = ["📋 最近会话（用 /resume <编号> 切换）\n"]
    for i, s in enumerate(sessions, 1):
        t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
        summary = s["summary"] or "（无文字内容）"
        lines.append(f"{i}. [{t}] {summary}\n    📁 {s['proj']}")
    await update.message.reply_text("\n".join(lines))


async def on_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _new_session, _resume_session_id
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("用法：/resume <编号>\n先用 /sessions 查看编号")
        return

    idx = int(args[0]) - 1
    sessions = list_sessions(10)
    if idx < 0 or idx >= len(sessions):
        await update.message.reply_text(f"❌ 编号超出范围，共 {len(sessions)} 个会话")
        return

    s = sessions[idx]
    _resume_session_id = s["id"]
    _new_session = False
    t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
    summary = s["summary"] or "（无文字内容）"
    await update.message.reply_text(f"✅ 已切换到会话 {idx+1}\n[{t}] {summary}\n\n发消息继续这个会话吧")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Claude Code Gateway 已就绪\n\n"
        "支持：文字 / 语音 / 图片（可附加文字说明）\n\n"
        "命令：\n"
        "/sessions — 查看历史会话列表\n"
        "/resume <编号> — 切换到指定会话\n"
        "/new — 开启全新会话"
    )


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        print("⏳ 加载 Whisper 模型（首次需要下载，稍等）...")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        print("✅ Whisper 模型已就绪")
    return _whisper_model


async def transcribe_voice(file_path: str) -> str:
    loop = asyncio.get_event_loop()
    def _run():
        model = get_whisper_model()
        segments, _ = model.transcribe(file_path, beam_size=5)
        return "".join(seg.text for seg in segments).strip()
    return await loop.run_in_executor(None, _run)


def main():
    print("🤖 Claude Code Telegram Gateway 启动中...")
    print(f"   Bot Token: {BOT_TOKEN[:15]}...")
    print("   等待手机消息...\n")

    import httpx
    proxy = "socks5://127.0.0.1:53542"
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30).read_timeout(30).write_timeout(30)
        .proxy(proxy)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(CommandHandler("sessions", on_sessions))
    app.add_handler(CommandHandler("resume", on_resume))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
