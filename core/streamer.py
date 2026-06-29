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

    def __init__(self):
        self.text = ""
        self.steps: list[str] = []
        self.current_status = ""
        self.has_content = False
        self.partial_mode = False
        self.last_send = 0.0
        self._pending: asyncio.Task | None = None
        self._finalized = False

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
        return "\n\n".join(parts) if parts else "⏳ 处理中..."

    def _archive_status(self):
        """思考类(💭)不归档,工具类等归档进步骤历史。"""
        s = self.current_status
        if s and not s.startswith("💭"):
            self.steps.append(s.removeprefix("🔧 ").strip())

    async def _flush(self, final: bool = False):
        await self._send(self._render(), final)
        self.last_send = time.monotonic()

    async def _schedule(self):
        """绝不在 claude 读取循环里 await 平台发送 —— 设后台 task 节流刷新,立即返回。"""
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
        """首响占位(企微 5s 内必须首发等场景用)。"""
        await self._flush(False)

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        self.text += chunk
        await self._schedule()

    async def set_status(self, line: str):
        if line == self.current_status:
            return
        self._archive_status()
        self.current_status = line
        await self._schedule()

    async def clear_status(self):
        if not self.current_status:
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
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._archive_status()
        self.current_status = ""
        if override_text:
            self.text = override_text.strip()
        elif not self.has_content:
            self.text = (fallback or "").strip() or "✅ 完成（无文字输出）"
        await self._flush(True)
