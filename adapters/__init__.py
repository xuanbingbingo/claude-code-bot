"""平台适配器工厂(惰性 import,只装当前平台的依赖)。"""


def make_adapter(name: str, config=None):
    if name == "feishu":
        from .feishu.adapter import FeishuAdapter
        return FeishuAdapter(config)
    if name == "wecom":
        from .wecom.adapter import WecomAdapter
        return WecomAdapter(config)
    if name == "telegram":
        from .telegram.adapter import TelegramAdapter
        return TelegramAdapter(config)
    if name == "dingtalk":
        from .dingtalk.adapter import DingtalkAdapter
        return DingtalkAdapter(config)
    raise SystemExit(f"❌ 未知 BOT_PLATFORM: {name}（可选 feishu / wecom / telegram / dingtalk）")
