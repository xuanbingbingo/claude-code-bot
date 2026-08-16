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
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime

CLAUDE_CWD = os.environ.get("CLAUDE_CWD", os.path.expanduser("~/aiProjects"))

# 失败后的提示：保留会话（不自动重置），用户可直接重发接着干；
# 如确实想重开，自己发 /new。网络抖动等临时问题不该丢掉整个会话上下文。
_RETRY_NOTE = "\n\n↩️ 会话已保留，直接重发即可接着上面继续；如想重开会话发 /new。"

# 单次 claude 执行超时（秒）。长任务被砍会导致飞书消息「没返回全」，默认放宽到 1800s（30 分钟）。
# 可用环境变量 CLAUDE_RUN_TIMEOUT 覆盖。
_RUN_TIMEOUT = int(os.environ.get("CLAUDE_RUN_TIMEOUT", "1800"))
_RUN_TIMEOUT_MSG = f"❌ 执行超时（超过 {_RUN_TIMEOUT // 60} 分钟）。{_RETRY_NOTE}"

# 静默看门狗：单次超过该秒数没有任何流输出，判定 API 流卡死（stalled），
# 立即中断而不是干等总超时。可用 CLAUDE_STALL_TIMEOUT 覆盖。
_STALL_TIMEOUT = int(os.environ.get("CLAUDE_STALL_TIMEOUT", "180"))

# ⚠️ 分级：工具执行期间流上本来就没有任何事件（Bash 在跑视频渲染/大下载时可以静默好几
# 分钟），拿等 API 的 180s 去卡它属于误杀——历史上 gen-video 就是这么被砍的。
# 已发出 tool_use 但还没收到 tool_result 时改用这把更长的尺子；等 API 仍用短的，
# 真·流中断依旧能被快速发现。可用 CLAUDE_TOOL_STALL_TIMEOUT 覆盖。
_TOOL_STALL_TIMEOUT = max(_STALL_TIMEOUT,
                          int(os.environ.get("CLAUDE_TOOL_STALL_TIMEOUT", "900")))

# 🔴 stream-json 是「一行一个事件」，而 asyncio 的 StreamReader 默认只肯攒 64KB，
# 超了 readline() 直接抛 ValueError("Separator is not found, and chunk exceed the
# limit")，整轮任务当场毙掉——历史现场：读了几张视频关键帧后，单个 tool_result
# 事件轻松破 64KB，前 13 步全绿最后一步报这个错。默认放到 64MB（对齐 API 的 32MB
# 上限，留一倍余量）。可用 CLAUDE_STREAM_LIMIT 覆盖（单位字节）。
_STREAM_LIMIT = int(os.environ.get("CLAUDE_STREAM_LIMIT", str(64 * 1024 * 1024)))

# 逐 token 流式开关，默认开（飞书要像 CLI 一样实时流式）。
# 可用 CLAUDE_PARTIAL_MESSAGES=0 关闭。
_PARTIAL_FLAG = (["--include-partial-messages"]
                 if os.environ.get("CLAUDE_PARTIAL_MESSAGES", "1") != "0" else [])

# 思考深度上限（token）。Opus 高强度扩展思考会跑几分钟，飞书上看着像卡死。
# 给一个上限让响应保持迅捷；可用 CLAUDE_MAX_THINKING 调整，设 "0" 取消上限。
_MAX_THINKING = os.environ.get("CLAUDE_MAX_THINKING", "8000").strip()
_STALL_MSG = f"⚠️ 模型响应卡住了（{_STALL_TIMEOUT}s 无任何输出），已中断。多半是 API 流中断。{_RETRY_NOTE}"
_TOOL_STALL_MSG = (f"⚠️ 某个工具跑了 {_TOOL_STALL_TIMEOUT // 60} 分钟还没吭声，已中断。"
                   f"长任务请让我用 detach-run 甩到后台，别在对话里干等。{_RETRY_NOTE}")


class _StreamStalled(Exception):
    """流静默超时。in_tool 区分「工具跑太久」与「API 流断了」，善后文案不同。"""

    def __init__(self, in_tool: bool = False):
        super().__init__()
        self.in_tool = in_tool


@dataclass
class _RunOutcome:
    """一次 claude 子进程执行的原始结局；分类与善后统一在 ClaudeSession._settle 做。"""
    text: str = ""
    is_error: bool = False
    api_error_status: int | None = None
    raw_tail: str = ""
    stalled: bool = False
    stalled_in_tool: bool = False
    timed_out: bool = False


def _tool_delta(event: dict) -> int:
    """本事件让「在跑的工具数」变化多少：发起 tool_use +1，回 tool_result -1。

    只认顶层 assistant/user 事件，不认 stream_event —— 后者是同一次 tool_use 的
    逐块增量，按它计数会把一个工具重复加好几次。
    """
    t = event.get("type")
    if t not in ("assistant", "user"):
        return 0
    want = "tool_use" if t == "assistant" else "tool_result"
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    n = sum(1 for b in content if isinstance(b, dict) and b.get("type") == want)
    return n if t == "assistant" else -n


_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# 判为「上游/代理瞬时故障」的文本特征（小写匹配）。
# 关键教训——用户的 CLI 走自建推理网关(127.0.0.1:8888 / AntProxy)，
# 该网关常抖出 502 upstream unreachable / 503 warming up / 401 auth / 429。
# 这些是上游临时故障，会话本身完好无损，绝不能因此丢会话。
_TRANSIENT_MARKERS = (
    "upstream unreachable", "warming up", "overloaded",
    "server-side issue", "try again", "timeout",
    "authenticat",    # 401：代理鉴权抖动，会话没坏（覆盖 authenticate/authentication）
)


def _classify_error(api_error_status: int | None, text: str) -> str:
    """把一次失败分成 transient（瞬时，保会话）/ fatal（会话历史损坏，才考虑换线）。"""
    if api_error_status in _TRANSIENT_STATUSES:
        return "transient"
    low = (text or "").lower()
    if any(m in low for m in _TRANSIENT_MARKERS):
        return "transient"
    return "fatal"


def _heal_session_tail(sid: str) -> None:
    """自愈被强杀截断的会话 jsonl 尾部。

    超时/卡死杀 claude 进程可能把正在写的最后一行截成半行 JSON，
    --resume 解析残行会失败，进而把整条会话误判成「历史损坏」丢掉上下文。
    规则：尾字节是换行 → 完好不动；尾行是完整 JSON 只缺换行 → 补换行；
    尾行残缺 → 截到最后一个完整行。窗口内找不到行首（单行超大，如图片
    base64）则宁可不动。
    """
    path = _find_session_file(sid)
    if not path:
        return
    try:
        with open(path, "rb+") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return
            window = min(size, 8 * 1024 * 1024)
            f.seek(size - window)
            tail = f.read(window)
            nl = tail.rfind(b"\n")
            if nl < 0 and window < size:
                return
            last = tail[nl + 1:]
            try:
                json.loads(last.decode("utf-8"))
                f.write(b"\n")
                print(f"[WARN] 会话 {sid[:8]} 尾行缺换行，已补全")
            except Exception:
                f.truncate(size - len(last))
                print(f"[WARN] 会话 {sid[:8]} 尾行残缺（疑被强杀截断），已修剪 {len(last)} 字节")
    except Exception as e:
        print(f"[WARN] 会话文件自愈失败 {sid[:8]}:{e}")


async def _graceful_kill(proc, grace: float = 5.0) -> None:
    """先 SIGTERM 给 claude 留 flush 会话文件的机会，宽限 grace 秒再 SIGKILL。

    直接 proc.kill() 会把正在写的会话 jsonl 截断成半行，是「超时后会话
    恢复失败、上下文丢失」的根源之一；任何要杀子进程的路径都必须走这里。
    等待回收的任何异常（含「Future attached to a different loop」）一律吞掉：
    信号已发出，进程一定会死。
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:
        return
    try:
        proc.kill()
    except Exception:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception:
        pass


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
    elif t == "system" and event.get("subtype") == "thinking_tokens":
        # 思考内容常被中转脱敏成空串，这里用 token 计数做实时进度显示
        tokens = event.get("estimated_tokens")
        if tokens is not None and hasattr(streamer, "set_thinking_progress"):
            await streamer.set_thinking_progress(tokens)
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
        # 最近一次「成功跑完」的会话 id。失败轮也会刷 current_session_id
        # （init 事件先于报错到达），恢复失败时靠它回退、重启续接也优先
        # 持久化它（见 resumable_session_id），避免指针停在残缺会话上。
        self.last_good_session_id: str | None = None
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
        # 换线就不该再回退到旧线：否则新会话首轮一失败，兜底逻辑会把
        # 上一段无关对话的上下文诈尸回来。
        self.last_good_session_id = None

    def set_resume_session(self, session_id: str | None):
        self.resume_session_id = session_id
        self.new_session = False
        self.current_session_id = session_id
        # 用户/恢复逻辑显式选中的会话即视为完好基线
        self.last_good_session_id = session_id

    def set_cwd(self, new_cwd: str) -> None:
        """切换工作目录，默认开新会话（要接旧会话用 /resume）"""
        self.cwd = new_cwd
        self.set_new_session()

    @property
    def resumable_session_id(self) -> str | None:
        """重启续接该落盘的 id：当前指针的 jsonl 还在就用它，否则回退最近成功会话。
        SessionManager.persist 优先取它，保证落盘的指针永远指向可恢复的记录。"""
        for sid in (self.current_session_id, self.last_good_session_id):
            if sid and _session_file_exists(sid):
                return sid
        return None

    def build_session_flags(self) -> list[str]:
        # 显式 /resume <sid> 优先（来自用户命令或重启恢复）
        if self.resume_session_id:
            _heal_session_tail(self.resume_session_id)
            return ["--resume", self.resume_session_id]
        if self.new_session:
            return []
        # 钉死本网关自己那条会话：用 --resume <sid>，绝不用 --continue。
        # --continue 接的是「工作目录里最近活跃的会话」，飞书网关与 CLI 共用
        # 同一个 cwd（~/aiProjects）的会话池，--continue 会串到 CLI 的会话上。
        # 优先当前指针；其 jsonl 不在（上轮被杀/被清）则回退最近一次成功会话。
        for sid in (self.current_session_id, self.last_good_session_id):
            if not sid or not _session_file_exists(sid):
                continue
            if sid != self.current_session_id:
                print(f"[WARN] 会话 {(self.current_session_id or '?')[:8]} 记录不在，回退上一完好会话 {sid[:8]}")
                self.current_session_id = sid
            _heal_session_tail(sid)
            return ["--resume", sid]
        if self.current_session_id or self.last_good_session_id:
            print("[WARN] 无可恢复的会话记录，本轮开新会话")
        return []

    def _spawn_env(self) -> dict:
        """spawn claude 用的环境：合并进程环境 + 会话注入 + 思考上限。"""
        env = {**os.environ, **self.extra_env}
        if _MAX_THINKING and _MAX_THINKING != "0":
            env["MAX_THINKING_TOKENS"] = _MAX_THINKING
        return env

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
        """终止当前运行中的 Claude 子进程，返回是否实际杀掉了东西。

        走 _graceful_kill：先 SIGTERM 留给 claude flush 会话文件的机会，
        宽限后才 SIGKILL；跨 loop 等待异常一律吞掉，/stop 永远干净返回。
        """
        proc = self.current_proc
        if proc is None or proc.returncode is not None:
            return False
        await _graceful_kill(proc, grace=2.0)
        return True

    async def _execute(self, cmd: list[str], history_prompt: str, streamer,
                       stdin_payload: bytes | None = None) -> _RunOutcome:
        """跑一次 claude 子进程、泵事件流，返回原始结局（分类善后见 _settle）。

        文本与图片两条路径共用这一个引擎——历史教训：两份复制粘贴的执行
        循环让错误处理只修了文本路径，图片路径同类 bug 原样复发。
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_payload else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.cwd,
            env=self._spawn_env(),
            limit=_STREAM_LIMIT,
        )
        self.current_proc = proc
        out = _RunOutcome()

        async def _pump():
            history_logged = False
            pending_tools = 0          # 已发出 tool_use 但未回 tool_result 的数量
            if stdin_payload:
                proc.stdin.write(stdin_payload)
                await proc.stdin.drain()
                proc.stdin.close()
            while True:
                # 单行读取设静默上限。工具执行中用更长的尺子（见 _TOOL_STALL_TIMEOUT
                # 注释）：Bash 跑长任务时流上本就没有事件，短超时会误杀。
                in_tool = pending_tools > 0
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=_TOOL_STALL_TIMEOUT if in_tool else _STALL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise _StreamStalled(in_tool=in_tool)
                except ValueError:
                    # 兜底：单行连 _STREAM_LIMIT 都撑爆了。asyncio 在抛错前已经把
                    # 缓冲清空，残余半行会被下一轮当新行读到，由下面的
                    # JSONDecodeError 静默跳过。丢一个事件也远好过整轮任务崩掉
                    # ——上限只该是「降级线」，不该是「断头台」。
                    print("\n⚠️ 跳过一行超长事件（超出 stream limit）", flush=True)
                    continue
                if not line:
                    break  # EOF，进程正常结束
                decoded = line.decode("utf-8", errors="replace")
                out.raw_tail = (out.raw_tail + decoded)[-4096:]
                print(decoded, end="", flush=True)
                s = decoded.strip()
                if not s:
                    continue
                try:
                    event = json.loads(s)
                except json.JSONDecodeError:
                    continue
                pending_tools = max(0, pending_tools + _tool_delta(event))
                if event.get("type") == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        self.current_session_id = sid
                        if not history_logged:
                            append_history_entry(history_prompt, self.cwd, sid)
                            ensure_session_title(sid, history_prompt)
                            history_logged = True
                await _dispatch_event(event, streamer)
                if event.get("type") == "result":
                    out.text = event.get("result", "") or out.text
                    if event.get("is_error"):
                        out.is_error = True
                        out.api_error_status = event.get("api_error_status")

        try:
            try:
                await asyncio.wait_for(_pump(), timeout=_RUN_TIMEOUT)
                await proc.wait()
            except _StreamStalled as e:
                await _graceful_kill(proc)
                out.stalled = True
                out.stalled_in_tool = e.in_tool
            except asyncio.TimeoutError:
                await _graceful_kill(proc)
                out.timed_out = True
        finally:
            if self.current_proc is proc:
                self.current_proc = None
        return out

    async def _settle(self, out: _RunOutcome, streamer) -> str:
        """统一善后：超时/卡死/错误分类/指针提升，任何执行路径都不许绕过。

        核心原则：区分「上游/代理瞬时故障」与「真·会话历史损坏」。
        瞬时故障（5xx/429/408/401/upstream unreachable/warming up 等）
        一律保留会话让用户重发续上；真损坏也先回退最近成功会话，
        回退不了才开新——「一出错就核爆整条会话」正是丢上下文 bug 的老根。
        """
        async def _notify(msg: str):
            # 异常结局要让用户在卡片上看到，不能只藏在返回值里
            # （streamer 已有内容时 finalize 的 fallback 会被忽略）。
            if streamer:
                await streamer.clear_status()
                if getattr(streamer, "has_content", False):
                    await streamer.append(f"\n\n{msg}")

        if out.stalled:
            secs = _TOOL_STALL_TIMEOUT if out.stalled_in_tool else _STALL_TIMEOUT
            what = "工具执行" if out.stalled_in_tool else "流"
            print(f"\n🛑 {what}卡死（{secs}s 无输出），已中断，会话保留")
            msg = _TOOL_STALL_MSG if out.stalled_in_tool else _STALL_MSG
            await _notify(msg)
            return msg
        if out.timed_out:
            print("\n⏰ 执行超时（会话保留）")
            await _notify(_RUN_TIMEOUT_MSG)
            return _RUN_TIMEOUT_MSG

        print(f"\n{'─' * 60}")

        if "Request too large" in out.raw_tail or "max 32MB" in out.raw_tail:
            self.set_new_session()
            return ("❌ 该会话内容过大（超过 32MB API 限制），无法恢复。\n"
                    "已自动切换为新会话模式，请重新发送消息。")

        if out.is_error:
            if _classify_error(out.api_error_status, out.text) == "transient":
                tag = f"（{out.api_error_status}）" if out.api_error_status else ""
                msg = f"⚠️ 上游/代理临时故障{tag}，本次输出被中断。{_RETRY_NOTE}"
                await _notify(msg)
                return msg
            dead = self.current_session_id
            fb = self.last_good_session_id
            if fb and fb != dead and _session_file_exists(fb):
                self.set_resume_session(fb)
                msg = (f"⚠️ 会话恢复失败：{out.text}\n\n"
                       f"已自动回退到上一个完好会话（{fb[:8]}…），直接重发消息即可接着继续。")
                await _notify(msg)
                return msg
            self.set_new_session()
            msg = f"⚠️ 会话恢复失败：{out.text}\n\n已自动切换为新会话模式，请重发消息。"
            await _notify(msg)
            return msg

        # 成功：提升「最近成功会话」，供失败回退与重启续接兜底
        if self.current_session_id:
            self.last_good_session_id = self.current_session_id
        return (out.text or "").strip()

    async def run_claude(self, prompt: str, streamer=None) -> str:
        print(f"\n{'─' * 60}")
        print(f"📱  {prompt}")
        print(f"{'─' * 60}\n")
        sys.stdout.flush()

        await self.stop()
        cmd = (
            self._build_cmd_prefix()
            + self.build_session_flags()
            + ["--output-format", "stream-json"]
            + _PARTIAL_FLAG
            + ["--verbose", "-p", prompt]
        )
        self.new_session = False
        self.resume_session_id = None  # 用一次即清，后续钉住 current_session_id 续接

        out = await self._execute(cmd, prompt, streamer)
        return await self._settle(out, streamer)

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
        stdin_payload = (json.dumps({"type": "user", "message": message}) + "\n").encode()

        await self.stop()
        cmd = (
            self._build_cmd_prefix()
            + self.build_session_flags()
            + ["-p", "--input-format", "stream-json", "--output-format", "stream-json"]
            + _PARTIAL_FLAG
            + ["--verbose"]
        )
        self.new_session = False
        self.resume_session_id = None

        out = await self._execute(cmd, prompt_text, streamer, stdin_payload=stdin_payload)
        return await self._settle(out, streamer)
