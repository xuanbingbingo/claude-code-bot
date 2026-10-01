"""进度询问识别 —— 任务在跑时,「好了吗」这类话不该抢占掉正在跑的那轮。

背景:同会话新消息默认抢占(见 gateway.py)。但用户在长任务中途问一句「好了吗」,
本意是看进度,抢占等于亲手把任务杀掉 —— 历史上问进度把渲染任务问死过。
所以在抢占之前先过这道闸:命中就由网关直接回进度,不 spawn claude、不碰在跑的进程。

判定刻意用固定规则而非模型:零延迟、结果可复现。两个条件同时满足才算:
  1. 去掉标点/语气词后足够短(MAX_LEN 字以内)—— 长句多半夹着新指令,宁可放它去抢占;
  2. 整句命中下面某个进度询问模式(fullmatch,不是包含)。
误判兜底:被当成进度询问的指令,回复里会提示「要打断请发 /stop」;
没认出来的进度询问,行为与改动前一致(抢占),发现后往 _PATTERNS 里补一条即可。
"""
import re

MAX_LEN = 12

# 首尾可剥掉的标点、空白与语气词(只剥首尾,不动中间)
_TRIM = " \t\r\n?？!！.。,，~～…、:：;；呀啊呢吧嘛哈呐哦噢喔额呃"
_LEAD = re.compile(r"^(那个|那|所以|请问|问下|问一下|话说|喂|哎|诶|嗯|老哥|兄弟|你)+")

_VERB = r"(做|跑|弄|搞|干|渲染|生成|下载|处理|执行|部署|装|安装|编译|构建|传|上传|出片|写)"
_ASK = r"(吗|么|没|没有|了没|了吗|了么|了没有)"

_PATTERNS = [
    rf"(好|弄好|搞好|做好|跑好)了?{_ASK}",          # 好了吗 / 好了没 / 弄好了没有
    r"好没好",
    rf"{_VERB}?完(了|成了|成)?{_ASK}",              # 完了吗 / 跑完没 / 做完了吗
    rf"完成(了)?{_ASK}",                            # 完成了吗
    rf"(有|出)?结果(了)?{_ASK}?",                   # 有结果了吗 / 结果呢
    rf"(出来|生成好|渲染好)了?{_ASK}",               # 出来了吗
    r"(现在|目前|当前)?(什么|啥)?(进度|进展)(怎么样|如何|咋样|到哪了|多少)?(了)?",
    r"(看|查|报)(一?下)?(进度|进展)",               # 看下进度 / 报一下进度
    r"(现在|目前)?(怎么样|咋样|如何|怎样)了?",        # 怎么样了
    r"(跑|做|弄|进行)?到哪(里|儿|一步|步)?了?",       # 到哪了 / 跑到哪一步了
    r"(还要|还得|还需要|要|大概|估计)?(多久|多长时间|几分钟)(能好|能完|好|完)?",
    r"多久(能|才能)?(好|完)",
    rf"还在{_VERB}?(吗|么|嘛)?",                     # 还在跑吗
    r"(还)?在吗",
    r"(还)?活着(吗|么|没)?",
    r"(是不是)?卡(住|死)?了(吗|么|没)?",
    r"(状态|情况)(怎么样|如何|咋样)?",
]
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))


def _normalize(text: str) -> str:
    s = (text or "").strip().strip(_TRIM)
    s = _LEAD.sub("", s).strip(_TRIM)
    return re.sub(r"\s+", "", s)


def is_progress_query(text: str) -> bool:
    s = _normalize(text)
    if not s or len(s) > MAX_LEN:
        return False
    return _RE.fullmatch(s) is not None


def _fmt_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒" if s else f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def progress_reply(elapsed: float, streamer=None) -> str:
    """由在跑那轮的 streamer 当场推导进度文案(不留状态)。"""
    lines = [f"⏳ 还在跑，已运行 {_fmt_elapsed(elapsed)}。"]
    if streamer is not None:
        cur = (getattr(streamer, "current_status", "") or "").strip()
        steps = getattr(streamer, "steps", None) or []
        if cur:
            lines.append(f"当前：{cur}")
        elif getattr(streamer, "has_content", False):
            lines.append("当前：正在写回复")
        elif steps:
            lines.append(f"刚完成：{steps[-1]}")
        if steps:
            lines.append(f"已完成 {len(steps)} 步。")
    lines.append("跑完会在原卡片出结果；要打断请发 /stop。")
    return "\n".join(lines)
