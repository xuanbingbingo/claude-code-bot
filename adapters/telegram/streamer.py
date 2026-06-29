"""TelegramStreamer —— edit_text 流式(继承 StreamerBase)。节流 1.2s,4000 字截断。"""
from core.streamer import StreamerBase


class TelegramStreamer(StreamerBase):
    THROTTLE = 1.2

    def __init__(self, message):
        super().__init__()
        self.msg = message

    async def _send(self, rendered: str, final: bool) -> None:
        try:
            await self.msg.edit_text(rendered[:4000])
        except Exception:
            pass
