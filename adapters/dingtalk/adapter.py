"""DingtalkAdapter —— 钉钉适配器模板(留接口,待实现)。

钉钉支持 Stream 模式长连接(wss,类似企微):用 AppKey+AppSecret 获取连接,
收 /v1.0/im 机器人消息回调,回复用 webhook/sessionWebhook。实现时参考 adapters/wecom:
  - __init__:读 DINGTALK_APP_KEY / DINGTALK_APP_SECRET
  - connect:建 Stream 长连接 + 心跳 + 收消息 → InboundMessage → gateway.handle
  - make_streamer:钉钉 streamer(钉钉 markdown 消息;流式可用 AI 卡片更新接口)
  - send_text:sessionWebhook 发 markdown
官方:open.dingtalk.com/document(Stream 模式 / AI 助理卡片)。
"""
from adapters.base import PlatformAdapter


class DingtalkAdapter(PlatformAdapter):
    name = "dingtalk"

    def __init__(self, config=None):
        raise SystemExit(
            "钉钉适配器尚未实现(模板见 adapters/dingtalk/adapter.py)。\n"
            "钉钉 Stream 模式长连接与企微相似,可照 adapters/wecom 实现 connect/make_streamer/send_text。")

    async def connect(self, gateway):
        raise NotImplementedError

    def make_streamer(self, inbound):
        raise NotImplementedError

    async def send_text(self, conv_id, chat_type, text):
        raise NotImplementedError
