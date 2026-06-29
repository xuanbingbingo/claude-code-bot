"""FeishuAdapter —— 飞书 lark_oapi WsClient 长连接适配器。
WsClient.start() 同步阻塞 → 跑在 to_thread;消息事件在 lark sdk 线程触发,解析成
InboundMessage 后用 run_coroutine_threadsafe 投递到主 loop 的 gateway.handle。
支持 text / image / audio / post;名册与 @ 接力委托 FeishuRelay。
"""
import asyncio
import json
import os
import tempfile
import threading

from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel

from core.messages import InboundMessage
from core.relay import RelayManager
from adapters.base import PlatformAdapter
import claude_core                       # 根目录唯一核心(transcribe_audio)
from .api import FeishuAPI
from .streamer import FeishuStreamer
from .relay import FeishuRelay

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FeishuAdapter(PlatformAdapter):
    name = "feishu"

    def __init__(self, config=None):
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.verify_token = os.environ.get("FEISHU_VERIFY_TOKEN", "")
        if not self.app_id or not self.app_secret:
            raise SystemExit("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
        self.api = FeishuAPI(self.app_id, self.app_secret)
        self.relay = FeishuRelay(self.api)
        self._gateway = None
        self._loop = None

    # ---- PlatformAdapter 接口 ----
    def make_streamer(self, inbound):
        return FeishuStreamer(self.api, inbound.conv_id, inbound.raw.get("_receive_id_type", "open_id"))

    async def send_text(self, conv_id, chat_type, text):
        rt = "chat_id" if chat_type == "group" else "open_id"
        await asyncio.to_thread(self.api.send_message, conv_id, "text", {"text": text}, rt)

    def backend_env(self, inbound):
        return {"FEISHU_SENDER_OPEN_ID": inbound.conv_id,
                "FEISHU_SENDER_ID_TYPE": inbound.raw.get("_receive_id_type", "open_id"),
                "FEISHU_APP_ID": self.app_id, "FEISHU_APP_SECRET": self.app_secret}

    def tool_prompt(self):
        tool = os.path.join(_ROOT, "tools", "feishu-send-file.py")
        if not os.path.isfile(tool):
            return ""
        return (f"你运行在飞书机器人网关里。当用户要求把本地文件/图片/视频发到飞书,直接执行:\n"
                f"    python3 {tool} <文件绝对路径>\n"
                "不传收件人时自动发到当前对话窗口。仅在用户明确要发文件时调用。")

    async def try_relay(self, inbound, response, relay_mgr):
        return await self.relay.try_relay(inbound, response, relay_mgr)

    # ---- 连接(WsClient.start 阻塞 → to_thread)----
    async def connect(self, gateway):
        self._gateway = gateway
        self._loop = asyncio.get_running_loop()
        handler = (EventDispatcherHandler.builder(encrypt_key="", verification_token=self.verify_token or "")
                   .register_p2_im_message_receive_v1(self._on_event).build())
        client = WsClient(app_id=self.app_id, app_secret=self.app_secret,
                          event_handler=handler, log_level=LogLevel.INFO)
        print(f"✅ 飞书长连接启动 (app_id={self.app_id[:8]}…)")
        await asyncio.to_thread(client.start)

    def _on_event(self, event):
        threading.Thread(target=self._process, args=(event,), daemon=True).start()

    def _process(self, event):
        try:
            inbound = self._parse(event)
            if inbound:
                asyncio.run_coroutine_threadsafe(self._gateway.handle(inbound), self._loop)
        except Exception as e:
            import traceback
            print(f"❌ 解析消息出错: {e}")
            traceback.print_exc()

    # ---- 解析事件 → InboundMessage ----
    def _parse(self, event):
        data = event.event
        message = data.message
        msg_type = message.message_type
        message_id = message.message_id or ""
        sender_id = data.sender.sender_id.open_id if data.sender and data.sender.sender_id else ""
        content = json.loads(message.content) if message.content else {}
        chat_type = getattr(message, "chat_type", "") or ""
        chat_id = getattr(message, "chat_id", "") or ""
        if chat_type == "group" and chat_id:
            conv_id, recv_type, ctype = chat_id, "chat_id", "group"
        else:
            conv_id, recv_type, ctype = sender_id, "open_id", "private"

        mentions = []
        for m in (getattr(message, "mentions", None) or []):
            mid = getattr(m, "id", None)
            mentions.append({"name": getattr(m, "name", "") or "",
                             "open_id": getattr(mid, "open_id", "") if mid else "",
                             "key": getattr(m, "key", "") or ""})
        if chat_id:
            self.relay.harvest(chat_id, mentions)

        text, images = "", []
        if msg_type == "text":
            text = content.get("text", "")
            for m in mentions:
                if m["key"]:
                    text = text.replace(m["key"], "")
        elif msg_type == "post":
            text, _imgs = self._extract_post(content)
        elif msg_type == "image":
            p = self._download(message_id, content.get("image_key", ""), "image", ".jpg")
            if p:
                images.append(p)
        elif msg_type == "audio":
            text = self._transcribe(message_id, content.get("file_key", ""))
        else:
            return None

        hop = RelayManager.parse_hop(text)
        text = RelayManager.strip_hop_tag(text).strip()
        if not text and not images:
            return None
        raw = {"_receive_id_type": recv_type, "message_id": message_id, "_hop": hop}
        return InboundMessage(conv_id=conv_id, text=text, platform="feishu", chat_type=ctype,
                              user_id=sender_id, images=images, raw=raw)

    # ---- 媒体辅助 ----
    def _download(self, message_id, file_key, rtype, suffix):
        if not file_key:
            return None
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            path = tmp.name
        return path if self.api.download_resource(message_id, file_key, rtype, path) else None

    def _transcribe(self, message_id, file_key):
        path = self._download(message_id, file_key, "file", ".opus")
        if not path:
            return ""
        try:
            return claude_core.transcribe_audio(path) or ""
        except Exception as e:
            print(f"[WARN] 语音识别失败: {e}")
            return ""
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    @staticmethod
    def _extract_post(content):
        texts, imgs = [], []

        def walk(node):
            if isinstance(node, dict):
                tag = node.get("tag")
                if tag in ("text", "a", "md") and node.get("text"):
                    texts.append(node["text"])
                elif tag == "img" and node.get("image_key"):
                    imgs.append(node["image_key"])
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        walk(v)
            elif isinstance(node, list):
                for it in node:
                    walk(it)

        title = content.get("title", "") if isinstance(content, dict) else ""
        walk(content)
        return ((f"{title} " if title else "") + " ".join(texts)).strip(), imgs
