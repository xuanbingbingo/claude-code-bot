#!/usr/bin/env python3
"""
Claude Code Gateway - Core Logic
被 Telegram 和 Feishu 渠道共享的核心逻辑
"""

import asyncio
import base64
import glob
import json
import os
import sys
import time
from datetime import datetime

CLAUDE_CWD = os.environ.get("CLAUDE_CWD", os.path.expanduser("~/aiProjects"))

_WHISPER_MODEL = None


def transcribe_audio(file_path: str) -> str:
    """用 faster-whisper 把音频文件转成中文文本。首次调用会加载模型。"""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        print("⏳ 加载 Whisper 模型（首次需要下载，稍等）...")
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        print("✅ Whisper 模型已就绪")
    segments, _ = _WHISPER_MODEL.transcribe(file_path, beam_size=5)
    return "".join(seg.text for seg in segments).strip()


def _session_file_exists(sid: str) -> bool:
    """检查 session 对应的 jsonl 是否还在 ~/.claude/projects 下存在。"""
    return _find_session_file(sid) is not None


def _find_session_file(sid: str) -> str | None:
    """定位 session 对应的 jsonl 路径。"""
    if not sid:
        return None
    base = os.path.expanduser("~/.claude/projects")
    for path in glob.iglob(f"{base}/**/{sid}.jsonl", recursive=True):
        if "/subagents/" in path:
            continue
        return path
    return None


def set_session_title(sid: str, title: str) -> bool:
    """给指定 session 追加一条 custom-title 记录；成功返回 True。"""
    if not sid or not title:
        return False
    path = _find_session_file(sid)
    if not path:
        return False
    entry = {
        "type": "custom-title",
        "sessionId": sid,
        "customTitle": title,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _last_session_id() -> str | None:
    """从 history.jsonl 取 CLAUDE_CWD 目录下最新且仍存在的会话 ID"""
    path = os.path.expanduser("~/.claude/history.jsonl")
    candidates: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("project") == CLAUDE_CWD:
                        sid = d.get("sessionId")
                        if sid:
                            candidates.append(sid)
                except Exception:
                    pass
    except Exception:
        return None
    # 倒序遍历，找最近且 jsonl 仍存在的
    for sid in reversed(candidates):
        if _session_file_exists(sid):
            return sid
    return None


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


async def _dispatch_event(event: dict, streamer):
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
        elif et == "content_block_delta":
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                streamer.partial_mode = True
                txt = delta.get("text") or ""
                if txt:
                    await streamer.append(txt)
        elif et == "content_block_stop":
            await streamer.clear_status()
    elif t == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text" and not streamer.partial_mode:
                text = block.get("text") or ""
                if text:
                    await streamer.append(text)
            elif bt == "tool_use":
                await streamer.set_status(_format_tool_use(block))
    elif t == "user":
        await streamer.clear_status()
    elif t == "system" and event.get("subtype") == "status":
        status = event.get("status", "")
        if status == "requesting" and not streamer.has_content:
            pass


class ClaudeSession:
    """每个用户/渠道独立的 Claude 会话状态管理"""

    def __init__(self):
        self.new_session = False
        self.resume_session_id = _last_session_id()
        self.current_session_id: str | None = self.resume_session_id

    def set_new_session(self):
        self.new_session = True
        self.resume_session_id = None
        self.current_session_id = None

    def set_resume_session(self, session_id: str | None):
        self.resume_session_id = session_id
        self.new_session = False
        self.current_session_id = session_id

    def build_session_flags(self) -> list[str]:
        if self.resume_session_id:
            return ["--resume", self.resume_session_id]
        if not self.new_session:
            return ["--continue"]
        return []

    async def run_claude(self, prompt: str, streamer=None) -> str:
        print(f"\n{'─' * 60}")
        print(f"📱  {prompt}")
        print(f"{'─' * 60}\n")
        sys.stdout.flush()

        session_flags = self.build_session_flags()
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
        self.new_session = False
        self.resume_session_id = None  # 用一次后清掉，后续靠 --continue

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=CLAUDE_CWD,
        )

        result_text = ""
        raw_tail = ""
        result_is_error = False

        async def _read():
            nonlocal result_text, raw_tail, result_is_error
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
                if event.get("type") == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        self.current_session_id = sid
                await _dispatch_event(event, streamer)
                if event.get("type") == "result":
                    result_text = event.get("result", "") or result_text
                    if event.get("is_error"):
                        result_is_error = True

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
            self.new_session = True
            return "❌ 该会话内容过大（超过 32MB API 限制），无法恢复。\n已自动切换为新会话模式，请重新发送消息。"

        # resume 的会话历史里有脏数据导致 API 拒绝 → 自动切新会话
        if result_is_error and "API Error" in (result_text or ""):
            self.new_session = True
            return (
                f"⚠️ 恢复会话失败：{result_text}\n\n"
                "已自动切换为新会话模式，请重发消息。"
            )

        return (result_text or "").strip()

    async def run_claude_with_image(self, image_path: str, caption: str, streamer=None) -> str:
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

        session_flags = self.build_session_flags()
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
        self.new_session = False
        self.resume_session_id = None

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
                if event.get("type") == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        self.current_session_id = sid
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
