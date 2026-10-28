"""角色记忆 —— 网关统一注入（2026-10-24 起），不再靠人设里写「每次回答前先 Read」。

为什么：实测 `claude -p --resume` 只认会话首轮的 --append-system-prompt，之后每轮传的新系统提示
全被忽略；而续接会话里模型也基本不会再去 Read memory.md。于是飞书刚写的记忆，同一角色在另一边
几十分钟都看不到。解决：每轮把两份记忆文件的最新内容拼在用户消息前面（用户消息永远是新的）。

本模块三件事：
- discipline(role, where)：记忆纪律（读/写/结构/发文件/个人记忆库），进 append-system-prompt（首轮生效即可，内容不变）
- memory_block(role)：此刻的记忆内容（_shared + 私有；CEO 外加各部门摘要），进每轮用户消息前缀
- persona_stamp(paths)：人设文件指纹，变了就该开新会话（续接会话吃不到新人设）

角色参数在 ~/aiProjects/bots/_shared/roles.json；写记忆走 ~/aiProjects/bots/memo.py。
"""
import importlib.util
import json
import os

HOME = os.path.expanduser("~")
BOTS = os.environ.get("BOTS_DIR") or os.path.join(HOME, "aiProjects", "bots")
ROLES_FILE = os.path.join(BOTS, "_shared", "roles.json")
SHARED_FILE = os.path.join(BOTS, "_shared", "memory.md")
MEMO = os.path.join(BOTS, "memo.py")
PERSONAL_MEMORY = os.path.join(HOME, ".claude", "projects", "-Users-libin-aiProjects", "memory")

BLOCK_OPEN = "[每轮注入 · 开始]"
BLOCK_CLOSE = "[每轮注入 · 结束]"
CHATLOG_KEEP = 200          # 每个角色保留最近多少轮跨渠道对话摘要


def _memo():
    spec = importlib.util.spec_from_file_location("bots_memo", MEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def roles_config():
    try:
        return json.load(open(ROLES_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"roles": {}}


def role_info(role):
    return (roles_config().get("roles") or {}).get(role) or {}


def role_from_env():
    """BOT_ROLE 显式优先；否则从 BOT_PERSONA_FILE 里的 bots/<x>/persona.md 推。"""
    r = os.environ.get("BOT_ROLE", "").strip()
    if r:
        return r
    for p in os.environ.get("BOT_PERSONA_FILE", "").split(","):
        p = os.path.abspath(os.path.expanduser(p.strip()))
        if p.startswith(BOTS + os.sep) and p.endswith("persona.md"):
            return os.path.relpath(p, BOTS).split(os.sep)[0]
    return ""


# ---------------- 读 ----------------

def _read(path):
    try:
        return open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def _tail_lines(lines, limit):
    """从后往前取整行，总长不超过 limit。"""
    out, n = [], 0
    for l in reversed(lines):
        n += len(l) + 1
        if n > limit and out:
            out.append("…（更早的省略，要看 Read 文件或 archive/）")
            break
        out.append(l)
    return list(reversed(out))


def _head(lines, limit):
    out, n = [], 0
    for l in lines:
        n += len(l) + 1
        if n > limit and out:
            out.append("…（后面省略）")
            break
        out.append(l)
    return out


def role_sections(role):
    """→ (facts, flow) 两段的条目列表（兼容旧平铺格式）。"""
    memo = _memo()
    _, facts, flow = memo.split_sections(_read(memo.mem_path(role)))
    return facts, flow


def shared_text(limit=6000):
    text = _read(SHARED_FILE).strip()
    if not text:
        return "（空）"
    lines = text.splitlines()
    # 事实区整段保留，广播日志取尾部
    if "## 广播日志" in text:
        head, log = text.split("## 广播日志", 1)
        log_lines = [l for l in log.splitlines()[1:] if l.strip()]
        return head.strip() + "\n## 广播日志\n" + "\n".join(_tail_lines(log_lines, max(1500, limit - len(head))))
    return "\n".join(_tail_lines(lines, limit))


def memory_block(role, shared_limit=6000, facts_limit=6000, flow_limit=10000):
    """此刻的记忆，给每轮用户消息做前缀。"""
    info = role_info(role)
    who = info.get("who", role)
    facts, flow = role_sections(role)
    parts = [
        f"## 你的记忆（程序刚读的，比你会话里记着的新；有出入以这里为准。老板看不到这段，别复述、别提它存在）",
        f"### 公共记忆 _shared/memory.md\n{shared_text(shared_limit)}",
        f"### 你（{who}）的长期事实 bots/{role}/memory.md\n" + ("\n".join(_head(facts, facts_limit)) if facts else "（空）"),
        f"### 你（{who}）的近期流水\n" + ("\n".join(_tail_lines(flow, flow_limit)) if flow else "（空）"),
        "### 你最近和老板的对话（跨渠道：飞书 / 运转台 / 定时任务 都在，由程序每轮记录，最新在最后）\n"
        + ("\n".join(recent_chats(role)) or "（还没有）"),
    ]
    if info.get("reads_all"):
        parts.append(_all_departments_digest(exclude=role))
    parts.append(_company_inbox(role))   # 公司通讯：CEO / 老板 / 同事发给你的话，每轮注入（10-24）
    return "\n\n".join(parts) + "\n"


def _company_inbox(role):
    """运转台的公司总线（~/aiProjects/ceo-agent/comms.py）：飞书里的角色 bot 同样收得到 CEO 传达的拍板和询问。"""
    try:
        import importlib.util, sys
        ceo_dir = os.path.join(os.path.expanduser("~"), "aiProjects", "ceo-agent")
        if ceo_dir not in sys.path:
            sys.path.insert(0, ceo_dir)
        import comms  # noqa
        import members  # noqa（comms 已把 collectors 加进 sys.path）
        m = next((x for x in members.all_members() if x.get("bot") == role or x.get("id") == role), None)
        who = (m or {}).get("id") or ("ceo" if role == "ceo" else role)
        return comms.inbox_block(who) or "### 公司收件箱\n（没有新消息）"
    except Exception as e:
        return f"### 公司收件箱\n（读不到：{e}）"


def _all_departments_digest(exclude, per_role=1400):
    out = ["### 各部门记忆摘要（你是总调度，能看全部；每个部门取长期事实开头 + 最近几条流水）"]
    for r, info in (roles_config().get("roles") or {}).items():
        if r == exclude:
            continue
        facts, flow = role_sections(r)
        if not facts and not flow:
            continue
        body = []
        if facts:
            body += _head(facts, per_role // 2)
        if flow:
            body += _tail_lines(flow, per_role // 2)
        out.append(f"#### {info.get('who', r)}（bots/{r}）\n" + "\n".join(body))
    return "\n\n".join(out)


# ---------------- 跨渠道最近对话 ----------------
# 10-24 晚第二个坑：记忆文件共享了，但「对话记录」仍是每个渠道一份——老板在飞书跟 CEO 聊了派活，
# 运转台里的 CEO 翻自己的聊天历史答的是两天前的。模型不会把每句闲聊写进记忆，所以由程序每轮记一行。

def chatlog_path(role):
    return os.path.join(BOTS, role, "chatlog.jsonl")


def log_turn(role, channel, user_text, assistant_text, limit_user=240, limit_assistant=400, t=None):
    """记一轮对话摘要（截断）到 bots/<role>/chatlog.jsonl，滚动只留最近 CHATLOG_KEEP 轮。调用方失败不应影响对话。
    t：可选时间串 "YYYY-MM-DD HH:MM"（回填历史用），默认当前时间。"""
    if not role:
        return
    import time as _t
    rec = {"t": t or _t.strftime("%Y-%m-%d %H:%M"), "ch": channel,
           "u": _squash(user_text, limit_user), "a": _squash(assistant_text, limit_assistant)}
    p = chatlog_path(role)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        lines = open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else []
    except OSError:
        lines = []
    lines.append(json.dumps(rec, ensure_ascii=False))
    lines = lines[-CHATLOG_KEEP:]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, p)


def _squash(text, limit):
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def recent_chats(role, n=12, limit=3000):
    """最近 n 轮跨渠道对话，按时间正序，最新在最后（和流水一致）。"""
    try:
        lines = open(chatlog_path(role), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    recs = []
    for l in lines:
        try:
            recs.append(json.loads(l))
        except ValueError:
            continue
    recs.sort(key=lambda r: r.get("t", ""))      # 多渠道各自追加 + 回填历史，文件顺序≠时间顺序
    out = [f"- [{r.get('t','')} · {r.get('ch','')}] 老板：{r.get('u','')}\n  我：{r.get('a','')}" for r in recs[-n:]]
    return _tail_lines(out, limit)


def wrap_turn(role, prompt, extra=""):
    """每轮真正发给 claude 的正文 = 注入块 + 用户原话。调用方的历史/日志里只存原话。"""
    body = memory_block(role) + (("\n" + extra.strip() + "\n") if extra else "")
    return f"{BLOCK_OPEN}\n{body}{BLOCK_CLOSE}\n\n用户说：\n{prompt}"


# ---------------- 纪律 ----------------

def discipline(role, where="飞书"):
    """记忆纪律全文。where = 飞书 | 运转台 | 定时任务，只影响「发文件」那一节的措辞。"""
    info = role_info(role)
    who = info.get("who", role)
    remember = info.get("remember", "老板的偏好、拍板结论、待办、本部门的重要变化")
    extra = "".join(f"- {x}\n" for x in info.get("extra", []))
    cfg = roles_config()
    keep = cfg.get("keep_days", 45)
    if where == "运转台":
        send = ("你此刻在「CEO 运转台」网页里和老板对话：产出文件直接在回答里给绝对路径，不要用 cc-connect send、也不要发飞书。")
    elif where == "定时任务":
        send = ("本轮是定时任务触发，最终回复会自动推给老板；要把文件/图片/视频发到飞书，执行 "
                "`python3 /Users/libin/aiProjects/claude-gateway/tools/feishu-send-file.py <文件绝对路径>`。")
    else:
        send = ("你跑在 claude-gateway 飞书网关里。生成了报告 / 图表 / 导出文件要交给用户时，直接执行 "
                "`python3 /Users/libin/aiProjects/claude-gateway/tools/feishu-send-file.py <文件绝对路径>`，"
                "不传收件人就发回当前聊天；绝对路径最稳，发完口头确认「已发送」。")
    return f"""\
# 记忆纪律（网关统一注入，所有角色同一份规矩；你是 {who}，角色目录 bots/{role}/）

## 一、读记忆：不用自己读，每条消息前已经注入
每条用户消息开头有一段「{BLOCK_OPEN} … {BLOCK_CLOSE}」，是程序在你回答前一刻读出来的
`bots/_shared/memory.md`（公共）和 `bots/{role}/memory.md`（你的私有）最新内容{"，以及各部门记忆摘要" if info.get("reads_all") else ""}。
- 这就是你此刻的记忆，比你会话上下文里记着的新；有出入以注入块为准。飞书 / 运转台 / 定时任务里的你共用同一份文件，别处刚写的这里立刻能看到
- 老板看不到这段：回答时别复述它、别提它存在、别说「根据注入 / 根据记忆文件」，直接当作你知道的事
- 注入只带长期事实全文和近期流水，更早的要 `Read bots/{role}/memory.md` 或 `bots/{role}/archive/`
- 注入块末尾还有「你最近和老板的对话」：跨渠道（飞书 / 运转台 / 定时任务）最近十来轮的一句话摘要，由程序自动记。老板问「我上次/最近跟你聊了什么」看这里，不要只翻当前渠道的聊天记录；完整记录在 `bots/{role}/chatlog.jsonl`

## 二、写记忆：一律用 memo.py，不要手工 echo >>
值得以后记住的（{remember}）当场写，不要等老板提醒：
    python3 {MEMO} note {role} "<完整结论>" --public "<给全公司看的一句话>"
- `--public` 那句会同时进公共记忆（谁 · 结论 · 详情在哪），CEO 和其他部门靠它知道你做了什么；不给 --public 就自动取内容前 80 字
- 🔴 必须带 --public 的：老板的拍板结论、你完成的调研/报告/分析、本部门的重要变化
- 🔴 只能私有、要加 `--private-only` 的：老板的个人隐私/资金/账号凭证、客户敏感信息、未经证实的判断、纯内部流水（排错过程、临时参数）
- 老板明确说「记入公共记忆 / 同步给大家 / 广播」→ `python3 {MEMO} public {role} "<内容>"`，写完回报「已写入公共记忆：…」
- 稳定不变的长期结论（规则、方法论、固定偏好）→ `python3 {MEMO} fact {role} "<内容>"` 进「长期事实」
{extra}
## 三、记忆文件的结构，别手动改
`memory.md` 分「## 长期事实」和「## 流水」两段：流水 append-only、最新在最后；超过 {keep} 天的流水每晚由
`bots/memory_tidy.py` 归档到 `bots/{role}/archive/` 并提炼进长期事实。禁止整文件覆盖、禁止改段落结构。

## 四、发送文件给用户
{send}

## 五、个人背景记忆库（只读，用于了解老板）
老板的长期个人记忆库在 `{PERSONAL_MEMORY}/`，`MEMORY.md` 是索引（一行一条，指向 topic 文件）。
- 问题涉及老板的个人情况、偏好、公司、历史项目或过往决策时：先 `grep -i "<关键词>" {PERSONAL_MEMORY}/MEMORY.md` 定位，再 Read 对应 topic 文件
- 不要一次性通读索引或整个目录；🔴 严格只读，禁止写入/修改/删除
- 系统提示里「把记忆写进 auto-memory 目录」的指引对你不适用——你的记忆只走 memo.py 进 bots/{role}/
"""


# ---------------- 人设指纹 ----------------

def persona_stamp(paths):
    """人设文件 + 纪律模块的 mtime 指纹。变了 → 续接会话吃不到新人设，该开新会话。"""
    parts = []
    for p in list(paths or []) + [__file__, ROLES_FILE]:
        try:
            parts.append(f"{os.path.basename(p)}:{int(os.stat(os.path.expanduser(p)).st_mtime)}")
        except OSError:
            parts.append(f"{os.path.basename(p)}:0")
    return "|".join(parts)
