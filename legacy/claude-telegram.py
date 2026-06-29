#!/usr/bin/env python3
"""
Claude Code Telegram Gateway
手机发消息/语音/图片 → 本地 Claude Code 执行 → 终端实时显示 + 手机收到回复
"""

import asyncio
import base64
import os
import sys
import tempfile
import time
from datetime import datetime
from telegram import Update, Message
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from claude_core import ClaudeSession, list_sessions

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

_session = ClaudeSession()
_whisper_model = None


# ── Telegram 流式输出 ─────────────────────────────────────────

class TgStreamer:
    """把 Claude Code 的 stream-json 事件实时推到 Telegram。"""

    MAX_LEN = 3500
    THROTTLE = 1.2

    def __init__(self, message: Message, prefix: str = ""):
        self.msg = message
        self.prefix = prefix
        self.text = ""
        self.status = ""
        self.last_edit = 0.0
        self.pending: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.has_content = False
        self.partial_mode = False
        self._hb_task: asyncio.Task | None = None
        self._started_at = time.monotonic()

    def start_heartbeat(self):
        if self._hb_task is None:
            self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        try:
            while not self.has_content:
                elapsed = int(time.monotonic() - self._started_at)
                if not self.status or self.status.startswith("⏳"):
                    await self.set_status(f"⏳ 处理中 {elapsed}s...")
                await asyncio.sleep(2)
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
                pass
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
            await self.flush()
            try:
                self.msg = await self.msg.reply_text("⏳ 继续...")
            except Exception:
                pass
            self.text = ""
            self.prefix = ""
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
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        if self.pending and not self.pending.done():
            self.pending.cancel()
        self.status = ""
        if not self.has_content:
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
        await self._do_edit()


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("⏳ 处理中...")
    streamer = TgStreamer(thinking)
    streamer.start_heartbeat()
    try:
        response = await _session.run_claude(update.message.text, streamer)
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

    streamer = TgStreamer(thinking, prefix=f"🎙️已识别：{text}")
    streamer.start_heartbeat()
    try:
        response = await _session.run_claude(text, streamer)
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
        response = await _session.run_claude_with_image(tmp_path, caption, streamer)
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
    _session.set_new_session()
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
    _session.set_resume_session(s["id"])
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
    # 代理可配：设了 TELEGRAM_PROXY 就走显式代理，留空则直连（依赖系统 TUN 透明转发）
    proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30).read_timeout(30).write_timeout(30)
    )
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
        print(f"   代理: {proxy}")
    else:
        print("   代理: 直连（依赖系统 TUN）")
    app = builder.build()
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
