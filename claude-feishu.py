#!/usr/bin/env python3
"""
Claude Code Feishu (Lark) Gateway - 长连接模式
手机/飞书发消息/图片 → 本地 Claude Code 执行 → 飞书收到回复

使用飞书开放平台「长连接」事件订阅方式，无需公网地址和 nginx 反代。
本地直接通过 WebSocket 连接飞书服务器接收消息。
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime

import httpx
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from claude_core import ClaudeSession, list_sessions, set_session_title, _session_file_exists, transcribe_audio, list_agents

# 加载 .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN", "")
if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
    print("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请在 .env 中设置")
    sys.exit(1)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
MAX_MSG_LEN = 3800

# 每个 open_id → ClaudeSession
_sessions: dict[str, ClaudeSession] = {}


# ── 飞书 API 同步封装 ─────────────────────────────────────────


def _get_tenant_access_token() -> str:
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=30,
    )
    data = resp.json()
    return data.get("tenant_access_token", "")


def _send_message_sync(receive_id: str, msg_type: str, content: dict, receive_id_type: str = "open_id") -> dict:
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] send_message failed: {data}")
    return data


def _build_card(text: str) -> dict:
    """飞书 interactive 卡片（可被 PATCH 更新）。
    使用 markdown 组件渲染 GFM 表格 / 列表 / 代码块 / 加粗 / 链接（需飞书 7.6+）。
    """
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": [
            {
                "tag": "markdown",
                "content": text or " ",
            }
        ],
    }


def _send_card_sync(receive_id: str, text: str, receive_id_type: str = "open_id") -> dict:
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(_build_card(text), ensure_ascii=False),
    }
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] send_card failed: {data}")
    return data


def _update_card_sync(message_id: str, text: str) -> dict:
    """PATCH 更新 interactive 卡片。只有 interactive 消息能被更新。"""
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"content": json.dumps(_build_card(text), ensure_ascii=False)}
    resp = httpx.patch(
        f"{FEISHU_BASE_URL}/im/v1/messages/{message_id}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] update_card failed: {data}")
    return data


def _download_resource_sync(message_id: str, file_key: str, rtype: str, save_path: str) -> bool:
    """下载消息里的图片 / 文件（语音）。rtype ∈ {'image', 'file'}。"""
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(
        f"{FEISHU_BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": rtype},
        headers=headers,
        timeout=60,
    )
    if resp.status_code == 200:
        with open(save_path, "wb") as f:
            f.write(resp.content)
        return True
    print(f"[WARN] download resource failed: status={resp.status_code} body={resp.text[:300]}")
    return False


# ── 流式输出推送 ──────────────────────────────────────────────

# 飞书卡片硬限 30 KB（含 JSON 包装+样式标签），扣 5 KB 余量做实际预算
_CARD_BUDGET_BYTES = 25000


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_by_bytes(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节预算截断字符串，结尾加省略号。"""
    if max_bytes <= 0:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut + "…"


class FeishuStreamerV1:
    """旧版（v1）—— 单变量 status 覆盖、2 秒节流。保留作 fallback。"""

    MAX_LEN = 3500
    THROTTLE = 2.0

    def __init__(self, receive_id: str, message_id: str):
        self.receive_id = receive_id
        self.message_id = message_id
        self.text = ""
        self.status = ""
        self.last_edit = 0.0
        self.has_content = False
        self.partial_mode = False

    def _compose(self) -> str:
        parts = []
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
            await asyncio.to_thread(_update_card_sync, self.message_id, text)
        except Exception:
            pass
        self.last_edit = time.monotonic()

    async def _schedule(self):
        elapsed = time.monotonic() - self.last_edit
        if elapsed >= self.THROTTLE:
            await self._do_edit()

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        budget = self.MAX_LEN - len(self.status) - 32
        if len(self.text) + len(chunk) > budget:
            await self._do_edit()
            try:
                result = await asyncio.to_thread(
                    _send_card_sync, self.receive_id, "⏳ 继续..."
                )
                self.message_id = result.get("data", {}).get("message_id", self.message_id)
            except Exception:
                pass
            self.text = ""
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

    async def finalize(self, fallback: str = ""):
        self.status = ""
        if not self.has_content:
            text = (fallback or "").strip()
            if not text:
                await asyncio.to_thread(
                    _update_card_sync, self.message_id, "✅ 完成（无文字输出）"
                )
                return
            await asyncio.to_thread(
                _update_card_sync, self.message_id, text[:self.MAX_LEN]
            )
            for i in range(self.MAX_LEN, len(text), MAX_MSG_LEN):
                await asyncio.to_thread(
                    _send_card_sync, self.receive_id, text[i:i + MAX_MSG_LEN]
                )
            return
        await self._do_edit()


class FeishuStreamerV2:
    """新版（v2）—— 步骤历史化 + 0.25s 节流 + 25KB 字节预算自适应折叠。

    卡片布局：
        📋 已完成 N 步
        ✅ Read package.json
        ✅ Edit src/foo.ts
        ✅ Bash(npm test)

        💬 <Claude 的文字输出>

        🔄 当前：Bash(npm install)
    """

    BUDGET = _CARD_BUDGET_BYTES
    THROTTLE = 0.25  # 飞书单条消息更新硬限 5 QPS（200ms），留 50ms 余量

    _HB_INTERVAL = 3.0           # 心跳：静默超过该秒数就刷新一次状态，避免「卡死」观感
    _HB_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _HB_MAX = 240               # 心跳最多存活 240*3s=12min，防孤儿任务长刷死卡

    def __init__(self, receive_id: str, message_id: str):
        self.receive_id = receive_id
        self.message_id = message_id
        self.text = ""
        self.steps: list[str] = []
        self.current_status: str = ""
        self.last_edit = 0.0
        self.has_content = False
        self.partial_mode = False
        self._dirty = False
        self._pending_task: asyncio.Task | None = None
        self._thinking = ""
        self._finalized = False
        self._hb_task: asyncio.Task | None = None
        self._hb_frame = 0
        self._phase_start = 0.0

    @staticmethod
    def _is_archivable(status: str) -> bool:
        # 思考类（💭）不归档，工具类（🔧）等其他状态归档
        return bool(status) and not status.startswith("💭")

    @staticmethod
    def _strip_tool_prefix(s: str) -> str:
        # set_status 传入的工具名形如 "🔧 Read(foo.ts)"，归档时去掉前缀（步骤区用 ✅ 代替）
        return s.removeprefix("🔧 ").strip()

    def _render_steps(self, skip: int = 0) -> str:
        if not self.steps:
            return ""
        n = len(self.steps)
        skip = max(0, min(skip, n))
        kept = self.steps[skip:]
        lines = [f"**📋 已完成 {n} 步**", ""]
        if skip > 0:
            lines.append(f"_…（前 {skip} 步已折叠）_")
        for s in kept:
            lines.append(f"- ✅ {self._strip_tool_prefix(s)}")
        return "\n".join(lines)

    def _assemble(self, steps_part: str, text_part: str, status_part: str) -> str:
        sections = []
        if steps_part:
            sections.append(steps_part)
        if text_part:
            sections.append(text_part if text_part.startswith("💬") else f"💬 {text_part}")
        if status_part:
            sections.append(status_part)
        return "\n\n".join(sections) if sections else "⏳ 处理中..."

    def _status_line(self) -> str:
        """渲染状态行：未结束时带 spinner + 已耗时，保证视觉上「一直在动」。"""
        if self._finalized:
            return ""
        # 正文正在流式输出且无显式状态时，不额外加状态行（正文本身在动）
        if not self.current_status and self.has_content:
            return ""
        spin = self._HB_FRAMES[self._hb_frame % len(self._HB_FRAMES)]
        label = self.current_status or "⏳ 处理中"
        elapsed = ""
        if self._phase_start:
            secs = int(time.monotonic() - self._phase_start)
            if secs >= 2:
                elapsed = f"（{secs}s）"
        return f"**{spin} {label}{elapsed}**"

    def _compose(self) -> str:
        text_part = self.text or ""
        status_part = self._status_line()

        full = self._assemble(self._render_steps(), text_part, status_part)
        if _byte_len(full) <= self.BUDGET:
            return full

        # 折叠步骤（每次丢更多前缀）
        n = len(self.steps)
        for ratio in (4, 2, 4 / 3, 1):
            skip = max(1, int(n / ratio))
            full = self._assemble(self._render_steps(skip=skip), text_part, status_part)
            if _byte_len(full) <= self.BUDGET:
                return full

        # 全折掉步骤还超 → 截短 text
        steps_part = self._render_steps(skip=n)
        fixed = self._assemble(steps_part, "", status_part)
        remaining = self.BUDGET - _byte_len(fixed) - 64
        text_part = _truncate_by_bytes(text_part, max(0, remaining))
        return self._assemble(steps_part, text_part, status_part)

    async def _do_edit(self):
        self._dirty = False
        text = self._compose()
        try:
            await asyncio.to_thread(_update_card_sync, self.message_id, text)
        except Exception:
            pass
        self.last_edit = time.monotonic()

    async def _delayed_edit(self, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._dirty:
            await self._do_edit()

    async def _schedule(self):
        # 关键：绝不在调用方(读取循环)里 await 飞书 PATCH，否则飞书一慢就把
        # claude 输出管道堵死、claude 被迫阻塞写入 → 表现为「思考中卡死」。
        # 一律丢到后台 task 异步刷卡片，读取循环只设脏标记立即返回，永不背压。
        self._dirty = True
        if self._pending_task and not self._pending_task.done():
            return
        elapsed = time.monotonic() - self.last_edit
        delay = 0 if elapsed >= self.THROTTLE else (self.THROTTLE - elapsed)
        self._pending_task = asyncio.create_task(self._delayed_edit(delay))

    def _ensure_heartbeat(self):
        """惰性启动心跳。任何静默期（扩展思考 / 长工具执行）持续刷新状态行，
        卡片永远在动，根治「没事件就卡死」的观感。"""
        if self._hb_task is None and not self._finalized:
            try:
                self._hb_task = asyncio.create_task(self._heartbeat())
            except RuntimeError:
                self._hb_task = None

    async def _heartbeat(self):
        try:
            for _ in range(self._HB_MAX):
                await asyncio.sleep(self._HB_INTERVAL)
                if self._finalized:
                    return
                # 仅在确实静默（最近一次编辑已过去 ~1 个心跳周期）时补刷
                if time.monotonic() - self.last_edit >= self._HB_INTERVAL - 0.3:
                    self._hb_frame += 1
                    self._dirty = True
                    await self._do_edit()
        except asyncio.CancelledError:
            return

    def _mark_phase(self):
        """进入一个新「阶段」（思考/工具/输出）时重置耗时计时。"""
        self._phase_start = time.monotonic()

    async def add_thinking(self, chunk: str):
        """扩展思考增量：把最新思考片段滚进状态行（不进正文、不归档）。"""
        if not chunk:
            return
        if not self._thinking:
            self._mark_phase()
        self._thinking += chunk
        tail = self._thinking[-70:].replace("\n", " ").strip()
        self.current_status = f"💭 {tail}"
        self._ensure_heartbeat()
        await self._schedule()

    async def set_thinking_progress(self, tokens: int):
        """思考内容被中转脱敏（空串）时，用 token 计数做实时进度，
        不重置阶段计时，让深度思考看起来一直在动而不是假死。"""
        if self._thinking:
            return  # 已有真思考文本在滚，token 计数让位
        if not self._phase_start:
            self._mark_phase()
        self.current_status = f"💭 思考中 ~{tokens} tokens"
        self._ensure_heartbeat()
        await self._schedule()

    async def _rollover(self):
        """当前卡片快撑满 25KB 前，先把它定格为完整内容，再开一张新卡片继续。

        修复 V2 回归：旧逻辑超预算直接从尾部截断正文，导致卡片"冻住/截断"。
        这里改成像 V1 一样滚动到新卡，长回答跨多张卡片，内容永不丢。
        """
        # 当前卡此刻仍在预算内，_do_edit 会完整渲染、不截断
        await self._do_edit()
        try:
            result = await asyncio.to_thread(
                _send_card_sync, self.receive_id, "⏳ 接上条继续…"
            )
            new_id = result.get("data", {}).get("message_id", "")
            if new_id:
                self.message_id = new_id
        except Exception:
            pass
        # 新卡从干净状态开始（步骤已在上一张卡体现）
        self.text = ""
        self.steps = []
        self.current_status = ""
        self.last_edit = 0.0
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        # 预判：加上这段后整卡是否会超预算；会则先滚动到新卡片，避免截断/冻结
        if self.text and _byte_len(self._compose()) + _byte_len(chunk) > self.BUDGET:
            await self._rollover()
        self.text += chunk
        self._thinking = ""        # 出现正文＝思考阶段结束
        self._ensure_heartbeat()
        await self._schedule()

    async def set_status(self, line: str):
        if line == self.current_status:
            return
        if self._is_archivable(self.current_status):
            self.steps.append(self.current_status)
        self.current_status = line
        self._thinking = ""
        self._mark_phase()
        self._ensure_heartbeat()
        await self._schedule()

    async def clear_status(self):
        if not self.current_status:
            return
        if self._is_archivable(self.current_status):
            self.steps.append(self.current_status)
        self.current_status = ""
        self._thinking = ""
        await self._schedule()

    async def finalize(self, fallback: str = ""):
        self._finalized = True
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        if self._is_archivable(self.current_status):
            self.steps.append(self.current_status)
        self.current_status = ""
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

        if not self.has_content:
            text = (fallback or "").strip() or "✅ 完成（无文字输出）"
            self.text = text
        await self._do_edit()


def _make_streamer(receive_id: str, message_id: str):
    """根据 FEISHU_STREAM_MODE 环境变量选择 streamer 实现。

    取值：
      - v2（默认）：步骤历史化 + 0.25s 节流 + 25KB 字节预算
      - v1       ：旧实现（单变量覆盖 + 2s 节流），出问题回滚用
    """
    mode = os.environ.get("FEISHU_STREAM_MODE", "v2").strip().lower()
    if mode == "v1":
        return FeishuStreamerV1(receive_id, message_id)
    return FeishuStreamerV2(receive_id, message_id)


# 兼容老代码引用
FeishuStreamer = FeishuStreamerV2


# ── 会话管理 ─────────────────────────────────────────────────


# 仓库内自带的发文件工具（路径相对本文件推导，不写死任何用户绝对路径）。
_SEND_FILE_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "tools", "feishu-send-file.py")
# 注入 claude system prompt：让它知道有这个工具，用户要发文件时主动调用——
# 客户零配置，无需自己写 CLAUDE.md。
_FEISHU_SEND_PROMPT = (
    "你运行在飞书机器人网关里，正在和用户在飞书聊天窗对话。\n"
    "当用户要求把某个本地文件 / 图片 / 视频发到飞书（例如「发我」「发到聊天框」"
    "「把这个视频发我手机」「发我手机上」），直接执行：\n"
    f"    python3 {_SEND_FILE_TOOL} <文件绝对路径>\n"
    "不传收件人时，工具会自动发到「当前对话窗口」（网关已注入发起人 open_id 与当前 bot 凭证）。\n"
    "图片→image、视频→media、其它→file；视频超 28MB 会自动压缩。仅在用户明确要发文件时才调用。"
)


def _get_session(open_id: str) -> ClaudeSession:
    if open_id not in _sessions:
        _sessions[open_id] = ClaudeSession()
    sess = _sessions[open_id]
    # 把「当前会话发起人」身份 + 当前 bot 凭证注入 claude 子进程环境，
    # 供仓库内 tools/feishu-send-file.py 实现「发文件到当前飞书聊天窗」。
    # 一个 session 固定服务一个 open_id，每次取用时刷一遍幂等，开销可忽略。
    if open_id:
        sess.extra_env["FEISHU_SENDER_OPEN_ID"] = open_id
        sess.extra_env["FEISHU_SENDER_ID_TYPE"] = "open_id"
        if FEISHU_APP_ID:
            sess.extra_env["FEISHU_APP_ID"] = FEISHU_APP_ID
        if FEISHU_APP_SECRET:
            sess.extra_env["FEISHU_APP_SECRET"] = FEISHU_APP_SECRET
    if os.path.isfile(_SEND_FILE_TOOL):
        sess.extra_append_prompt = _FEISHU_SEND_PROMPT
    return sess


# 全局后台 event loop：所有协程共用，避免 session 内 asyncio 对象（subprocess、
# Future、StreamReader）在不同消息之间跨 loop 漂移导致
# "got Future <Future pending> attached to a different loop"。
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()


def _start_background_loop():
    """启动后台线程跑 forever loop。幂等，重复调用安全。"""
    global _loop, _loop_thread

    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return

        _loop_ready.clear()

        def _runner():
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop_ready.set()
            try:
                _loop.run_forever()
            finally:
                _loop.close()

        _loop_thread = threading.Thread(
            target=_runner, daemon=True, name="feishu-bg-loop"
        )
        _loop_thread.start()
        _loop_ready.wait()


def _run_async(coro):
    """把协程投递到全局后台 loop，阻塞等待结果。

    所有消息处理线程共用同一个 loop，session 内部的 asyncio 对象
    （subprocess、Future、Lock 等）始终绑定在这个 loop 上，跨消息复用安全。
    """
    if _loop is None or not _loop.is_running():
        _start_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


# ── 命令处理 ─────────────────────────────────────────────────


_CWD_LIST_LIMIT = 30


def _list_subdirs(cwd: str) -> list[str]:
    try:
        names = [
            n for n in os.listdir(cwd)
            if not n.startswith(".") and os.path.isdir(os.path.join(cwd, n))
        ]
    except Exception:
        return []
    names.sort(key=str.lower)
    return names


def _render_cwd_view(session) -> str:
    subdirs = _list_subdirs(session.cwd)
    session.last_cwd_listing = subdirs[:_CWD_LIST_LIMIT]

    parent = os.path.dirname(session.cwd.rstrip("/"))
    lines = [f"📁 当前目录\n{session.cwd}"]
    if parent and parent != session.cwd:
        lines.append(f"\n上级：{parent}（/cwd ..）")

    if not subdirs:
        lines.append("\n（无子目录）")
    else:
        lines.append("")
        for i, name in enumerate(session.last_cwd_listing, 1):
            lines.append(f"{i}. {name}/")
        if len(subdirs) > _CWD_LIST_LIMIT:
            lines.append(f"…还有 {len(subdirs) - _CWD_LIST_LIMIT} 个未显示，请用路径指定")

    lines.append("\n用法：/cwd <编号> / /cwd .. / /cwd <路径>")
    return "\n".join(lines)


def _resolve_cwd_arg(session, arg: str) -> str | None:
    if not arg:
        return None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(session.last_cwd_listing):
            return os.path.join(session.cwd, session.last_cwd_listing[idx])
        return None
    if arg.startswith("~"):
        return os.path.abspath(os.path.expanduser(arg))
    if os.path.isabs(arg):
        return os.path.abspath(arg)
    # 相对路径（含 ..）→ 基于 session.cwd
    return os.path.abspath(os.path.join(session.cwd, arg))


def _handle_command(sender: str, text: str) -> bool:
    text = text.strip()
    if text.startswith("/new"):
        session = _get_session(sender)
        session.set_new_session()
        _send_message_sync(sender, "text", {"text": "🔄 下一条消息将开启全新对话"})
        return True
    elif text.startswith("/sessions"):
        session = _get_session(sender)
        sessions = list_sessions(10, cwd=session.cwd)
        if not sessions:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 没有找到历史会话\n📁 {session.cwd}"},
            )
            return True
        lines = ["📋 最近会话（/resume <编号> 或 /resume <sessionId>）\n"]
        for i, s in enumerate(sessions, 1):
            t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
            summary = s["summary"] or "（无文字内容）"
            lines.append(
                f"{i}. [{t}] {summary}\n"
                f"    📁 {s['proj']}\n"
                f"    🔖 {s['id']}"
            )
        _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/resume"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": "用法：/resume <编号> 或 /resume <sessionId>\n先用 /sessions 查看"},
            )
            return True
        arg = parts[1].strip()
        session = _get_session(sender)
        if arg.isdigit():
            idx = int(arg) - 1
            sessions = list_sessions(10, cwd=session.cwd)
            if idx < 0 or idx >= len(sessions):
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 编号超出范围，共 {len(sessions)} 个会话"},
                )
                return True
            s = sessions[idx]
            session.set_resume_session(s["id"])
            t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
            summary = s["summary"] or "（无文字内容）"
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已切换到会话 {idx+1}\n[{t}] {summary}\n🔖 {s['id']}\n\n发消息继续这个会话吧"},
            )
        elif _session_file_exists(arg):
            session.set_resume_session(arg)
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已切换到会话\n🔖 {arg}\n\n发消息继续这个会话吧"},
            )
        else:
            # 按标题/summary 匹配（在更大的池子里找）
            pool = list_sessions(100, cwd=session.cwd)
            matches = [s for s in pool if (s["summary"] or "").strip() == arg]
            if not matches:
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 未找到 session：{arg}\n可输入编号、sessionId 或 /sessions 里的标题"},
                )
            elif len(matches) == 1:
                s = matches[0]
                session.set_resume_session(s["id"])
                t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                _send_message_sync(
                    sender, "text",
                    {"text": f"✅ 已切换到会话\n[{t}] {s['summary']}\n🔖 {s['id']}\n\n发消息继续这个会话吧"},
                )
            else:
                lines = [f"⚠️ 标题「{arg}」匹配到 {len(matches)} 个，请用 sessionId 明确指定："]
                for s in matches[:5]:
                    t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                    lines.append(f"[{t}] 🔖 {s['id']}")
                _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/rename"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": "用法：\n/rename <新名称>           # 重命名当前会话\n/rename <sessionId> <新名称>  # 重命名指定会话"},
            )
            return True
        session = _get_session(sender)
        if len(parts) == 2:
            # /rename <新名称>
            title = parts[1].strip()
            sid = session.current_session_id
            if not sid:
                _send_message_sync(
                    sender, "text",
                    {"text": "❌ 当前无活跃会话，先发一条消息或用 /resume 切换，再 /rename"},
                )
                return True
        else:
            # /rename <sessionId> <新名称>
            sid = parts[1].strip()
            title = parts[2].strip()
            if not _session_file_exists(sid):
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 未找到 session：{sid}"},
                )
                return True
        if not title:
            _send_message_sync(sender, "text", {"text": "❌ 名称不能为空"})
            return True
        if set_session_title(sid, title):
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已重命名\n🔖 {sid}\n📝 {title}"},
            )
        else:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 重命名失败（未找到 session 文件）：{sid}"},
            )
        return True
    elif text.startswith("/cwd"):
        parts = text.split(maxsplit=1)
        session = _get_session(sender)
        if len(parts) < 2:
            _send_message_sync(sender, "text", {"text": _render_cwd_view(session)})
            return True

        arg = parts[1].strip()
        target = _resolve_cwd_arg(session, arg)
        if target is None:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 无法解析：{arg}\n/cwd 查看当前目录和可选子目录"},
            )
            return True
        if not os.path.isdir(target):
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 目录不存在：{target}"},
            )
            return True

        session.set_cwd(target)
        if session.current_session_id:
            pool = list_sessions(1, cwd=target)
            summary = pool[0]["summary"] if pool else ""
            tip = (
                f"已自动接续最近会话\n🔖 {session.current_session_id}"
                + (f"\n📝 {summary}" if summary else "")
            )
        else:
            tip = "该目录暂无历史会话，下一条消息将开启全新会话"

        _send_message_sync(
            sender, "text",
            {"text": f"✅ 已切换工作目录\n{tip}\n\n{_render_cwd_view(session)}"},
        )
        return True
    elif text.startswith("/stop"):
        session = _get_session(sender)
        # stop 是 async，在后台跑
        async def _do_stop():
            killed = await session.stop()
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": "🛑 已中断当前任务" if killed else "ℹ️ 当前没有运行中的任务"},
            )
        _run_async(_do_stop())
        return True
    elif text.startswith("/status"):
        session = _get_session(sender)
        running = session.current_proc is not None and session.current_proc.returncode is None
        lines = [
            "📊 当前状态",
            f"📁 目录：{session.cwd}",
            f"🤖 模型：{session.model or '默认'}",
            f"🔐 模式：{session.mode}",
            f"🔖 会话：{session.current_session_id or '（新会话）'}",
            f"⚙️ 任务：{'运行中' if running else '空闲'}",
        ]
        _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/model"):
        parts = text.split(maxsplit=1)
        session = _get_session(sender)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": f"🤖 当前模型：{session.model or '默认'}\n\n"
                         f"切换：/model opus|sonnet|haiku|<完整ID>\n"
                         f"重置：/model default"},
            )
            return True
        name = parts[1].strip()
        if name.lower() in ("default", "reset", "clear", "清除", "默认"):
            session.set_model(None)
            _send_message_sync(sender, "text", {"text": "✅ 已重置为 Claude CLI 默认模型"})
        else:
            session.set_model(name)
            _send_message_sync(sender, "text", {"text": f"✅ 已切换模型：{name}\n下一条消息生效"})
        return True
    elif text.startswith("/mode"):
        parts = text.split(maxsplit=1)
        session = _get_session(sender)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": f"🔐 当前权限模式：{session.mode}\n\n"
                         f"可选：bypass（跳过所有确认）/ plan（只规划不执行）/ default（每次确认）/ accept（自动接受文件编辑）\n"
                         f"切换：/mode <名称>"},
            )
            return True
        name = parts[1].strip().lower()
        if session.set_mode(name):
            _send_message_sync(sender, "text", {"text": f"✅ 已切换权限模式：{name}\n下一条消息生效"})
        else:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 未知模式：{name}\n可选：bypass / plan / default / accept"},
            )
        return True
    elif text.startswith("/agents"):
        session = _get_session(sender)
        parts = text.split(maxsplit=1)
        keyword = parts[1].strip().lower() if len(parts) > 1 else ""
        agents = list_agents(cwd=session.cwd)
        if keyword:
            agents = [a for a in agents if keyword in a["name"].lower() or keyword in a["description"].lower()]
        if not agents:
            _send_message_sync(
                sender, "text",
                {"text": "❌ 未找到可用 agent\n" + (f"关键词：{keyword}" if keyword else f"已扫描：~/.claude/agents 和 {session.cwd}/.claude/agents")},
            )
            return True
        lines = [f"🤖 可用 Agent ({len(agents)} 个)" + (f" · 关键词「{keyword}」" if keyword else "")]
        for a in agents:
            icon = "📂" if a["scope"] == "project" else "🌍"
            model_tag = f" [{a['model']}]" if a.get("model") else ""
            desc = a["description"][:80]
            if len(a["description"]) > 80:
                desc += "…"
            lines.append(f"\n{icon} {a['name']}{model_tag}\n   {desc}")
        lines.append("\n调用：/agent <name> <任务描述>")
        _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/agent"):
        session = _get_session(sender)
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": "用法：\n/agent <name> — 查看 agent 详情\n/agent <name> <任务描述> — 调用 agent 执行任务\n\n先用 /agents 查看可用列表"},
            )
            return True
        name = parts[1].strip()
        agents = list_agents(cwd=session.cwd)
        match = next((a for a in agents if a["name"] == name), None)
        if not match:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 未找到 agent：{name}\n用 /agents 查看列表"},
            )
            return True
        if len(parts) < 3:
            icon = "📂 项目" if match["scope"] == "project" else "🌍 全局"
            model_tag = f"\n🧠 模型：{match['model']}" if match.get("model") else ""
            _send_message_sync(
                sender, "text",
                {"text": f"🤖 {match['name']}\n{icon}{model_tag}\n\n{match['description']}\n\n调用：/agent {match['name']} <任务描述>"},
            )
            return True
        task_desc = parts[2].strip()
        # 把指令改写成让 Claude 主循环通过 Task 工具派发给该 subagent
        rewritten = (
            f'请调用 subagent "{match["name"]}" 完成以下任务，并把它的结果原样返回：\n\n'
            f'{task_desc}'
        )
        _run_async(_handle_text_message(sender, rewritten))
        return True
    elif text.startswith("/start"):
        session = _get_session(sender)
        _send_message_sync(
            sender,
            "text",
            {
                "text": (
                    "🤖 Claude Code Gateway 已就绪\n\n"
                    f"📁 目录：{session.cwd}\n"
                    f"🤖 模型：{session.model or '默认'}\n"
                    f"🔐 模式：{session.mode}\n\n"
                    "支持：文字 / 图片 / 语音\n\n"
                    "会话：\n"
                    "/sessions — 查看历史会话\n"
                    "/resume <编号|sessionId|标题> — 切换会话\n"
                    "/rename [<sessionId>] <新名称> — 重命名\n"
                    "/new — 开启全新会话\n"
                    "/status — 查看当前状态\n\n"
                    "运行：\n"
                    "/stop — 中断当前任务（新消息会自动中断）\n"
                    "/model [opus|sonnet|haiku|default] — 切换模型\n"
                    "/mode [bypass|plan|default|accept] — 切换权限模式\n\n"
                    "目录：\n"
                    "/cwd — 查看当前目录 + 子目录列表\n"
                    "/cwd <编号|..|路径> — 切换目录\n\n"
                    "Agent：\n"
                    "/agents [关键词] — 列出可用 subagent\n"
                    "/agent <name> — 查看 agent 详情\n"
                    "/agent <name> <任务> — 调用 agent 执行\n\n"
                    "其他未注册的 /xxx 会直接透传给 Claude CLI\n"
                    "（如 /commit、/review 等官方 skill）"
                )
            },
        )
        return True
    return False


# ── 消息处理 ─────────────────────────────────────────────────


async def _handle_text_message(sender: str, text: str):
    if not text or not sender:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "⏳ 处理中...")
    reply_id = result.get("data", {}).get("message_id", "")

    session = _get_session(sender)
    streamer = _make_streamer(sender, reply_id) if reply_id else None

    try:
        response = await session.run_claude(text, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


async def _handle_image_message(sender: str, message_id: str, file_key: str, caption: str = ""):
    if not file_key or not sender or not message_id:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "🖼️ 处理图片中...")
    reply_id = result.get("data", {}).get("message_id", "")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    downloaded = await asyncio.to_thread(
        _download_resource_sync, message_id, file_key, "image", tmp_path
    )
    if not downloaded:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 图片下载失败")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": "❌ 图片下载失败"}
            )
        return

    session = _get_session(sender)
    streamer = _make_streamer(sender, reply_id) if reply_id else None

    prompt = caption.strip() or "请描述这张图片的内容"
    response = ""
    try:
        response = await session.run_claude_with_image(tmp_path, prompt, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


async def _handle_file_message(sender: str, message_id: str, file_key: str, file_name: str = ""):
    if not file_key or not sender or not message_id:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "📎 处理文件中...")
    reply_id = result.get("data", {}).get("message_id", "")

    suffix = os.path.splitext(file_name)[1] if file_name else ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name

    # 如果有原始文件名，重命名临时文件让 Claude 能看到扩展名和名称
    if file_name:
        named_path = os.path.join(os.path.dirname(tmp_path), file_name)
        os.rename(tmp_path, named_path)
        tmp_path = named_path

    downloaded = await asyncio.to_thread(
        _download_resource_sync, message_id, file_key, "file", tmp_path
    )
    if not downloaded:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 文件下载失败")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return

    session = _get_session(sender)
    streamer = _make_streamer(sender, reply_id) if reply_id else None

    display_name = file_name or os.path.basename(tmp_path)
    prompt = f"用户通过飞书发送了文件：{display_name}\n文件已保存到本地：{tmp_path}\n\n请根据文件内容和类型做出合适的处理（如读取、分析、总结等）。"

    try:
        response = await session.run_claude(prompt, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


async def _handle_audio_message(sender: str, message_id: str, file_key: str):
    if not file_key or not sender or not message_id:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "🎙️ 识别语音中...")
    reply_id = result.get("data", {}).get("message_id", "")

    # 飞书语音默认是 opus
    with tempfile.NamedTemporaryFile(suffix=".opus", delete=False) as tmp:
        tmp_path = tmp.name

    downloaded = await asyncio.to_thread(
        _download_resource_sync, message_id, file_key, "file", tmp_path
    )
    if not downloaded:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 语音下载失败")
        return

    try:
        text = await asyncio.to_thread(transcribe_audio, tmp_path)
    except Exception as e:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, f"❌ 语音识别失败：{e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if not text:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 语音识别为空，请重试")
        return

    print(f"\n🎙️  语音识别：{text}")
    if reply_id:
        await asyncio.to_thread(_update_card_sync, reply_id, f"🎙️ 已识别：{text}\n\n⏳ 处理中...")

    session = _get_session(sender)
    streamer = _make_streamer(sender, reply_id) if reply_id else None
    # 让 streamer 的首次 append 不再被 partial_mode 吞；同时让头部继续显示识别结果
    if streamer:
        streamer.text = f"🎙️ 已识别：{text}\n\n"

    try:
        response = await session.run_claude(text, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


def _process_message_event(event: P2ImMessageReceiveV1):
    """在后台线程中处理消息事件（避免阻塞 WebSocket 读取循环）。"""
    try:
        data = event.event
        message = data.message
        msg_type = message.message_type
        message_id = message.message_id or ""
        sender_id = data.sender.sender_id.open_id if data.sender and data.sender.sender_id else ""
        content = json.loads(message.content) if message.content else {}

        print(f"[DEBUG] 收到消息: msg_type={msg_type}, sender_id={sender_id}, message_id={message_id}, content={content}")

        if msg_type == "text":
            text = content.get("text", "").strip()
            if text.startswith("/"):
                if _handle_command(sender_id, text):
                    return
            _run_async(_handle_text_message(sender_id, text))
        elif msg_type == "image":
            file_key = content.get("image_key", "")
            _run_async(_handle_image_message(sender_id, message_id, file_key))
        elif msg_type == "audio":
            file_key = content.get("file_key", "")
            _run_async(_handle_audio_message(sender_id, message_id, file_key))
        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "")
            _run_async(_handle_file_message(sender_id, message_id, file_key, file_name))
        else:
            print(f"[DEBUG] 暂不支持的消息类型: {msg_type}")
    except Exception as e:
        print(f"❌ 处理消息事件出错：{e}")
        traceback.print_exc()


def _handle_message_event(event: P2ImMessageReceiveV1):
    """长连接消息事件回调 — 只做解析，实际处理放到后台线程。"""
    threading.Thread(
        target=_process_message_event,
        args=(event,),
        daemon=True,
    ).start()


# ── 主入口 ───────────────────────────────────────────────────


def main():
    print("🤖 Claude Code Feishu Gateway 启动中（长连接模式）...")
    print(f"   App ID: {FEISHU_APP_ID[:8]}...")

    # 提前启动后台 loop，确保第一条消息进来时 loop 已就绪
    _start_background_loop()
    print("   后台 event loop 已就绪")
    print("   等待飞书消息...\n")

    handler = (
        EventDispatcherHandler.builder(
            encrypt_key="",
            verification_token=FEISHU_VERIFY_TOKEN or "",
        )
        .register_p2_im_message_receive_v1(_handle_message_event)
        .build()
    )

    client = WsClient(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=LogLevel.INFO,
    )
    client.start()


if __name__ == "__main__":
    main()
