"""Gateway 同会话并发语义单测 —— 后到的消息「抢占」前一轮,而不是排队或并跑。

为什么必须有这层:同一个 conv_id 共用同一个 ClaudeSession(同一条 claude 会话),
两轮真并发 = 两个 claude 进程往同一个 jsonl 交织写 + current_session_id 互相覆盖。
而抢占又不能静默 —— 被杀的那轮 claude 不会再吐 result 事件,backend.run 返回空串,
卡片会停在半截文字上冒充完整答案。这两点都在下面钉死。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.gateway import Gateway              # noqa: E402
from core.commands import CommandRouter       # noqa: E402
from core.messages import InboundMessage      # noqa: E402
from core.streamer import StreamerBase        # noqa: E402

CONV = "oc_test"


def _msg(text: str) -> InboundMessage:
    return InboundMessage(conv_id=CONV, text=text, platform="fake")


class FakeStreamer(StreamerBase):
    THROTTLE = 0
    HEARTBEAT = 0

    def __init__(self, first_frame_delay: float = 0.0):
        super().__init__()
        self.sent: list[tuple[str, bool]] = []
        self._delay = first_frame_delay

    async def _send(self, rendered: str, final: bool) -> None:
        self.sent.append((rendered, final))

    async def first_frame(self):
        # 真实链路里首帧是一次发卡片的网络往返 —— 抢跑窗口就开在这里
        if self._delay:
            await asyncio.sleep(self._delay)
        await super().first_frame()

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""


class FakeAdapter:
    def __init__(self, first_frame_delay: float = 0.0):
        self.streamers: list[FakeStreamer] = []
        self.texts: list[str] = []
        self._delay = first_frame_delay

    def make_streamer(self, inbound):
        s = FakeStreamer(self._delay)
        self.streamers.append(s)
        return s

    def backend_env(self, inbound):
        return {}

    async def send_text(self, conv_id, chat_type, text):
        self.texts.append(text)


class FakeBackend:
    """模拟 ClaudeSession:被 stop() 就立刻返回空串(claude 被 SIGTERM 后不会再吐 result)。"""

    def __init__(self, work_time: float = 0.3):
        self.work_time = work_time
        self.running = 0
        self.max_concurrent = 0
        self.started: list[str] = []
        self.completed: list[str] = []
        self._stop_evt: asyncio.Event | None = None

    def has(self, cap):
        return True

    def inject_env(self, env):
        pass

    async def run(self, prompt, streamer):
        self.started.append(prompt)
        self.running += 1
        self.max_concurrent = max(self.max_concurrent, self.running)
        stop = asyncio.Event()
        self._stop_evt = stop
        try:
            await streamer.append(f"半截-{prompt}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.work_time)
            except asyncio.TimeoutError:
                self.completed.append(prompt)
                return f"完整-{prompt}"
            return ""                      # 被杀 → 没有 result 事件
        finally:
            self.running -= 1
            self._stop_evt = None

    async def stop(self):
        if self._stop_evt and not self._stop_evt.is_set():
            self._stop_evt.set()
            return True
        return False


class FakeSessions:
    def __init__(self, backend):
        self.backend = backend
        self.persisted: list[str] = []

    def get_or_create(self, conv_id, is_group=False):
        return self.backend

    def persist(self, conv_id):
        self.persisted.append(conv_id)


class FakeRelay:
    async def maybe_relay(self, inbound, resp, adapter):
        return False


def _build(work_time=0.3, first_frame_delay=0.0):
    backend = FakeBackend(work_time)
    adapter = FakeAdapter(first_frame_delay)
    gw = Gateway(adapter, FakeSessions(backend), CommandRouter(), FakeRelay())
    return gw, adapter, backend


class TestPreemption:
    @pytest.mark.asyncio
    async def test_second_message_preempts_first(self):
        """第二条掐掉第一条:两轮不并发,第一条卡片显式标注中断且保留半截内容。"""
        gw, adapter, backend = _build(work_time=0.3)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)                 # 让 Q1 真的跑起来
        await gw.handle(_msg("Q2"))
        await t1

        assert backend.max_concurrent == 1, "同一会话不许两个 claude 进程并跑"
        assert backend.started == ["Q1", "Q2"]
        assert backend.completed == ["Q2"], "只有最后一条该跑完"

        card1 = adapter.streamers[0].last
        assert "半截-Q1" in card1, "被抢占也要留住已产出的内容"
        assert Gateway.PREEMPT_NOTE in card1, "被抢占必须显式标注,不能冒充完整答案"
        # 已有流式内容时 finalize 的 fallback 本就该被忽略(正文以流式为准),
        # 这里只需确认 Q2 那张卡是干净收尾、没有被打上中断标记
        card2 = adapter.streamers[1].last
        assert "半截-Q2" in card2
        assert Gateway.PREEMPT_NOTE not in card2

    @pytest.mark.asyncio
    async def test_preempt_before_spawn(self):
        """抢跑窗口:第一条还卡在发卡片、进程没起来时被抢占 —— 绝不能再 spawn。"""
        gw, adapter, backend = _build(work_time=0.05, first_frame_delay=0.2)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)                 # Q1 还堵在 first_frame 里
        await gw.handle(_msg("Q2"))
        await t1

        assert backend.started == ["Q2"], "没 spawn 的那轮必须自己作废,不许补跑"
        assert backend.max_concurrent == 1
        assert Gateway.PREEMPT_NOTE in adapter.streamers[0].last

    @pytest.mark.asyncio
    async def test_rapid_fire_only_last_runs(self):
        """连点三条:中间那条不许在队列里补跑,只有最后一条算数。"""
        gw, adapter, backend = _build(work_time=0.2)
        tasks = [asyncio.create_task(gw.handle(_msg(f"Q{i}"))) for i in range(1, 4)]
        await asyncio.gather(*tasks)

        assert backend.max_concurrent <= 1
        assert backend.completed == ["Q3"]
        for s in adapter.streamers[:-1]:
            assert Gateway.PREEMPT_NOTE in s.last

    @pytest.mark.asyncio
    async def test_stop_command_marks_card(self):
        """/stop 杀掉在跑的那轮 → 卡片收尾成「已手动中断」,不是空回复。"""
        gw, adapter, backend = _build(work_time=1.0)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)
        await gw.handle(_msg("/stop"))
        await asyncio.wait_for(t1, timeout=1.0)

        assert "🛑 已中断当前任务" in adapter.texts
        card = adapter.streamers[0].last
        assert Gateway.STOP_NOTE in card
        assert "半截-Q1" in card

    @pytest.mark.asyncio
    async def test_command_not_blocked_by_running_turn(self):
        """命令不进锁:长任务跑着时 /status 必须立刻回,不能被排在后面。"""
        gw, adapter, backend = _build(work_time=0.3)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)
        backend.status_lines = lambda: ["📁 目录:/tmp"]
        await asyncio.wait_for(gw.handle(_msg("/status")), timeout=0.1)
        assert any("当前状态" in t for t in adapter.texts)
        await t1

    @pytest.mark.asyncio
    async def test_single_message_unaffected(self):
        """单条消息照常走完,不受抢占逻辑影响。"""
        gw, adapter, backend = _build(work_time=0.05)
        await gw.handle(_msg("Q1"))
        assert backend.completed == ["Q1"]
        card = adapter.streamers[0].last
        assert Gateway.PREEMPT_NOTE not in card
        assert "半截-Q1" in card
