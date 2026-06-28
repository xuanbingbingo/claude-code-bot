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

# 失败后自动重置会话的提示：避免下一条消息继续 resume 这个已卡死/超时的脏会话，
# 打破「一个会话坏了之后条条都失败」的恶性循环。
_RESET_NOTE = "\n\n🔄 已自动重置会话（下一条消息从全新开始，不再接续这个卡住的会话）。"

# 单次 claude 执行超时（秒）。长任务被砍会导致飞书消息「没返回全」，默认放宽到 600s（10 分钟）。
# 可用环境变量 CLAUDE_RUN_TIMEOUT 覆盖。
_RUN_TIMEOUT = int(os.environ.get("CLAUDE_RUN_TIMEOUT", "600"))
_RUN_TIMEOUT_MSG = f"❌ 执行超时（超过 {_RUN_TIMEOUT // 60} 分钟）。{_RESET_NOTE}"

# 静默看门狗：单次超过该秒数没有任何流输出，判定 API 流卡死（stalled），
# 立即中断而不是干等总超时。可用 CLAUDE_STALL_TIMEOUT 覆盖。
_STALL_TIMEOUT = int(os.environ.get("CLAUDE_STALL_TIMEOUT", "180"))
_STALL_MSG = f"⚠️ 模型响应卡住了（{_STALL_TIMEOUT}s 无任何输出），已自动中断。多半是 API 流中断。{_RESET_NOTE}"


class _StreamStalled(Exception):
    """流在中途静默超过 _STALL_TIMEOUT，判定卡死。"""

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
    """给指定 session 追加一条 custom-title 记录；成功返回 True。

    写入后会立即把 jsonl 的 mtime 还原为写入前的值——CLI /resume 列表
    按 mtime 倒序，标题补丁不应该把老会话顶到列表最前面。
    """
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
        prev_atime = os.path.getatime(path)
        prev_mtime = os.path.getmtime(path)
    except Exception:
        prev_atime = prev_mtime = None
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        return False
    if prev_mtime is not None:
        try:
            os.utime(path, (prev_atime, prev_mtime))
        except Exception:
            pass
    return True


def _has_custom_title(sid: str) -> bool:
    """检查 session 的 jsonl 里是否已有 custom-title。"""
    path = _find_session_file(sid)
    if not path:
        return False
    try:
        with open(path) as f:
            for line in f:
                if '"custom-title"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "custom-title" and d.get("customTitle"):
                        return True
                except Exception:
                    pass
    except Exception:
        return False
    return False


def ensure_session_title(sid: str, prompt: str) -> bool:
    """如果 session 还没标题，用 prompt 首行前 30 字补一条 custom-title。

    解决：飞书/Telegram 走 `claude --print` 时 CLI 不会自动生成 ai-title，
    导致这些会话在 CLI /resume 列表里没有可读标题，用户认不出来。
    """
    if not sid or not prompt:
        return False
    if _has_custom_title(sid):
        return False
    title = prompt.strip().splitlines()[0][:30] if prompt.strip() else ""
    if not title:
        return False
    return set_session_title(sid, title)


# CLI 内置元命令（执行后会创建 sid 但不是真实对话），这些 prompt 不应污染 history.jsonl
_META_SLASH_COMMANDS = {
    "/exit", "/quit",
    "/usage", "/cost", "/status",
    "/help", "/clear", "/compact",
    "/model", "/config", "/permissions",
    "/login", "/logout",
    "/init", "/resume", "/rewind",
    "/agents", "/mcp",
    "/review", "/security-review",
    "/bug", "/release-notes",
    "/ide", "/migrate-installer", "/doctor",
    "/sessions", "/rename", "/cwd", "/stop", "/mode", "/agent", "/start", "/new",
}


def _is_meta_slash_command(prompt: str) -> bool:
    """判断是否是不应记入会话索引的元命令（如 /usage、/exit）。"""
    if not prompt:
        return False
    s = prompt.lstrip()
    if not s.startswith("/"):
        return False
    head = s.split(None, 1)[0].lower()
    return head in _META_SLASH_COMMANDS


def append_history_entry(prompt: str, project: str, session_id: str,
                         timestamp_ms: int | None = None) -> bool:
    """把一条用户输入追加到 ~/.claude/history.jsonl。

    CLI 的 /resume 列表和飞书的 list_sessions 都依赖这个文件做索引；
    飞书走 subprocess 调起 claude 时不会自动写入，需要由网关补一行。
    纯元命令（/usage、/exit 等）会被跳过，避免污染会话索引。
    """
    if not prompt or not session_id or not project:
        return False
    if _is_meta_slash_command(prompt):
        return False
    entry = {
        "display": prompt[:200],
        "pastedContents": {},
        "timestamp": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
        "project": project,
        "sessionId": session_id,
    }
    path = os.path.expanduser("~/.claude/history.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"[WARN] append_history_entry failed: {e}")
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
    """从 history.jsonl 读取指定目录（默认 CLAUDE_CWD）下的最近会话。

    summary 选择优先级：customTitle > 第一条非斜杠命令的 display > 最早的 display。
    避免飞书会话标题全显示成 /exit、/resume、/sessions 等命令字符串。
    """
    target = cwd or CLAUDE_CWD
    history_path = os.path.expanduser("~/.claude/history.jsonl")
    proj_of: dict[str, str] = {}
    latest: dict[str, int] = {}
    displays: dict[str, list[tuple[int, str]]] = {}
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
                    proj_of.setdefault(sid, entry.get("project", ""))
                    latest[sid] = max(latest.get(sid, 0), ts)
                    displays.setdefault(sid, []).append((ts, entry.get("display", "")))
                except Exception:
                    pass
    except Exception:
        return []

    custom_titles = _load_custom_titles()

    def _pick_summary(sid: str) -> str:
        if sid in custom_titles and custom_titles[sid]:
            return custom_titles[sid][:60]
        items = sorted(displays.get(sid, []), key=lambda x: x[0])
        for _, d in items:
            if d and not d.lstrip().startswith("/"):
                return d[:60]
        return (items[0][1] if items else "")[:60]

    sessions = [
        {
            "id": sid,
            "timestamp": latest[sid],
            "summary": _pick_summary(sid),
            "proj": proj_of.get(sid, ""),
        }
        for sid in proj_of
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
            elif dt == "thinking_delta":
                # 扩展思考增量：实时滚进状态行，避免思考期卡片「卡死」
                think = delta.get("thinking") or ""
                if think and hasattr(streamer, "add_thinking"):
                    await streamer.add_thinking(think)
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
        # 默认开新会话：首条消息全新开，同一段对话内后续消息靠 --continue 自动接上。
        # 如需接续旧会话，用 /resume <编号>。
        self.new_session = True
        self.resume_session_id = None
        self.current_session_id: str | None = None
        self.last_cwd_listing: list[str] = []
        self.model: str | None = None  # None = 用 Claude CLI 默认
        self.mode: str = "bypass"
        self.current_proc: asyncio.subprocess.Process | None = None
        # 注入到 claude 子进程环境的额外变量（在 os.environ 之上 merge）。
        # 飞书/TG 网关用它把「当前会话发起人」标识传进去，供 feishu-send-file 等
        # 工具实现「发到当前聊天窗口」。一个会话进程固定服务一个发起人，启动时设一次即可。
        self.extra_env: dict[str, str] = {}
        # 追加到 claude 的 system prompt（--append-system-prompt）。网关用它在不依赖
        # 客户手写 CLAUDE.md 的情况下，告知 claude「有哪些渠道工具可用」，开箱即用。
        self.extra_append_prompt: str = ""

    def set_new_session(self):
        self.new_session = True
        self.resume_session_id = None
        self.current_session_id = None

    def set_resume_session(self, session_id: str | None):
        self.resume_session_id = session_id
        self.new_session = False
        self.current_session_id = session_id

    def set_cwd(self, new_cwd: str) -> None:
        """切换工作目录，默认开新会话（要接旧会话用 /resume）"""
        self.cwd = new_cwd
        self.set_new_session()

    def build_session_flags(self) -> list[str]:
        if self.resume_session_id:
            return ["--resume", self.resume_session_id]
        # 钉死本网关自己那条会话：用 --resume <current_session_id>，
        # 而不是 --continue。--continue 接的是「工作目录里最近活跃的会话」，
        # 由于飞书网关与 CLI 共用同一个 cwd（~/aiProjects）的会话池，
        # 在 CLI 聊过之后再发飞书，--continue 会串到 CLI 的会话上。
        # 改为 --resume 自己的 session id 后，飞书永远只接它自己的线；
        # 主动 /resume <编号> 仍走上面 resume_session_id 分支，可跳到任意 CLI 会话。
        if not self.new_session and self.current_session_id:
            return ["--resume", self.current_session_id]
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
        if self.extra_append_prompt:
            cmd.extend(["--append-system-prompt", self.extra_append_prompt])
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
            env={**os.environ, **self.extra_env},
        )
        self.current_proc = proc

        result_text = ""
        raw_tail = ""
        result_is_error = False

        async def _read():
            nonlocal result_text, raw_tail, result_is_error
            history_logged = False
            while True:
                # 单行读取设静默上限：超过 _STALL_TIMEOUT 没有任何输出＝流卡死
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_STALL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    raise _StreamStalled()
                if not line:
                    break  # EOF，进程正常结束
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
                        if not history_logged:
                            append_history_entry(prompt, self.cwd, sid)
                            ensure_session_title(sid, prompt)
                            history_logged = True
                await _dispatch_event(event, streamer)
                if event.get("type") == "result":
                    result_text = event.get("result", "") or result_text
                    if event.get("is_error"):
                        result_is_error = True

        try:
            try:
                await asyncio.wait_for(_read(), timeout=_RUN_TIMEOUT)
                await proc.wait()
            except _StreamStalled:
                proc.kill()
                await proc.wait()
                self.set_new_session()   # 脏会话不再续，下条从全新开始
                print(f"\n🛑 流卡死（{_STALL_TIMEOUT}s 无输出），已中断并重置会话")
                if streamer:
                    await streamer.clear_status()
                    if getattr(streamer, "has_content", False):
                        await streamer.append(f"\n\n{_STALL_MSG}")
                return _STALL_MSG
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                print("\n⏰ 执行超时")
                self.set_new_session()   # 超时多半是会话已劣化，下条从全新开始
                if streamer:
                    await streamer.clear_status()
                    if getattr(streamer, "has_content", False):
                        await streamer.append(f"\n\n{_RUN_TIMEOUT_MSG}")
                return _RUN_TIMEOUT_MSG
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
            env={**os.environ, **self.extra_env},
        )
        self.current_proc = proc

        result_text = ""

        async def _stream():
            nonlocal result_text
            proc.stdin.write(stdin_payload.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            history_logged = False
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_STALL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    raise _StreamStalled()
                if not line:
                    break
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
                        if not history_logged:
                            append_history_entry(prompt_text, self.cwd, sid)
                            ensure_session_title(sid, prompt_text)
                            history_logged = True
                await _dispatch_event(event, streamer)
                if event.get("type") == "result":
                    result_text = event.get("result", "") or result_text

        try:
            try:
                await asyncio.wait_for(_stream(), timeout=_RUN_TIMEOUT)
                await proc.wait()
            except _StreamStalled:
                proc.kill()
                await proc.wait()
                self.set_new_session()   # 脏会话不再续，下条从全新开始
                print(f"\n🛑 流卡死（{_STALL_TIMEOUT}s 无输出），已中断并重置会话")
                if streamer:
                    await streamer.clear_status()
                    if getattr(streamer, "has_content", False):
                        await streamer.append(f"\n\n{_STALL_MSG}")
                return _STALL_MSG
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self.set_new_session()   # 超时多半是会话已劣化，下条从全新开始
                if streamer:
                    await streamer.clear_status()
                    if getattr(streamer, "has_content", False):
                        await streamer.append(f"\n\n{_RUN_TIMEOUT_MSG}")
                return _RUN_TIMEOUT_MSG
        finally:
            if self.current_proc is proc:
                self.current_proc = None

        print(f"\n{'─' * 60}")
        return (result_text or "").strip()
