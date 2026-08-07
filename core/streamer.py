"""StreamerBase —— claude_core 的 streamer 协议抽象基类。

claude_core.ClaudeSession 在执行时回调 streamer:append(文本增量)/set_status(工具·思考)/
clear_status/finalize,可选 add_thinking/set_thinking_progress,并读 has_content/partial_mode。

通用的状态管理、步骤归档、节流调度在此;平台"实际发送"由子类实现 _send()。
平台特定优化(飞书 25KB 折叠/rollover、企微全量替换/6min 降级、TG edit_text/RetryAfter)
在各 adapter 的 streamer 里覆盖 _render / finalize。
"""
import asyncio
import time
from abc import ABC, abstractmethod


class StreamerBase(ABC):
    THROTTLE = 1.0          # 节流秒(子类按平台限频覆盖:飞书 0.25 / TG 1.2 / 企微 2.0)
    MAX_STEPS = 8           # _render 默认展示的最近步骤数
    PLACEHOLDER = "⏳ 处理中..."      # 空白态占位;心跳提示会整条替换它而非叠加

    HEARTBEAT = 15          # 心跳间隔秒;设 0 关闭

    def __init__(self):
        self.text = ""
        self.steps: list[str] = []
        self.current_status = ""
        self.has_content = False
        self.partial_mode = False
        self.last_send = 0.0
        self._pending: asyncio.Task | None = None
        self._finalized = False
        self._last_event = time.monotonic()      # 最后一次收到 claude 事件的时刻
        self._hb: asyncio.Task | None = None

    # ---- 子类必须实现:把渲染好的文本发到平台(final=True 为收尾帧)----
    @abstractmethod
    async def _send(self, rendered: str, final: bool) -> None: ...

    # ---- 渲染(子类可覆盖以加字节预算 / 折叠)----
    def _render(self) -> str:
        parts = []
        if self.steps:
            shown = self.steps[-self.MAX_STEPS:]
            parts.append(f"**📋 已完成 {len(self.steps)} 步**\n" +
                         "\n".join(f"- ✅ {s}" for s in shown))
        if self.text:
            parts.append(self.text)
        if self.current_status and not self.has_content:
            parts.append(self.current_status)
        return "\n\n".join(parts) if parts else self.PLACEHOLDER

    def _archive_status(self):
        """思考类(💭)不归档,工具类等归档进步骤历史。"""
        s = self.current_status
        if s and not s.startswith("💭"):
            self.steps.append(s.removeprefix("🔧 ").strip())

    async def _flush(self, final: bool = False):
        rendered = self._render()
        hint = self._idle_hint(final)
        if hint:
            # 空白态整条替换占位,否则叠成「处理中...」+「已等待」两行 ⏳ 很傻
            rendered = hint if rendered == self.PLACEHOLDER else f"{rendered}\n\n{hint}"
        await self._send(rendered, final)
        self.last_send = time.monotonic()

    # ---- 心跳:长工具执行期间流上没有任何事件,卡片会僵在最后一帧看着像死了 ----
    def _idle_hint(self, final: bool) -> str:
        """静默提示由「当前静默了多久」当场推导,不留状态。

        这样任意一次重绘(心跳的 / 事件触发的)都自动带上或抹掉它,
        不会出现「事件早就来了、卡片上还挂着上一轮已等待 Xs」的残留。
        """
        if final or not self.HEARTBEAT:
            return ""
        idle = time.monotonic() - self._last_event
        return f"⏳ 干活中…已等待 {max(1, int(idle))}s" if idle >= self.HEARTBEAT else ""

    def _touch(self) -> bool:
        """收到事件 —— 刷新活跃时间;返回「刚才正挂着等待提示」(调用方需补一帧抹掉它)。"""
        now = time.monotonic()
        was_idle = bool(self.HEARTBEAT) and (now - self._last_event) >= self.HEARTBEAT
        self._last_event = now
        return was_idle

    def start_heartbeat(self):
        if self._hb or not self.HEARTBEAT or self._finalized:
            return

        async def _beat():
            try:
                while not self._finalized:
                    await asyncio.sleep(self.HEARTBEAT)
                    if self._finalized:
                        break
                    if time.monotonic() - self._last_event < self.HEARTBEAT:
                        continue                # 流还在动,不用刷
                    try:
                        await self._flush(False)
                    except Exception:
                        pass                    # 平台抖动不该掀翻整轮对话
            except asyncio.CancelledError:
                return
        self._hb = asyncio.create_task(_beat())

    def _stop_heartbeat(self):
        if self._hb and not self._hb.done():
            self._hb.cancel()
        self._hb = None

    async def _schedule(self):
        """绝不在 claude 读取循环里 await 平台发送 —— 设后台 task 节流刷新,立即返回。"""
        self._touch()
        if self._finalized:
            return
        if self._pending and not self._pending.done():
            return
        elapsed = time.monotonic() - self.last_send
        delay = 0 if elapsed >= self.THROTTLE else (self.THROTTLE - elapsed)

        async def _later():
            try:
                await asyncio.sleep(delay)
                await self._flush(False)
            except asyncio.CancelledError:
                return
        self._pending = asyncio.create_task(_later())

    # ---- claude_core streamer 协议 ----
    async def first_frame(self):
        """首响占位(企微 5s 内必须首发等场景用),顺带起心跳。"""
        self.start_heartbeat()
        await self._flush(False)

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        self.text += chunk
        await self._schedule()

    async def set_status(self, line: str):
        # 提前 return 的分支也要 _touch:重复的同一状态照样证明流还活着。
        # 若刚才正挂着等待提示,还得补一帧把它抹掉,否则卡片上残留过期的「已等待」。
        stale = self._touch()
        if line == self.current_status:
            if stale:
                await self._schedule()
            return
        self._archive_status()
        self.current_status = line
        await self._schedule()

    async def clear_status(self):
        stale = self._touch()
        if not self.current_status:
            if stale:
                await self._schedule()
            return
        self._archive_status()
        self.current_status = ""
        await self._schedule()

    async def add_thinking(self, chunk: str):
        if chunk:
            self.current_status = "💭 " + chunk[-60:].replace("\n", " ").strip()
            await self._schedule()

    async def set_thinking_progress(self, tokens: int):
        if not self.has_content:
            self.current_status = f"💭 思考中 ~{tokens} tokens"
            await self._schedule()

    async def finalize(self, fallback: str = "", override_text: str = ""):
        self._finalized = True
        self._stop_heartbeat()
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._archive_status()
        self.current_status = ""
        if override_text:
            self.text = override_text.strip()
        elif not self.has_content:
            self.text = (fallback or "").strip() or "✅ 完成（无文字输出）"
        await self._flush(True)

    async def discard(self) -> bool:
        """接力已另发独立 @ 消息后,丢弃本条流式过程消息,避免与接力消息重复。
        基类无法真正撤回,退化为收尾一个极短指引;支持撤回的平台(飞书)覆盖本方法。"""
        await self.finalize(override_text="↳ 已 @ 队友接力,内容见下条")
        return False
