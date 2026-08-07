"""StreamerBase 心跳与渲染单测。

心跳存在的理由：claude 跑一个长 Bash 时流上没有任何事件，卡片会僵在最后一帧，
用户看着就是「卡住不输出」。心跳负责在静默期定期刷「已等待 Xs」证明还活着，
并且必须在收到任何事件时立刻撤掉，绝不能污染正文或步骤历史。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.streamer import StreamerBase  # noqa: E402


class FakeStreamer(StreamerBase):
    """记录每一帧发送内容，不碰网络。"""
    THROTTLE = 0
    HEARTBEAT = 0.05

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, bool]] = []

    async def _send(self, rendered: str, final: bool) -> None:
        self.sent.append((rendered, final))

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""


async def _settle(n: float = 0.12):
    """等一会儿，让心跳 / 节流刷新的后台 task 跑到。"""
    await asyncio.sleep(n)


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_beats_while_silent(self):
        s = FakeStreamer()
        await s.first_frame()
        await _settle(0.18)
        assert "已等待" in s.last, f"静默期应出现等待提示，实际:{s.last!r}"
        await s.finalize(fallback="done")

    @pytest.mark.asyncio
    async def test_placeholder_replaced_not_stacked(self):
        # 空白态时心跳应整条替换占位，不能叠成「处理中...」+「已等待」两行
        s = FakeStreamer()
        await s.first_frame()
        await _settle(0.18)
        assert s.last.count("⏳") == 1, f"占位与心跳叠加了:{s.last!r}"
        await s.finalize(fallback="done")

    @pytest.mark.asyncio
    async def test_event_clears_hint(self):
        s = FakeStreamer()
        await s.first_frame()
        await _settle(0.18)
        assert "已等待" in s.last
        await s.append("有输出了")
        await _settle(0.02)          # 等节流刷新那一帧落地
        assert "已等待" not in s.last, "来了新内容还挂着等待提示"
        await s.finalize()

    @pytest.mark.asyncio
    async def test_repeated_status_still_counts_as_alive(self):
        # set_status 收到与当前相同的状态会提前 return，但那同样证明流还活着
        s = FakeStreamer()
        await s.first_frame()
        await s.set_status("🔧 Bash")
        await _settle(0.08)
        await s.set_status("🔧 Bash")          # 重复状态：也要补一帧抹掉过期提示
        await _settle(0.02)
        assert "已等待" not in s.last, "重复状态没能抹掉残留的等待提示"
        await s.finalize()

    @pytest.mark.asyncio
    async def test_hint_never_pollutes_text_or_steps(self):
        s = FakeStreamer()
        await s.first_frame()
        await s.append("正文")
        await _settle(0.18)
        assert "已等待" in s.last                      # 卡片上看得到
        assert "已等待" not in s.text                  # 但不进正文
        assert not any("已等待" in x for x in s.steps)  # 也不进步骤历史
        await s.finalize()

    @pytest.mark.asyncio
    async def test_finalize_stops_beating(self):
        s = FakeStreamer()
        await s.first_frame()
        await s.finalize(fallback="收尾")
        n = len(s.sent)
        await _settle(0.2)
        assert len(s.sent) == n, "finalize 之后心跳还在发帧"
        assert s.sent[-1][1] is True
        assert "已等待" not in s.sent[-1][0], "收尾帧不该带等待提示"

    @pytest.mark.asyncio
    async def test_send_failure_does_not_kill_the_turn(self):
        class Boom(FakeStreamer):
            async def _send(self, rendered, final):
                if not final:
                    raise RuntimeError("飞书抖了一下")
                await super()._send(rendered, final)

        s = Boom()
        try:
            await s.first_frame()
        except RuntimeError:
            pass
        await _settle(0.18)          # 心跳内部抛错必须被吞掉，不能冒泡炸掉整轮
        await s.finalize(fallback="仍然收尾成功")
        assert s.sent and s.sent[-1][1] is True

    @pytest.mark.asyncio
    async def test_disabled_when_interval_zero(self):
        class NoBeat(FakeStreamer):
            HEARTBEAT = 0

        s = NoBeat()
        await s.first_frame()
        n = len(s.sent)
        await _settle(0.2)
        assert len(s.sent) == n
        await s.finalize()
