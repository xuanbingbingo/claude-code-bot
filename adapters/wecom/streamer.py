"""WecomStreamer —— 企微全量替换式流式(继承 StreamerBase)。
企微 aibot_respond_msg 每帧发完整 content(客户端整体替换)→ 正好契合 _render() 全量输出。
超过安全时限(默认 5min,企微硬限 6min)自动降级:finish 当前流,余下用 aibot_send_msg 主动推。
"""
import time
import uuid

from core.streamer import StreamerBase

BUDGET = 3800


class WecomStreamer(StreamerBase):
    THROTTLE = 2.0          # 企微限频 30 条/分

    def __init__(self, adapter, inbound, stream_safe_sec: int = 300):
        super().__init__()
        self.adapter = adapter
        self.msgid = inbound.raw.get("msgid", "")
        self.req_id = inbound.raw.get("_req_id", "")     # 复用原消息 req_id(否则 846605)
        self.conv_id = inbound.conv_id
        self.chat_type = 2 if inbound.chat_type == "group" else 1
        self.stream_id = "stream_" + uuid.uuid4().hex[:16]
        self._start = time.monotonic()
        self._degraded = False
        self._safe = stream_safe_sec

    def _render(self) -> str:
        r = super()._render()
        return (r[:BUDGET - 1] + "…") if len(r) > BUDGET else r

    async def _send(self, rendered: str, final: bool) -> None:
        # 降级:流式快到 6min 硬限 → finish 当前流,后续走主动推送
        if not self._degraded and (time.monotonic() - self._start) > self._safe:
            self._degraded = True
            await self.adapter.send_stream(self.req_id, self.msgid, self.stream_id,
                                           rendered + "\n\n_（内容较长,完整结果稍后推送）_", True)
            return
        if self._degraded:
            if final:
                full = self.text
                for i in range(0, len(full), BUDGET):
                    await self.adapter.send_active(self.conv_id, self.chat_type, full[i:i + BUDGET])
            return
        await self.adapter.send_stream(self.req_id, self.msgid, self.stream_id, rendered, final)
