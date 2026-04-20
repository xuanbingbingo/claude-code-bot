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
from datetime import datetime
from telegram import Update
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

_new_session = True
_resume_session_id = None  # None=用 --continue，有值=用 --resume <id>
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


# ── Claude 执行 ───────────────────────────────────────────────

def _build_session_flags() -> list[str]:
    if _resume_session_id:
        return ["--resume", _resume_session_id]
    if not _new_session:
        return ["--continue"]
    return []


async def run_claude(prompt: str) -> str:
    global _new_session, _resume_session_id

    print(f"\n{'─' * 60}")
    print(f"📱  {prompt}")
    print(f"{'─' * 60}\n")
    sys.stdout.flush()

    session_flags = _build_session_flags()
    cmd = ["claude", "--dangerously-skip-permissions"] + session_flags + ["-p", prompt]
    _new_session = False
    _resume_session_id = None  # 用一次后清掉，后续靠 --continue

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=CLAUDE_CWD,
    )

    chunks = []
    try:
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace")
            print(text, end="", flush=True)
            chunks.append(text)
        await asyncio.wait_for(proc.wait(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        print("\n⏰ 执行超时")
        return "❌ 执行超时（超过5分钟）"

    print(f"\n{'─' * 60}")
    return "".join(chunks).strip()


async def run_claude_with_image(image_path: str, caption: str) -> str:
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
        + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
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

    stdout, _ = await proc.communicate(input=stdin_payload.encode())
    result_text = ""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                result_text = event.get("result", "")
            elif event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        print(block["text"], end="", flush=True)
        except json.JSONDecodeError:
            print(line, flush=True)

    print(f"\n{'─' * 60}")
    return result_text.strip()


# ── Telegram handlers ─────────────────────────────────────────

async def send_response(update: Update, thinking, response: str):
    await thinking.delete()
    if not response:
        await update.message.reply_text("✅ 完成（无文字输出）")
        return
    for i in range(0, len(response), MAX_TG_LEN):
        await update.message.reply_text(response[i:i + MAX_TG_LEN])


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("⏳ 处理中...")
    try:
        response = await run_claude(update.message.text)
    except Exception as e:
        await thinking.delete()
        await update.message.reply_text(f"❌ 出错了：{e}")
        return
    await send_response(update, thinking, response)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("🎙️ 识别语音中...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(tmp_path)

        text = await transcribe_voice(tmp_path)
        print(f"\n🎙️  语音识别结果：{text}")
        if not text:
            await thinking.delete()
            await update.message.reply_text("❌ 语音识别失败，请重试")
            return

        await thinking.edit_text(f"🎙️ 已识别：{text}\n\n⏳ 处理中...")
        response = await run_claude(text)
    except Exception as e:
        await thinking.delete()
        await update.message.reply_text(f"❌ 出错了：{e}")
        return
    finally:
        os.unlink(tmp_path)
    await send_response(update, thinking, response)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking = await update.message.reply_text("🖼️ 处理图片中...")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        await photo_file.download_to_drive(tmp_path)

        caption = update.message.caption or ""
        response = await run_claude_with_image(tmp_path, caption)
    except Exception as e:
        await thinking.delete()
        await update.message.reply_text(f"❌ 出错了：{e}")
        return
    finally:
        os.unlink(tmp_path)
    await send_response(update, thinking, response)


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
