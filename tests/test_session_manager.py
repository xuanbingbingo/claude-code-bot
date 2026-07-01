"""SessionManager 持久化与恢复单测（含 Gateway 异常路径落盘）。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.session_manager import SessionManager  # noqa: E402
from core.gateway import Gateway                 # noqa: E402


class FakeBackend:
    def __init__(self):
        self.current_session_id = None
        self.resumable_session_id = None
        self.resumed_with = None
        self._files = set()

    def session_file_exists(self, sid):
        return sid in self._files

    def set_resume_session(self, sid):
        self.resumed_with = sid


def test_persist_prefers_resumable(tmp_path):
    sm = SessionManager(lambda is_group=False: FakeBackend(), state_key="k", state_dir=str(tmp_path))
    b = sm.get_or_create("conv1")
    b.current_session_id = "dead-sid"       # 失败轮留下的残缺指针
    b.resumable_session_id = "good-sid"
    sm.persist("conv1")
    data = json.load(open(tmp_path / ".sessions-k.json"))
    assert data == {"conv1": "good-sid"}


def test_restore_resumes_existing_session(tmp_path):
    state = tmp_path / ".sessions-k.json"
    state.write_text(json.dumps({"conv1": "sid-a"}))

    def factory(is_group=False):
        b = FakeBackend()
        b._files = {"sid-a"}
        return b

    sm = SessionManager(factory, state_key="k", state_dir=str(tmp_path))
    assert sm.loaded_count() == 1
    b = sm.get_or_create("conv1")
    assert b.resumed_with == "sid-a"


def test_restore_skips_deleted_session(tmp_path):
    state = tmp_path / ".sessions-k.json"
    state.write_text(json.dumps({"conv1": "sid-gone"}))
    sm = SessionManager(lambda is_group=False: FakeBackend(), state_key="k", state_dir=str(tmp_path))
    b = sm.get_or_create("conv1")
    assert b.resumed_with is None  # jsonl 不在就正常开新，不硬 resume


def test_corrupted_state_file_tolerated(tmp_path):
    (tmp_path / ".sessions-k.json").write_text("{broken json")
    sm = SessionManager(lambda is_group=False: FakeBackend(), state_key="k", state_dir=str(tmp_path))
    assert sm.loaded_count() == 0


def test_no_state_key_disables_persistence(tmp_path):
    sm = SessionManager(lambda is_group=False: FakeBackend(), state_key="", state_dir=str(tmp_path))
    sm.get_or_create("conv1").current_session_id = "sid"
    sm.persist("conv1")  # no-op，不抛
    assert not list(tmp_path.glob(".sessions-*"))


def test_empty_sid_removes_pointer(tmp_path):
    sm = SessionManager(lambda is_group=False: FakeBackend(), state_key="k", state_dir=str(tmp_path))
    b = sm.get_or_create("conv1")
    b.current_session_id = "sid-1"
    b.resumable_session_id = "sid-1"
    sm.persist("conv1")
    b.current_session_id = None      # /new 之后
    b.resumable_session_id = None
    sm.persist("conv1")
    data = json.load(open(tmp_path / ".sessions-k.json"))
    assert data == {}


# ---------- Gateway：backend.run 抛异常也要落盘指针 ----------

class _Inbound:
    conv_id = "conv1"
    chat_type = "p2p"
    is_group = False
    is_command = False
    text = "hi"
    images = []


class _FakeStreamer:
    async def first_frame(self): ...
    async def finalize(self, fallback="", override_text=""): ...
    async def discard(self): return False


class _FakeAdapter:
    def backend_env(self, inbound): return {}
    def make_streamer(self, inbound): return _FakeStreamer()


class _ExplodingBackend(FakeBackend):
    def inject_env(self, env): ...
    async def run(self, prompt, streamer):
        # 模拟：init 事件已刷出新 session_id，随后执行炸掉
        self.current_session_id = "sid-from-crashed-run"
        self.resumable_session_id = "sid-from-crashed-run"
        raise RuntimeError("boom")


class _NoRelay:
    async def maybe_relay(self, inbound, resp, adapter): return False


def test_gateway_persists_pointer_even_on_exception(tmp_path):
    sm = SessionManager(lambda is_group=False: _ExplodingBackend(),
                        state_key="k", state_dir=str(tmp_path))
    gw = Gateway(_FakeAdapter(), sm, command_router=None, relay=_NoRelay())
    asyncio.run(gw.handle(_Inbound()))
    data = json.load(open(tmp_path / ".sessions-k.json"))
    assert data == {"conv1": "sid-from-crashed-run"}
