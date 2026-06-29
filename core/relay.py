"""RelayManager —— bot 间协作(群里 @ 队友接力)的平台无关框架。

core 只管:队友配置、注入"你有哪些队友"的 system_prompt、跳数解析、触发点。
平台特定的"把 @队友名 解析成真实 id 并发出接力消息"由 adapter.try_relay 钩子实现
(飞书:名册 + <at user_id>;企微:官方不支持 bot→bot 回调,no-op)。
"""
import re

_HOP_RE = re.compile(r"〔接力\s*(\d+)/\d+〕")


class RelayManager:
    def __init__(self, enabled: bool, max_hops: int, teammates: dict):
        self.enabled = enabled
        self.max_hops = max_hops
        self.teammates = teammates          # 别名 → 显示名

    def system_prompt(self) -> str:
        if not (self.enabled and self.teammates):
            return ""
        mates = "、".join(self.teammates.keys())
        return (
            f"你在一个多 bot 协作群里,队友有:{mates}。完成自己这一棒后,"
            "在回复最后单独一行用 `@队友名 交代的话` 把任务传下去;彻底完结才不 @。每条最多 @ 一位。"
        )

    @staticmethod
    def parse_hop(text: str) -> int:
        m = _HOP_RE.search(text or "")
        return int(m.group(1)) if m else 0

    @staticmethod
    def strip_hop_tag(text: str) -> str:
        return _HOP_RE.sub("", text or "").strip()

    async def maybe_relay(self, inbound, response: str, adapter) -> bool:
        """若开启且群聊,委托 adapter.try_relay 做平台特定接力;返回是否已接力。"""
        if not (self.enabled and self.teammates and inbound.is_group and response):
            return False
        fn = getattr(adapter, "try_relay", None)
        if fn is None:
            return False
        return await fn(inbound, response, self)
