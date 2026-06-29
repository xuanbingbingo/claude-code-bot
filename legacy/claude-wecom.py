#!/usr/bin/env python3
"""
Claude Code 企业微信(WeCom)智能机器人网关 —— 长连接模式
对标 claude-feishu.py,复用 claude_core,兼容现有多角色 .env / persona 机制。

企微智能机器人长连接协议(wss://openws.work.weixin.qq.com):
  连接后发 aibot_subscribe(bot_id+secret)订阅 →
  收 aibot_msg_callback(单聊/群@消息)/ aibot_event_callback(进会话/断开)→
  用 aibot_respond_msg(stream,**全量替换** content,finish=true 收尾)回复 →
  aibot_send_msg 主动推送(需用户先发过消息;也用于流式超时降级)。
硬约束:收到消息 5s 内必须回(发流式占位即可)、流式 6min 超时(超时降级 aibot_send_msg)、
        心跳 30s、每会话限频 30 条/分。

⚠️ 待联调校准(拿到真实 BotID/Secret 后对照官方文档 path/101463 与 SDK):
  - 个别帧字段名(aibot_respond_msg 的 stream 体)以官方 SDK 为准,已集中在 _frame_* 函数。
  - 图片/语音/文件:企微媒体需按 url+独立 aeskey 做 AES-256-CBC 解密下载,本版先支持文字(TODO)。
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime

# 企微长连接需直连其网关:清除可能存在的 HTTP(S)_PROXY,避免 websockets 走代理被重置。
# (本机/某些运行环境会注入 HTTPS_PROXY,websockets 15 默认读环境变量走代理 → 连不上 wss。)
for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_pk, None)

import websockets

from claude_core import ClaudeSession, list_sessions, set_session_title, _session_file_exists, list_agents

# ── 加载 .env(与飞书/telegram 同款:脚本同目录 .env,环境变量优先)──────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

WECOM_BOT_ID = os.environ.get("WECOM_BOT_ID", "")
WECOM_SECRET = os.environ.get("WECOM_SECRET", "")
WECOM_WS_URL = os.environ.get("WECOM_WS_URL", "wss://openws.work.weixin.qq.com")
if not WECOM_BOT_ID or not WECOM_SECRET:
    print("❌ 缺少 WECOM_BOT_ID / WECOM_SECRET,请在 .env 中设置")
    sys.exit(1)

# 流式安全时限:企微流式硬超时 6min,留 1min 余量,到点降级为主动推送
STREAM_SAFE_SEC = int(os.environ.get("WECOM_STREAM_SAFE_SEC", "300"))
MAX_MSG_LEN = 3800

# 每个会话 id(单聊=from.userid / 群=chatid)→ ClaudeSession
_sessions: dict[str, ClaudeSession] = {}
# 会话 id → chat_type(1=单聊 2=群聊),发送时据此决定 aibot_send_msg 的 chat_type
_chat_type: dict[str, int] = {}


# ── 人设加载(与飞书同逻辑,不依赖任何平台 SDK)────────────────────────────
def _load_bot_persona() -> str:
    inline = os.environ.get("BOT_PERSONA", "").strip()
    if inline:
        return inline
    path = os.environ.get("BOT_PERSONA_FILE", "").strip()
    if not path:
        return ""
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        print(f"[WARN] 读取人设文件失败 {path}: {e}")
        return ""
    if raw.startswith("---"):
        seg = raw.split("---", 2)
        if len(seg) == 3:
            raw = seg[2]
    return raw.strip()


_BOT_PERSONA = _load_bot_persona()
_BOT_NAME = os.environ.get("BOT_NAME", "").strip()

# ── bot 间协作(relay):注入队友提示;企微精确 @ 需 userid,留二期对齐 ──────
_RELAY_ENABLED = os.environ.get("BOT_RELAY", "") == "1"
_RELAY_TEAMMATES = [s.split(":")[-1].strip() for s in os.environ.get("BOT_TEAMMATES", "").split(",") if s.strip()]


def _relay_system_prompt() -> str:
    if not (_RELAY_ENABLED and _RELAY_TEAMMATES):
        return ""
    mates = "、".join(_RELAY_TEAMMATES)
    return (
        f"你在一个多 bot 协作群里,队友有:{mates}。完成自己这一棒后,"
        "在回复最后单独一行用 `@队友名 交代的话` 把任务传下去;彻底完结才不 @。每条最多 @ 一位。"
    )


def _get_session(conv_id: str) -> ClaudeSession:
    if conv_id not in _sessions:
        _sessions[conv_id] = ClaudeSession()
    sess = _sessions[conv_id]
    parts = []
    if _BOT_PERSONA:
        parts.append(_BOT_PERSONA)
    rp = _relay_system_prompt()
    if rp:
        parts.append(rp)
    sess.extra_append_prompt = "\n\n".join(parts)
    return sess


# ── 协议帧构造(字段集中此处,联调时只改这里)──────────────────────────────
def _req_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _frame_subscribe() -> dict:
    return {"cmd": "aibot_subscribe", "headers": {"req_id": f"aibot_subscribe_{int(time.time()*1000)}"},
            "body": {"bot_id": WECOM_BOT_ID, "secret": WECOM_SECRET}}


def _frame_respond_stream(orig_req_id: str, msgid: str, stream_id: str, content: str, finish: bool) -> dict:
    """流式回复:headers.req_id 必须复用原消息回调的 req_id(企微据此关联会话,否则 846605 invalid req_id)。
    同一 stream_id,content 每次为全量(企微客户端整体替换显示)。"""
    return {"cmd": "aibot_respond_msg", "headers": {"req_id": orig_req_id},
            "body": {"msgid": msgid, "aibotid": WECOM_BOT_ID, "msgtype": "stream",
                     "stream": {"id": stream_id, "finish": finish, "content": content or " "}}}


def _frame_send_msg(chatid: str, chat_type: int, markdown: str) -> dict:
    """主动推送 / 流式超时降级。chat_type:1=单聊 2=群聊。"""
    return {"cmd": "aibot_send_msg", "headers": {"req_id": _req_id("send")},
            "body": {"chatid": chatid, "chat_type": chat_type,
                     "msgtype": "markdown", "markdown": {"content": markdown[:MAX_MSG_LEN] or " "}}}


# ── 全量替换式流式输出(对标飞书 streamer 接口,鸭子类型)─────────────────────
class WecomStreamer:
    """企微流式:每次发完整文本(全量替换)。节流 2s(限频 30/min 安全)。
    超过 STREAM_SAFE_SEC 自动降级:finish=true 收口当前流,余下用 aibot_send_msg 分条推。"""

    THROTTLE = 2.0
    BUDGET = MAX_MSG_LEN

    def __init__(self, client: "WecomClient", conv_id: str, chat_type: int, msgid: str, orig_req_id: str):
        self.client = client
        self.conv_id = conv_id
        self.chat_type = chat_type
        self.msgid = msgid
        self.req_id = orig_req_id          # 原消息回调 req_id,流式回复必须复用
        self.stream_id = _req_id("stream")
        self.text = ""
        self.steps: list[str] = []
        self.current_status = ""
        self.has_content = False
        self.partial_mode = False
        self.last_send = 0.0
        self._start = time.monotonic()
        self._degraded = False           # 流式超时后切主动推送
        self._pending: asyncio.Task | None = None

    def _compose(self) -> str:
        sections = []
        if self.steps:
            sections.append("**📋 已完成 %d 步**\n" % len(self.steps) +
                            "\n".join(f"- ✅ {s}" for s in self.steps[-8:]))
        if self.text:
            sections.append(self.text)
        if self.current_status and not self.has_content:
            sections.append(self.current_status)
        body = "\n\n".join(sections) if sections else "⏳ 处理中..."
        if len(body) > self.BUDGET:
            body = body[: self.BUDGET - 1] + "…"
        return body

    async def _flush(self, finish: bool = False):
        # 超时降级:流式快到 6min 硬限 → 先 finish 当前流,后续走主动推送
        if not self._degraded and (time.monotonic() - self._start) > STREAM_SAFE_SEC:
            self._degraded = True
            await self.client.send_json(_frame_respond_stream(
                self.req_id, self.msgid, self.stream_id, self._compose() + "\n\n_（内容较长,完整结果稍后推送）_", True))
            return
        if self._degraded:
            return  # 流已关闭,等 finalize 用主动推送发全文
        await self.client.send_json(_frame_respond_stream(
            self.req_id, self.msgid, self.stream_id, self._compose(), finish))
        self.last_send = time.monotonic()

    async def _schedule(self):
        if self._pending and not self._pending.done():
            return
        elapsed = time.monotonic() - self.last_send
        delay = 0 if elapsed >= self.THROTTLE else (self.THROTTLE - elapsed)

        async def _later():
            try:
                await asyncio.sleep(delay)
                await self._flush()
            except asyncio.CancelledError:
                return
        self._pending = asyncio.create_task(_later())

    # —— claude_core 回调接口 ——
    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        self.text += chunk
        await self._schedule()

    async def set_status(self, line: str):
        if line == self.current_status:
            return
        if self.current_status and not self.current_status.startswith("💭"):
            self.steps.append(self.current_status.removeprefix("🔧 ").strip())
        self.current_status = line
        await self._schedule()

    async def clear_status(self):
        if self.current_status and not self.current_status.startswith("💭"):
            self.steps.append(self.current_status.removeprefix("🔧 ").strip())
        self.current_status = ""
        await self._schedule()

    async def add_thinking(self, chunk: str):
        if chunk:
            self.current_status = "💭 " + chunk[-60:].replace("\n", " ").strip()
            await self._schedule()

    async def set_thinking_progress(self, tokens: int):
        if not self.has_content:
            self.current_status = f"💭 思考中 ~{tokens} tokens"
            await self._schedule()

    async def finalize(self, fallback: str = "", override_text: str = ""):
        if self._pending and not self._pending.done():
            self._pending.cancel()
        if self.current_status and not self.current_status.startswith("💭"):
            self.steps.append(self.current_status.removeprefix("🔧 ").strip())
        self.current_status = ""
        if override_text:
            self.text = override_text.strip()
        elif not self.has_content:
            self.text = (fallback or "").strip() or "✅ 完成（无文字输出）"

        if self._degraded:
            # 流式已超时关闭 → 用主动推送发完整结果(可分条)
            full = self.text
            for i in range(0, len(full), MAX_MSG_LEN):
                await self.client.send_json(
                    _frame_send_msg(self.conv_id, self.chat_type, full[i:i + MAX_MSG_LEN]))
        else:
            await self._flush(finish=True)


# ── 长连接客户端 ──────────────────────────────────────────────────────────
class WecomClient:
    def __init__(self):
        self.ws = None
        self._send_lock = asyncio.Lock()

    async def send_json(self, frame: dict):
        if self.ws is None:
            print("[WARN] ws 未连接,丢弃发送")
            return
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(frame, ensure_ascii=False))
            except Exception as e:
                print(f"[WARN] 发送失败: {e}")

    async def _heartbeat(self):
        """应用层 JSON 心跳(企微不走协议层 ping/pong):每 30s 发一帧。
        帧:{"cmd":"heartbeat","headers":{"req_id":"ping_<ms>"}},响应 req_id 以 ping 开头。"""
        try:
            while True:
                await asyncio.sleep(30)
                await self.send_json({"cmd": "heartbeat",
                                      "headers": {"req_id": f"ping_{int(time.time() * 1000)}"}})
        except asyncio.CancelledError:
            return

    async def run(self):
        backoff = 2
        while True:
            try:
                # 关键:禁用 websockets 协议层 ping(企微非标准,会触发 1002/1011);改用应用层心跳。
                async with websockets.connect(WECOM_WS_URL, ping_interval=None, ping_timeout=None,
                                               max_size=4 * 1024 * 1024) as ws:
                    self.ws = ws
                    await self.send_json(_frame_subscribe())
                    print(f"✅ 已连接 {WECOM_WS_URL},发送订阅 (bot_id={WECOM_BOT_ID[:8]}…)")
                    hb = asyncio.create_task(self._heartbeat())
                    backoff = 2
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    finally:
                        hb.cancel()
            except Exception as e:
                self.ws = None
                # 退避加大(≥2s),避免疯狂重连触发企微订阅频率保护
                print(f"[WARN] 连接断开: {e};{backoff}s 后重连")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _dispatch(self, raw):
        try:
            frame = json.loads(raw)
        except Exception:
            print(f"[WARN] 非 JSON 帧: {str(raw)[:120]}")
            return
        cmd = frame.get("cmd", "")
        if not cmd:
            # 响应帧(订阅/心跳/发送的 ack):无 cmd,按 req_id 前缀分流
            rid = (frame.get("headers", {}) or {}).get("req_id", "") or frame.get("req_id", "")
            if rid.startswith("ping"):
                return  # 心跳 ack,静默(不刷屏)
            ec = frame.get("errcode")
            if ec == 0:
                print("   ✅ 操作成功 (errcode 0) —— 订阅/发送已确认")
            else:
                print(f"   ❌ 操作失败 errcode={ec} {frame.get('errmsg')}")
            return
        if cmd == "aibot_msg_callback":
            asyncio.create_task(self._on_message(frame))
        elif cmd == "aibot_event_callback":
            ev = (frame.get("body", {}).get("event", {}) or {}).get("eventtype", "")
            print(f"   事件: {ev}")
        else:
            print(f"   未处理 cmd: {cmd}")

    async def _on_message(self, frame: dict):
        body = frame.get("body", {}) or {}
        msgid = body.get("msgid", "")
        req_id = (frame.get("headers", {}) or {}).get("req_id", "")  # 回复必须复用此 req_id
        chattype = body.get("chattype", "single")
        chat_type = 2 if chattype == "group" else 1
        # 会话 id:群聊用 chatid,单聊用发送者 userid
        conv_id = body.get("chatid", "") if chat_type == 2 else (body.get("from", {}) or {}).get("userid", "")
        if not conv_id or not msgid:
            return
        _chat_type[conv_id] = chat_type

        msgtype = body.get("msgtype", "")
        if msgtype != "text":
            # TODO: 图片/语音/文件需按 url+aeskey 做 AES 解密下载,二期支持
            await self.send_json(_frame_send_msg(conv_id, chat_type,
                                 f"暂只支持文字消息(收到 {msgtype})"))
            return
        text = ((body.get("text", {}) or {}).get("content", "") or "").strip()
        # 群聊 @ 机器人:去掉开头的 @机器人名,避免污染 prompt
        if chat_type == 2 and text.startswith("@"):
            _sp = text.split(None, 1)
            text = _sp[1].strip() if len(_sp) > 1 else ""
        if not text:
            return
        print(f"[DEBUG] 收到 {chattype} 消息 conv={conv_id} text={text[:50]!r}")

        # 命令
        if text.startswith("/"):
            if await self._handle_command(conv_id, chat_type, msgid, text):
                return

        # 5s 约束:立刻发流式占位,再跑 Claude
        streamer = WecomStreamer(self, conv_id, chat_type, msgid, req_id)
        await streamer._flush()  # 占位 "⏳ 处理中..."(finish=false)
        session = _get_session(conv_id)
        try:
            response = await session.run_claude(text, streamer)
        except Exception as e:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
            return
        await streamer.finalize(fallback=response)

    async def _reply_text(self, conv_id: str, chat_type: int, text: str):
        """命令的简单文本回复:走主动推送(用户刚发过消息,允许)。"""
        await self.send_json(_frame_send_msg(conv_id, chat_type, text))

    async def _handle_command(self, conv_id: str, chat_type: int, msgid: str, text: str) -> bool:
        text = text.strip()
        session = _get_session(conv_id)
        if text.startswith("/new"):
            session.set_new_session()
            await self._reply_text(conv_id, chat_type, "🔄 下一条消息将开启全新对话")
        elif text.startswith("/sessions"):
            ss = list_sessions(10, cwd=session.cwd)
            if not ss:
                await self._reply_text(conv_id, chat_type, f"❌ 无历史会话\n📁 {session.cwd}")
                return True
            lines = ["📋 最近会话（/resume <编号>）\n"]
            for i, s in enumerate(ss, 1):
                t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                lines.append(f"{i}. [{t}] {s['summary'] or '（无内容）'}\n    🔖 {s['id']}")
            await self._reply_text(conv_id, chat_type, "\n".join(lines))
        elif text.startswith("/resume"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self._reply_text(conv_id, chat_type, "用法：/resume <编号|sessionId>")
                return True
            arg = parts[1].strip()
            if arg.isdigit():
                ss = list_sessions(10, cwd=session.cwd)
                idx = int(arg) - 1
                if 0 <= idx < len(ss):
                    session.set_resume_session(ss[idx]["id"])
                    await self._reply_text(conv_id, chat_type, f"✅ 已切到会话 {idx+1}\n🔖 {ss[idx]['id']}")
                else:
                    await self._reply_text(conv_id, chat_type, f"❌ 编号超范围（共 {len(ss)}）")
            elif _session_file_exists(arg):
                session.set_resume_session(arg)
                await self._reply_text(conv_id, chat_type, f"✅ 已切到会话\n🔖 {arg}")
            else:
                await self._reply_text(conv_id, chat_type, f"❌ 未找到 session：{arg}")
        elif text.startswith("/model"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self._reply_text(conv_id, chat_type, f"🤖 当前模型：{session.model or '默认'}\n切换：/model opus|sonnet|haiku|default")
                return True
            name = parts[1].strip()
            session.set_model(None if name in ("default", "默认", "reset") else name)
            await self._reply_text(conv_id, chat_type, f"✅ 模型：{name}（下条生效）")
        elif text.startswith("/mode"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self._reply_text(conv_id, chat_type, f"🔐 当前模式：{session.mode}\n切换：/mode bypass|plan|default|accept")
                return True
            ok = session.set_mode(parts[1].strip().lower())
            await self._reply_text(conv_id, chat_type, f"✅ 模式：{parts[1].strip()}" if ok else "❌ 未知模式")
        elif text.startswith("/stop"):
            killed = await session.stop()
            await self._reply_text(conv_id, chat_type, "🛑 已中断" if killed else "ℹ️ 无运行中任务")
        elif text.startswith("/status"):
            await self._reply_text(conv_id, chat_type,
                f"📊 目录 {session.cwd}\n🤖 模型 {session.model or '默认'}\n🔐 模式 {session.mode}\n🔖 {session.current_session_id or '（新会话）'}")
        elif text.startswith("/start"):
            await self._reply_text(conv_id, chat_type,
                "🤖 Claude Code 企微网关已就绪\n支持文字消息\n/new /sessions /resume /model /mode /stop /status")
        else:
            return False  # 未注册 /xxx → 透传给 Claude
        return True


def main():
    print("🤖 Claude Code 企业微信网关启动中（长连接）...")
    print(f"   Bot ID: {WECOM_BOT_ID[:8]}…  WS: {WECOM_WS_URL}")
    if _BOT_PERSONA:
        print(f"   🎭 人设: {_BOT_NAME or '(未命名)'}（{len(_BOT_PERSONA)} 字）")
    if _RELAY_ENABLED:
        print(f"   🔗 relay 开启,队友: {_RELAY_TEAMMATES}")
    asyncio.run(WecomClient().run())


if __name__ == "__main__":
    main()
