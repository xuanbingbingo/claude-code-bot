"""WecomAdapter —— 企业微信智能机器人长连接适配器。
长连接 aibot_subscribe → 收 aibot_msg_callback → 构造 InboundMessage → gateway.handle;
流式回复 aibot_respond_msg(req_id 复用)、主动推送 aibot_send_msg、应用层心跳、清代理直连。
"""
import asyncio
import json
import os
import time
import uuid

import websockets

from core.messages import InboundMessage
from adapters.base import PlatformAdapter
from .streamer import WecomStreamer

# 企微长连接需直连(websockets 会读 HTTPS_PROXY 走代理被重置)
for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_pk, None)


class WecomAdapter(PlatformAdapter):
    name = "wecom"

    def __init__(self, config=None):
        self.bot_id = os.environ.get("WECOM_BOT_ID", "")
        self.secret = os.environ.get("WECOM_SECRET", "")
        self.ws_url = os.environ.get("WECOM_WS_URL", "wss://openws.work.weixin.qq.com")
        self.stream_safe = int(os.environ.get("WECOM_STREAM_SAFE_SEC", "300"))
        if not self.bot_id or not self.secret:
            raise SystemExit("❌ 缺少 WECOM_BOT_ID / WECOM_SECRET")
        self.ws = None
        self._send_lock = asyncio.Lock()
        self._gateway = None

    def state_key(self):
        return self.bot_id      # 企微 bot_id 唯一稳定非敏感,按 bot 隔离会话指针文件

    # ---- 帧构造 ----
    def _frame_subscribe(self):
        return {"cmd": "aibot_subscribe", "headers": {"req_id": f"aibot_subscribe_{int(time.time()*1000)}"},
                "body": {"bot_id": self.bot_id, "secret": self.secret}}

    def _frame_respond(self, req_id, msgid, stream_id, content, finish):
        return {"cmd": "aibot_respond_msg", "headers": {"req_id": req_id},
                "body": {"msgid": msgid, "aibotid": self.bot_id, "msgtype": "stream",
                         "stream": {"id": stream_id, "finish": finish, "content": content or " "}}}

    def _frame_send(self, chatid, chat_type_int, markdown):
        return {"cmd": "aibot_send_msg", "headers": {"req_id": f"send_{uuid.uuid4().hex[:16]}"},
                "body": {"chatid": chatid, "chat_type": chat_type_int, "msgtype": "markdown",
                         "markdown": {"content": markdown[:3800] or " "}}}

    async def _ws_send(self, frame):
        if self.ws is None:
            return
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(frame, ensure_ascii=False))
            except Exception as e:
                print(f"[WARN] 发送失败: {e}")

    # ---- PlatformAdapter 接口 ----
    def make_streamer(self, inbound):
        return WecomStreamer(self, inbound, self.stream_safe)

    async def send_text(self, conv_id, chat_type, text):
        await self._ws_send(self._frame_send(conv_id, 2 if chat_type == "group" else 1, text))

    # ---- streamer 调用的发送 ----
    async def send_stream(self, req_id, msgid, stream_id, content, finish):
        await self._ws_send(self._frame_respond(req_id, msgid, stream_id, content, finish))

    async def send_active(self, conv_id, chat_type_int, markdown):
        await self._ws_send(self._frame_send(conv_id, chat_type_int, markdown))

    # ---- 连接 ----
    async def connect(self, gateway):
        self._gateway = gateway
        backoff = 2
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None, ping_timeout=None,
                                               max_size=4 * 1024 * 1024) as ws:
                    self.ws = ws
                    await self._ws_send(self._frame_subscribe())
                    print(f"✅ 已连接 {self.ws_url},订阅 (bot_id={self.bot_id[:8]}…)")
                    hb = asyncio.create_task(self._heartbeat())
                    backoff = 2
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    finally:
                        hb.cancel()
            except Exception as e:
                self.ws = None
                print(f"[WARN] 连接断开: {e};{backoff}s 后重连")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(30)
                await self._ws_send({"cmd": "heartbeat",
                                     "headers": {"req_id": f"ping_{int(time.time()*1000)}"}})
        except asyncio.CancelledError:
            return

    def _dispatch(self, raw):
        try:
            frame = json.loads(raw)
        except Exception:
            return
        cmd = frame.get("cmd", "")
        if not cmd:
            rid = (frame.get("headers", {}) or {}).get("req_id", "")
            if rid.startswith("ping"):
                return
            ec = frame.get("errcode")
            print("   ✅ 订阅/发送已确认" if ec == 0 else f"   ❌ errcode={ec} {frame.get('errmsg')}")
            return
        if cmd == "aibot_msg_callback":
            asyncio.create_task(self._on_message(frame))
        elif cmd == "aibot_event_callback":
            print("   事件:", (frame.get("body", {}).get("event", {}) or {}).get("eventtype", ""))

    async def _on_message(self, frame):
        body = frame.get("body", {}) or {}
        req_id = (frame.get("headers", {}) or {}).get("req_id", "")
        msgid = body.get("msgid", "")
        chattype = body.get("chattype", "single")
        chat_type = "group" if chattype == "group" else "private"
        conv_id = body.get("chatid", "") if chat_type == "group" else (body.get("from", {}) or {}).get("userid", "")
        if not conv_id or not msgid:
            return
        if body.get("msgtype") != "text":
            await self.send_text(conv_id, chat_type, f"暂只支持文字消息(收到 {body.get('msgtype')})")
            return
        text = ((body.get("text", {}) or {}).get("content", "") or "").strip()
        if chat_type == "group" and text.startswith("@"):     # 去掉 @机器人名
            sp = text.split(None, 1)
            text = sp[1].strip() if len(sp) > 1 else ""
        if not text:
            return
        print(f"[DEBUG] 收到 {chattype} 消息 conv={conv_id} text={text[:50]!r}")
        raw = dict(body)
        raw["_req_id"] = req_id
        inbound = InboundMessage(conv_id=conv_id, text=text, platform="wecom", chat_type=chat_type,
                                 user_id=(body.get("from", {}) or {}).get("userid", ""), raw=raw)
        await self._gateway.handle(inbound)
