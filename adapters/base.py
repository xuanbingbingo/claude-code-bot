"""PlatformAdapter —— 平台适配层抽象。
每个平台实现:建连接+收消息循环(connect)、为消息建 streamer(make_streamer)、
命令的简单文本回复(send_text)。可选:backend_env(平台凭证注入)、tool_prompt(平台工具提示)、
try_relay(bot 间接力)。
"""
from abc import ABC, abstractmethod


class PlatformAdapter(ABC):
    name = "base"

    @abstractmethod
    async def connect(self, gateway) -> None:
        """建立连接、进入收消息循环;每条消息构造 InboundMessage 后 await gateway.handle(inbound)。"""
        ...

    @abstractmethod
    def make_streamer(self, inbound):
        """为一条入站消息创建平台 streamer(StreamerBase 子类)。"""
        ...

    @abstractmethod
    async def send_text(self, conv_id: str, chat_type: str, text: str) -> None:
        """命令/系统提示的简单文本回复。"""
        ...

    # ---- 可选钩子(默认空实现)----
    def backend_env(self, inbound) -> dict:
        """注入给 backend 的平台环境变量(飞书 send-file 需要发起人 id + 凭证)。"""
        return {}

    def tool_prompt(self) -> str:
        """平台工具的 system prompt 提示(飞书 send-file);拼进人设。"""
        return ""

    def state_key(self) -> str:
        """本 bot 的唯一稳定标识,用于会话指针持久化文件名(.sessions-<key>.json)隔离。
        必须每个 bot 进程唯一且跨重启稳定、非敏感(进文件名)。返回 "" 则关闭持久化。"""
        return ""

    # try_relay(inbound, response, relay_manager) -> bool 可选;飞书实现,其它不实现
