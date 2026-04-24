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


def _last_session_id(cwd: str | None = None) -> str | None:
    """从 history.jsonl 取指定目录（默认 CLAUDE_CWD）下最新且仍存在的会话 ID"""
    target = cwd or CLAUDE_CWD
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
                    if d.get("project") == target:
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


def _parse_agent_frontmatter(path: str) -> dict | None:
    """从 agent 的 markdown 文件读取 YAML frontmatter。支持 |/> 块标量。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None
    if not lines or lines[0].strip() != "---":
        return None

    meta: dict[str, str] = {}
    i = 1
    while i < len(lines):
        s = lines[i].rstrip("\n")
        if s.strip() == "---":
            break
        if ":" not in s or s.startswith(" "):
            i += 1
            continue
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.strip()
        if val in ("|", ">"):
            # 读后续缩进行作为块标量
            block: list[str] = []
            i += 1
            while i < len(lines):
                t = lines[i].rstrip("\n")
                if t.strip() == "---":
                    break
                if t and not t.startswith((" ", "\t")):
                    break
                block.append(t.strip())
                i += 1
            # 合并成单行（去掉空行间隔）
            meta[key] = " ".join(x for x in block if x)
            continue
        meta[key] = val.strip("'\"")
        i += 1

    if not meta.get("name"):
        return None
    return meta


def list_agents(cwd: str | None = None) -> list[dict]:
    """扫描全局 + 项目级 agent 目录，返回 [{name, description, scope, model, path}, ...]。
    scope='project' 优先于 'user'，同名以 project 覆盖。
    """
    target_cwd = cwd or CLAUDE_CWD
    result: dict[str, dict] = {}
    sources = [
        ("user", os.path.expanduser("~/.claude/agents")),
        ("project", os.path.join(target_cwd, ".claude", "agents")),
    ]
    for scope, base in sources:
        if not os.path.isdir(base):
            continue
        for fname in sorted(os.listdir(base)):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(base, fname)
            meta = _parse_agent_frontmatter(path)
            if not meta:
                continue
            result[meta["name"]] = {
                "name": meta["name"],
                "description": meta.get("description", ""),
                "model": meta.get("model", ""),
                "scope": scope,
                "path": path,
            }
    return sorted(result.values(), key=lambda a: (a["scope"] != "project", a["name"].lower()))


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


def list_sessions(limit: int = 10, cwd: str | None = None) -> list[dict]:
    """从 history.jsonl 读取指定目录（默认 CLAUDE_CWD）下的最近会话"""
    target = cwd or CLAUDE_CWD
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
                    if entry.get("project") != target:
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
        if _session_file_exists(sid)
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

    MODES = {
        "bypass": "--dangerously-skip-permissions",
        "plan": ("--permission-mode", "plan"),
        "default": ("--permission-mode", "default"),
        "accept": ("--permission-mode", "acceptEdits"),
    }

    def __init__(self, cwd: str | None = None):
        self.cwd = cwd or CLAUDE_CWD
        self.new_session = False
        self.resume_session_id = _last_session_id(self.cwd)
        self.current_session_id: str | None = self.resume_session_id
        self.last_cwd_listing: list[str] = []
        self.model: str | None = None  # None = 用 Claude CLI 默认
        self.mode: str = "bypass"
        self.current_proc: asyncio.subprocess.Process | None = None

    def set_new_session(self):
        self.new_session = True
        self.resume_session_id = None
        self.current_session_id = None

    def set_resume_session(self, session_id: str | None):
        self.resume_session_id = session_id
        self.new_session = False
        self.current_session_id = session_id

    def set_cwd(self, new_cwd: str) -> None:
        """切换工作目录，并自动接续新目录下最新会话（无则开新会话）"""
        self.cwd = new_cwd
        last = _last_session_id(self.cwd)
        if last:
            self.set_resume_session(last)
        else:
            self.set_new_session()

    def build_session_flags(self) -> list[str]:
        if self.resume_session_id:
            return ["--resume", self.resume_session_id]
        if not self.new_session:
            return ["--continue"]
        return []

    def _build_cmd_prefix(self) -> list[str]:
        """组合 mode + model，返回 `claude <mode-flag> [--model <x>]`。"""
        cmd: list[str] = ["claude"]
        mode_flag = self.MODES.get(self.mode, self.MODES["bypass"])
        if isinstance(mode_flag, tuple):
            cmd.extend(mode_flag)
        else:
            cmd.append(mode_flag)
        if self.model:
            cmd.extend(["--model", self.model])
        return cmd

    def set_model(self, name: str | None) -> None:
        """设置模型别名（opus/sonnet/haiku）或完整 ID；None/空 → 清除。"""
        self.model = (name or "").strip() or None

    def set_mode(self, name: str) -> bool:
        """切换权限模式，返回是否合法。"""
        if name not in self.MODES:
            return False
        self.mode = name
        return True

    async def stop(self) -> bool:
        """终止当前运行中的 Claude 子进程，返回是否实际杀掉了东西。"""
        proc = self.current_proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.terminate()
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        return True

    async def run_claude(self, prompt: str, streamer=None) -> str:
        print(f"\n{'─' * 60}")
        print(f"📱  {prompt}")
        print(f"{'─' * 60}\n")
        sys.stdout.flush()

        await self.stop()
        session_flags = self.build_session_flags()
        cmd = (
            self._build_cmd_prefix()
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
            cwd=self.cwd,
        )
        self.current_proc = proc

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
        finally:
            if self.current_proc is proc:
                self.current_proc = None

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

        await self.stop()
        session_flags = self.build_session_flags()
        cmd = (
            self._build_cmd_prefix()
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
            cwd=self.cwd,
        )
        self.current_proc = proc

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
            try:
                await asyncio.wait_for(_stream(), timeout=300)
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                if streamer:
                    await streamer.clear_status()
                return "❌ 执行超时（超过5分钟）"
        finally:
            if self.current_proc is proc:
                self.current_proc = None

        print(f"\n{'─' * 60}")
        return (result_text or "").strip()
