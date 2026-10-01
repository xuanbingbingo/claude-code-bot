"""长任务不被 30 分钟砍 + 问进度不抢占 —— 两件事一起钉死。

背景:以前单轮跑满 CLAUDE_RUN_TIMEOUT(30 分钟)直接 SIGTERM,连带前台命令一起死;
中途问一句「好了吗」又会走抢占把任务杀掉。现在:
  - 软上限只挂横幅 + 打标记,硬上限(CLAUDE_RUN_HARD_TIMEOUT)才杀;
  - 有任务在跑时,进度询问由网关直接回,不 spawn、不抢占;其它普通消息照旧抢占。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_core as cc                                   # noqa: E402
from core.gateway import Gateway                            # noqa: E402
from core.commands import CommandRouter                     # noqa: E402
from core.progress_query import is_progress_query, progress_reply   # noqa: E402
from tests.test_gateway import (FakeStreamer, FakeAdapter, FakeBackend,  # noqa: E402
                                FakeSessions, FakeRelay, _msg)


# ---------- 进度询问识别 ----------

@pytest.mark.parametrize("text", [
    "好了吗", "好了没？", "好了没有", "弄好了吗", "完成了吗", "跑完了没", "做完了吗",
    "进度呢", "现在什么进度", "看下进度", "怎么样了", "到哪了", "跑到哪一步了",
    "还要多久", "多久能好", "还在跑吗", "在吗", "有结果了吗", "出来了吗",
    "卡住了吗", "那个，好了吗", "请问好了吗？", "好了没呀", " 好了吗 ",
])
def test_progress_query_hits(text):
    assert is_progress_query(text)


@pytest.mark.parametrize("text", [
    "", "好了", "好", "停", "继续", "重新跑一下", "进度条改成红色",
    "好了吗，好了就发我抖音版", "完成后发我", "在吗 帮我看下这个bug",
    "到哪了？顺便改下标题", "还要多久才能部署完然后再帮我发b站",
    "怎么样才能让它更快",
])
def test_progress_query_misses(text):
    assert not is_progress_query(text)


def test_progress_reply_reads_streamer():
    s = FakeStreamer()
    s.steps = ["Bash ls", "Read a.py"]
    s.current_status = "🔧 Bash npx remotion render"
    r = progress_reply(754, s)
    assert "12 分 34 秒" in r and "remotion render" in r and "已完成 2 步" in r
    assert "/stop" in r


# ---------- 网关:问进度不抢占,其它照旧抢占 ----------

def _build(work_time=0.3):
    backend = FakeBackend(work_time)
    adapter = FakeAdapter()
    gw = Gateway(adapter, FakeSessions(backend), CommandRouter(), FakeRelay())
    return gw, adapter, backend


class TestProgressQueryBypass:
    @pytest.mark.asyncio
    async def test_progress_query_does_not_preempt(self):
        gw, adapter, backend = _build(work_time=0.3)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(gw.handle(_msg("好了吗？")), timeout=0.1)
        await t1
        assert backend.started == ["Q1"], "问进度不许 spawn 新一轮"
        assert backend.completed == ["Q1"], "问进度不许把在跑的任务杀掉"
        assert any("还在跑" in t for t in adapter.texts)
        assert Gateway.PREEMPT_NOTE not in adapter.streamers[0].last

    @pytest.mark.asyncio
    async def test_normal_message_still_preempts(self):
        gw, adapter, backend = _build(work_time=0.3)
        t1 = asyncio.create_task(gw.handle(_msg("Q1")))
        await asyncio.sleep(0.05)
        await gw.handle(_msg("改成竖版"))
        await t1
        assert backend.completed == ["改成竖版"]
        assert Gateway.PREEMPT_NOTE in adapter.streamers[0].last

    @pytest.mark.asyncio
    async def test_progress_query_when_idle_runs_normally(self):
        """没任务在跑时「好了吗」就是普通一句话,照常交给 claude。"""
        gw, adapter, backend = _build(work_time=0.02)
        await gw.handle(_msg("好了吗"))
        assert backend.completed == ["好了吗"]
        assert not adapter.texts

    @pytest.mark.asyncio
    async def test_slot_cleared_after_turn(self):
        gw, adapter, backend = _build(work_time=0.02)
        await gw.handle(_msg("Q1"))
        slot = gw._slot("oc_test")
        assert slot.started_at is None and slot.streamer is None

    @pytest.mark.asyncio
    async def test_long_run_sends_completion_notice(self):
        gw, adapter, backend = _build(work_time=0.02)
        backend.last_run_went_long = True
        await gw.handle(_msg("Q1"))
        assert any("任务结束了" in t for t in adapter.texts)

    @pytest.mark.asyncio
    async def test_short_run_no_completion_notice(self):
        gw, adapter, backend = _build(work_time=0.02)
        await gw.handle(_msg("Q1"))
        assert not any("任务结束了" in t for t in adapter.texts)


# ---------- claude_core:软上限不杀,硬上限才杀 ----------

def _fake_cmd(sleep: float) -> list[str]:
    ev = json.dumps({"type": "result", "result": "done"})
    return ["sh", "-c", f"sleep {sleep}; echo '{ev}'"]


class TestSoftHardTimeout:
    def test_soft_deadline_does_not_kill(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(cc, "_RUN_TIMEOUT", 0.2)
        monkeypatch.setattr(cc, "_RUN_HARD_TIMEOUT", 5)
        s = cc.ClaudeSession(cwd=str(tmp_path))
        st = FakeStreamer()
        out = asyncio.run(s._execute(_fake_cmd(0.6), "p", st))
        assert not out.timed_out, "过了软上限也必须跑完"
        assert out.text == "done"
        assert s.last_run_went_long is True
        assert any(cc._LONG_RUN_BANNER in r for r, _ in st.sent), "软上限要挂横幅"

    def test_hard_deadline_kills(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(cc, "_RUN_TIMEOUT", 0.1)
        monkeypatch.setattr(cc, "_RUN_HARD_TIMEOUT", 0.3)
        s = cc.ClaudeSession(cwd=str(tmp_path))
        out = asyncio.run(s._execute(_fake_cmd(3), "p", FakeStreamer()))
        assert out.timed_out

    def test_fast_run_not_marked_long(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        s = cc.ClaudeSession(cwd=str(tmp_path))
        out = asyncio.run(s._execute(_fake_cmd(0), "p", FakeStreamer()))
        assert out.text == "done" and s.last_run_went_long is False

    def test_banner_cleared_on_finalize(self):
        st = FakeStreamer()
        asyncio.run(st.set_banner("⏳ 横幅"))
        assert "⏳ 横幅" in st.last
        asyncio.run(st.finalize(fallback="ok"))
        assert "⏳ 横幅" not in st.last
