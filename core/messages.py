"""统一入站消息模型 —— adapter 把各平台消息解析成它,Gateway 消费它。"""
from dataclasses import dataclass, field


@dataclass
class InboundMessage:
    conv_id: str                       # 会话隔离 key(飞书 open_id/chat_id、企微 chatid/userid)
    text: str                          # 文本(已去 @ 前缀 / 接力跳数标记)
    platform: str                      # "feishu" | "wecom" | "telegram" | ...
    chat_type: str = "private"         # "private" | "group"
    user_id: str = ""                  # 发送者标识
    images: list = field(default_factory=list)   # 已下载到本地的图片路径
    raw: dict = field(default_factory=dict)      # 平台原始帧(回复时取 req_id/msgid/receive_id_type 等)

    @property
    def is_command(self) -> bool:
        return self.text.strip().startswith("/")

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"
