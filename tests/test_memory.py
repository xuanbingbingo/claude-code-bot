"""角色记忆注入(core/memory.py)单测:纪律、注入块、人设指纹、backend 每轮前缀。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup_bots(tmp_path, monkeypatch):
    bots = tmp_path / "bots"
    (bots / "_shared").mkdir(parents=True)
    (bots / "r1").mkdir()
    (bots / "ceo").mkdir()
    (bots / "_shared" / "roles.json").write_text(json.dumps({
        "keep_days": 45,
        "roles": {"r1": {"who": "角色一", "remember": "偏好"},
                  "ceo": {"who": "CEO", "reads_all": True}}}), encoding="utf-8")
    (bots / "_shared" / "memory.md").write_text("# 公共\n## 事实区\n- 公共事实A\n## 广播日志\n- [2026-10-24 08:00] 角色一：做了X（详情 bots/r1/memory.md）\n", encoding="utf-8")
    (bots / "r1" / "memory.md").write_text("# 角色一 · 私有记忆\n\n## 长期事实\n- 规则甲\n\n## 流水\n- [2026-10-24 07:17] 查了 chatcut\n", encoding="utf-8")
    (bots / "ceo" / "memory.md").write_text("# CEO\n- [2026-10-22] 老板拍板塔台优先\n", encoding="utf-8")   # 旧平铺格式
    # memo.py 从真实目录拷一份
    real = os.path.join(os.path.expanduser("~"), "aiProjects", "bots", "memo.py")
    (bots / "memo.py").write_text(open(real, encoding="utf-8").read(), encoding="utf-8")
    import core.memory as M
    monkeypatch.setattr(M, "BOTS", str(bots))
    monkeypatch.setattr(M, "ROLES_FILE", str(bots / "_shared" / "roles.json"))
    monkeypatch.setattr(M, "SHARED_FILE", str(bots / "_shared" / "memory.md"))
    monkeypatch.setattr(M, "MEMO", str(bots / "memo.py"))
    return M, bots


def test_memory_block_has_shared_facts_flow(tmp_path, monkeypatch):
    M, _ = _setup_bots(tmp_path, monkeypatch)
    b = M.memory_block("r1")
    assert "公共事实A" in b and "规则甲" in b and "chatcut" in b
    assert "各部门记忆摘要" not in b          # 普通角色看不到别人的私有记忆


def test_ceo_reads_all(tmp_path, monkeypatch):
    M, _ = _setup_bots(tmp_path, monkeypatch)
    b = M.memory_block("ceo")
    assert "各部门记忆摘要" in b and "chatcut" in b and "塔台优先" in b


def test_wrap_turn_puts_user_text_last(tmp_path, monkeypatch):
    M, _ = _setup_bots(tmp_path, monkeypatch)
    w = M.wrap_turn("r1", "你好")
    assert w.startswith(M.BLOCK_OPEN) and w.rstrip().endswith("用户说：\n你好") and M.BLOCK_CLOSE in w


def test_discipline_mentions_memo_and_role(tmp_path, monkeypatch):
    M, _ = _setup_bots(tmp_path, monkeypatch)
    d = M.discipline("r1", where="飞书")
    assert "memo.py note r1" in d and "--public" in d and "feishu-send-file" in d and "角色一" in d
    d2 = M.discipline("r1", where="运转台")
    assert "运转台" in d2 and "不要发飞书" in d2


def test_role_from_env_derives_from_persona_path(tmp_path, monkeypatch):
    M, bots = _setup_bots(tmp_path, monkeypatch)
    monkeypatch.delenv("BOT_ROLE", raising=False)
    monkeypatch.setenv("BOT_PERSONA_FILE", f"/x/agent.md,{bots}/r1/persona.md")
    assert M.role_from_env() == "r1"
    monkeypatch.setenv("BOT_ROLE", "ceo")
    assert M.role_from_env() == "ceo"


def test_persona_stamp_changes_on_edit(tmp_path, monkeypatch):
    M, bots = _setup_bots(tmp_path, monkeypatch)
    p = bots / "r1" / "persona.md"
    p.write_text("v1", encoding="utf-8")
    os.utime(p, (1_700_000_000, 1_700_000_000))
    s1 = M.persona_stamp([str(p)])
    os.utime(p, (1_700_000_100, 1_700_000_100))
    assert M.persona_stamp([str(p)]) != s1


def test_backend_wraps_prompt_each_turn(monkeypatch):
    from backends.claude_code import ClaudeCodeBackend
    seen = []

    async def fake_run(prompt, streamer=None):
        seen.append(prompt)
        return "ok"
    b = ClaudeCodeBackend(cwd="/tmp", turn_prefix=lambda p: "[CTX]\n" + p)
    monkeypatch.setattr(b.session, "run_claude", fake_run)
    asyncio.run(b.run("第一句", None))
    asyncio.run(b.run("第二句", None))
    assert seen == ["[CTX]\n第一句", "[CTX]\n第二句"]


def test_backend_prefix_failure_is_tolerated(monkeypatch):
    from backends.claude_code import ClaudeCodeBackend

    def boom(p):
        raise OSError("no memory file")
    seen = []

    async def fake_run(prompt, streamer=None):
        seen.append(prompt)
        return "ok"
    b = ClaudeCodeBackend(cwd="/tmp", turn_prefix=boom)
    monkeypatch.setattr(b.session, "run_claude", fake_run)
    asyncio.run(b.run("原话", None))
    assert seen == ["原话"]


def test_chatlog_cross_channel_sorted_and_trimmed(tmp_path, monkeypatch):
    M, bots = _setup_bots(tmp_path, monkeypatch)
    M.log_turn("r1", "运转台", "问A", "答A", t="2026-10-24 09:00")
    M.log_turn("r1", "飞书", "问B " * 200, "答B", t="2026-10-24 08:00")   # 时间更早但后写入;超长会截断
    M.log_turn("r1", "定时任务", "", "")                                   # 空内容也记(调用方保证有 resp)
    rows = M.recent_chats("r1", n=10, limit=100000)
    assert "飞书] 老板：问B" in rows[0] and "…" in rows[0] and "运转台] 老板：问A" in rows[1]
    blk = M.memory_block("r1")
    assert "你最近和老板的对话" in blk and "问A" in blk
    monkeypatch.setattr(M, "CHATLOG_KEEP", 3)
    for i in range(5):
        M.log_turn("r1", "飞书", f"q{i}", f"a{i}", t=f"2026-10-24 10:0{i}")
    assert len(open(M.chatlog_path("r1"), encoding="utf-8").read().splitlines()) == 3


def test_backend_logs_turn_after_run(monkeypatch):
    from backends.claude_code import ClaudeCodeBackend
    logged = []

    async def fake_run(prompt, streamer=None):
        return "回答"
    b = ClaudeCodeBackend(cwd="/tmp", turn_log=lambda u, a: logged.append((u, a)))
    monkeypatch.setattr(b.session, "run_claude", fake_run)
    asyncio.run(b.run("原话", None))
    assert logged == [("原话", "回答")]
