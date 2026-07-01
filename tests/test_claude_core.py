"""claude_core 会话生命周期核心逻辑单测。

覆盖丢上下文 bug 的全部根因路径：
错误分类（瞬时 vs 损坏）、会话文件尾部自愈、resume 回退链、_settle 善后。
全部用临时 HOME，不碰真实 ~/.claude。
"""
import asyncio
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_core as cc  # noqa: E402


# ---------- 工具 ----------

def _make_session_file(tmp_home, sid: str, lines: list[str] | None = None) -> str:
    """在临时 HOME 下造一个 session jsonl。"""
    d = os.path.join(tmp_home, ".claude", "projects", "-tmp-proj")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid}.jsonl")
    content = lines if lines is not None else [json.dumps({"type": "user", "n": 1})]
    with open(path, "w") as f:
        for ln in content:
            f.write(ln + "\n")
    return path


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return str(tmp_path)


# ---------- 错误分类 ----------

class TestClassifyError:
    @pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504, 529])
    def test_transient_statuses(self, status):
        assert cc._classify_error(status, "API Error") == "transient"

    @pytest.mark.parametrize("text", [
        "API Error: upstream unreachable",
        "the server is Warming Up, please wait",
        "Overloaded",
        "server-side issue detected",
        "please try again later",
        "request timeout",
        "failed to authenticate",
        "OAuth authentication error",
    ])
    def test_transient_markers(self, text):
        assert cc._classify_error(None, text) == "transient"

    def test_fatal_400(self):
        assert cc._classify_error(400, "invalid_request: messages malformed") == "fatal"

    def test_fatal_unknown_text(self):
        assert cc._classify_error(None, "No conversation found with session id xxx") == "fatal"


# ---------- 会话文件尾部自愈 ----------

class TestHealSessionTail:
    def test_intact_file_untouched(self, tmp_home):
        sid = str(uuid.uuid4())
        path = _make_session_file(tmp_home, sid)
        before = open(path, "rb").read()
        cc._heal_session_tail(sid)
        assert open(path, "rb").read() == before

    def test_truncated_tail_trimmed(self, tmp_home):
        sid = str(uuid.uuid4())
        path = _make_session_file(tmp_home, sid)
        with open(path, "ab") as f:
            f.write(b'{"type":"assistant","content":"half wri')  # 被 SIGKILL 截断的半行
        cc._heal_session_tail(sid)
        data = open(path, "rb").read()
        assert data.endswith(b"\n")
        for ln in data.splitlines():
            json.loads(ln)  # 每行都必须是完整 JSON

    def test_valid_json_missing_newline_gets_newline(self, tmp_home):
        sid = str(uuid.uuid4())
        path = _make_session_file(tmp_home, sid)
        with open(path, "ab") as f:
            f.write(b'{"type":"result","ok":true}')  # 完整 JSON 只缺换行
        cc._heal_session_tail(sid)
        lines = open(path, "rb").read().splitlines()
        assert json.loads(lines[-1]) == {"type": "result", "ok": True}

    def test_missing_file_noop(self, tmp_home):
        cc._heal_session_tail(str(uuid.uuid4()))  # 不存在也不能抛


# ---------- resume 回退链 ----------

class TestBuildSessionFlags:
    def test_new_session_no_flags(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        assert s.build_session_flags() == []

    def test_explicit_resume_wins(self, tmp_home):
        sid = str(uuid.uuid4())
        _make_session_file(tmp_home, sid)
        s = cc.ClaudeSession(cwd="/tmp")
        s.set_resume_session(sid)
        assert s.build_session_flags() == ["--resume", sid]

    def test_pin_current_session(self, tmp_home):
        sid = str(uuid.uuid4())
        _make_session_file(tmp_home, sid)
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = sid
        assert s.build_session_flags() == ["--resume", sid]

    def test_fallback_to_last_good_when_current_gone(self, tmp_home):
        good = str(uuid.uuid4())
        _make_session_file(tmp_home, good)
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = str(uuid.uuid4())  # jsonl 不存在（被杀丢失）
        s.last_good_session_id = good
        assert s.build_session_flags() == ["--resume", good]
        assert s.current_session_id == good  # 指针被纠正

    def test_all_gone_starts_fresh_never_continue(self, tmp_home):
        # 绝不能退到 --continue：同 cwd 会串到 CLI 的会话
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = str(uuid.uuid4())
        s.last_good_session_id = str(uuid.uuid4())
        assert s.build_session_flags() == []

    def test_resume_heals_truncated_tail(self, tmp_home):
        sid = str(uuid.uuid4())
        path = _make_session_file(tmp_home, sid)
        with open(path, "ab") as f:
            f.write(b'{"broken')
        s = cc.ClaudeSession(cwd="/tmp")
        s.set_resume_session(sid)
        s.build_session_flags()
        assert open(path, "rb").read().endswith(b"\n")


# ---------- resumable_session_id ----------

class TestResumableSessionId:
    def test_prefers_current_when_exists(self, tmp_home):
        cur, good = str(uuid.uuid4()), str(uuid.uuid4())
        _make_session_file(tmp_home, cur)
        _make_session_file(tmp_home, good)
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id, s.last_good_session_id = cur, good
        assert s.resumable_session_id == cur

    def test_falls_back_when_current_gone(self, tmp_home):
        good = str(uuid.uuid4())
        _make_session_file(tmp_home, good)
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = str(uuid.uuid4())
        s.last_good_session_id = good
        assert s.resumable_session_id == good

    def test_none_when_nothing_exists(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = str(uuid.uuid4())
        assert s.resumable_session_id is None


# ---------- _settle 善后 ----------

class _FakeStreamer:
    def __init__(self, has_content=False):
        self.has_content = has_content
        self.appended = []

    async def clear_status(self):
        pass

    async def append(self, chunk):
        self.appended.append(chunk)


def _settle(session, outcome, streamer=None):
    return asyncio.run(session._settle(outcome, streamer))


class TestSettle:
    def test_success_promotes_last_good(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = "sid-1"
        assert _settle(s, cc._RunOutcome(text="ok")) == "ok"
        assert s.last_good_session_id == "sid-1"

    def test_transient_error_keeps_session(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = "sid-1"
        msg = _settle(s, cc._RunOutcome(text="API Error", is_error=True, api_error_status=502))
        assert "临时故障" in msg
        assert s.new_session is False           # 会话必须保留
        assert s.current_session_id == "sid-1"

    def test_transient_error_notifies_streamer_with_content(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        st = _FakeStreamer(has_content=True)
        _settle(s, cc._RunOutcome(text="API Error", is_error=True, api_error_status=503), st)
        assert any("临时故障" in a for a in st.appended)

    def test_fatal_falls_back_to_last_good(self, tmp_home):
        good = str(uuid.uuid4())
        _make_session_file(tmp_home, good)
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = str(uuid.uuid4())  # 损坏的会话
        s.last_good_session_id = good
        msg = _settle(s, cc._RunOutcome(text="No conversation found", is_error=True))
        assert "回退" in msg
        assert s.resume_session_id == good        # 下一轮直接 --resume 完好会话
        assert s.new_session is False

    def test_fatal_without_fallback_starts_new(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = str(uuid.uuid4())
        msg = _settle(s, cc._RunOutcome(text="invalid_request", is_error=True, api_error_status=400))
        assert "新会话" in msg
        assert s.new_session is True

    def test_stall_keeps_session(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = "sid-1"
        msg = _settle(s, cc._RunOutcome(stalled=True))
        assert "会话已保留" in msg
        assert s.new_session is False

    def test_timeout_keeps_session(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.new_session = False
        s.current_session_id = "sid-1"
        msg = _settle(s, cc._RunOutcome(timed_out=True))
        assert "会话已保留" in msg
        assert s.new_session is False

    def test_oversize_starts_new(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.current_session_id = "sid-1"
        msg = _settle(s, cc._RunOutcome(raw_tail="Request too large: max 32MB"))
        assert "过大" in msg
        assert s.new_session is True


# ---------- 会话状态切换 ----------

class TestSessionStateTransitions:
    def test_new_session_clears_last_good(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.last_good_session_id = "old"
        s.set_new_session()
        # /new 后旧线的 last_good 必须清掉，否则新会话首轮一失败会把旧对话诈尸回来
        assert s.last_good_session_id is None

    def test_resume_sets_baseline(self, tmp_home):
        s = cc.ClaudeSession(cwd="/tmp")
        s.set_resume_session("sid-x")
        assert s.last_good_session_id == "sid-x"
        assert s.current_session_id == "sid-x"


# ---------- 元命令过滤 ----------

class TestMetaSlashCommand:
    @pytest.mark.parametrize("p", ["/usage", "/exit", " /sessions", "/RESUME 3"])
    def test_meta(self, p):
        assert cc._is_meta_slash_command(p) is True

    @pytest.mark.parametrize("p", ["/goal 出30篇选题", "普通消息", "/deep-research xx"])
    def test_not_meta(self, p):
        assert cc._is_meta_slash_command(p) is False
