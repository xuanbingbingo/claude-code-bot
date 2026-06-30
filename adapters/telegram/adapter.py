"""TelegramAdapter —— python-telegram-bot polling 适配器(集成进主 asyncio loop)。
text 为主;图片/语音可后续按 _on_msg 扩展。会话隔离按 chat_id。
"""
import os

from telegram.ext import Application, MessageHandler, filters

from core.messages import InboundMessage
from adapters.base import PlatformAdapter
from .streamer import TelegramStreamer


class TelegramAdapter(PlatformAdapter):
    name = "telegram"

    def __init__(self, config=None):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.token:
            raise SystemExit("❌ 缺少 TELEGRAM_BOT_TOKEN")
        self.proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
        self._app = None
        self._gateway = None

    def state_key(self):
        # token 形如 <botid>:<authstring>,冒号前的 botid 唯一稳定且非敏感,用它隔离(避免把密钥写进文件名)
        return self.token.split(":", 1)[0]

    def make_streamer(self, inbound):
        return TelegramStreamer(inbound.raw["_reply_msg"])

    async def send_text(self, conv_id, chat_type, text):
        await self._app.bot.send_message(chat_id=int(conv_id), text=text[:4000])

    async def connect(self, gateway):
        self._gateway = gateway
        builder = Application.builder().token(self.token).connect_timeout(30).read_timeout(30)
        if self.proxy:
            builder = builder.proxy(self.proxy).get_updates_proxy(self.proxy)
        self._app = builder.build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_msg))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_msg))
        print(f"✅ Telegram polling 启动 (token={self.token[:8]}…)")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        import asyncio
        await asyncio.Event().wait()      # 保持运行

    async def _on_msg(self, update, context):
        msg = update.message
        if not msg or not msg.text:
            return
        placeholder = await msg.reply_text("⏳ 处理中...")
        inbound = InboundMessage(conv_id=str(msg.chat_id), text=msg.text, platform="telegram",
                                 chat_type="private", user_id=str(msg.from_user.id if msg.from_user else ""),
                                 raw={"_reply_msg": placeholder})
        await self._gateway.handle(inbound)
