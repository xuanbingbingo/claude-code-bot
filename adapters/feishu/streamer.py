"""FeishuStreamer —— 飞书 interactive 卡片流式(继承 StreamerBase)。
首帧 send_card 拿 message_id,后续 update_card PATCH;25KB 字节预算自适应折叠步骤+截断正文。
节流 0.25s(飞书单消息更新 5 QPS)。同步 httpx 经 asyncio.to_thread 调,不堵 claude 读取循环。
"""
import asyncio

from core.streamer import StreamerBase

BUDGET = 25000


def _blen(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate(s: str, mx: int) -> str:
    if mx <= 0:
        return ""
    e = s.encode("utf-8")
    return s if len(e) <= mx else e[:mx].decode("utf-8", "ignore") + "…"


class FeishuStreamer(StreamerBase):
    THROTTLE = 0.25

    def __init__(self, api, receive_id: str, receive_id_type: str = "open_id", message_id: str = ""):
        super().__init__()
        self.api = api
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.message_id = message_id

    def _assemble(self, skip: int, text: str) -> str:
        parts = []
        if self.steps:
            kept = self.steps[skip:]
            head = f"**📋 已完成 {len(self.steps)} 步**"
            if skip:
                head += f"\n_（前 {skip} 步已折叠）_"
            parts.append(head + "\n" + "\n".join(f"- ✅ {s}" for s in kept))
        if text:
            parts.append(text if text.startswith("💬") else f"💬 {text}")
        if self.current_status and not self.has_content:
            parts.append(self.current_status)
        return "\n\n".join(parts) if parts else "⏳ 处理中..."

    def _render(self) -> str:
        full = self._assemble(0, self.text)
        if _blen(full) <= BUDGET:
            return full
        n = len(self.steps)
        for ratio in (4, 2, 4 / 3, 1):                  # 逐步折叠更多步骤
            full = self._assemble(max(1, int(n / ratio)), self.text)
            if _blen(full) <= BUDGET:
                return full
        base = self._assemble(n, "")                    # 全折叠后截断正文
        room = BUDGET - _blen(base) - 64
        return self._assemble(n, _truncate(self.text, max(0, room)))

    async def _send(self, rendered: str, final: bool) -> None:
        if not self.message_id:
            self.message_id = await asyncio.to_thread(
                self.api.send_card, self.receive_id, rendered, self.receive_id_type)
        else:
            await asyncio.to_thread(self.api.update_card, self.message_id, rendered)

    async def discard(self) -> bool:
        """接力已另发带 <at> 的独立消息 → 撤回本条流式卡片,群里只剩那条能 @ 到人的消息。
        撤回失败(无 id / 接口报错)则退化为收尾提示,绝不把卡片留在流式中间态。"""
        self._finalized = True
        if self._pending and not self._pending.done():
            self._pending.cancel()
        if self.message_id and await asyncio.to_thread(self.api.delete_message, self.message_id):
            return True
        await self.finalize(override_text="↳ 已 @ 队友接力,内容见下条")
        return False
