"""Gateway —— 三层汇合的编排器。
adapter 收到平台消息 → 构造 InboundMessage → await gateway.handle(inbound):
  命令 → CommandRouter;否则 → backend.run(streamer) 流式 → relay 接力 → finalize。
平台与后端都通过接口注入,Gateway 本身平台无关、后端无关。
"""


class Gateway:
    def __init__(self, adapter, session_manager, command_router, relay):
        self.adapter = adapter
        self.sessions = session_manager
        self.commands = command_router
        self.relay = relay

    async def handle(self, inbound):
        backend = self.sessions.get_or_create(inbound.conv_id, inbound.is_group)

        # 平台环境注入(飞书 send-file 需要发起人 id + bot 凭证;其他平台默认空)
        env = self.adapter.backend_env(inbound)
        if env:
            backend.inject_env(env)

        # 命令(已处理则结束)
        if inbound.is_command and await self.commands.dispatch(inbound, backend, self.adapter):
            # /new /resume /cwd 等会改变会话指针 → 落盘(persist 内部对未变化的会话自动跳过)
            self.sessions.persist(inbound.conv_id)
            return

        # 跑智能体 + 流式回复
        streamer = self.adapter.make_streamer(inbound)
        try:
            await streamer.first_frame()
        except Exception:
            pass
        try:
            if inbound.images:
                resp = await backend.run_with_image(
                    inbound.images[0], inbound.text or "请描述这张图片的内容", streamer)
            else:
                resp = await backend.run(inbound.text, streamer)
        except Exception as e:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
            return

        # 本轮会话指针落盘:current_session_id 已被 backend.run 刷成最新,重启后据此自动接回
        self.sessions.persist(inbound.conv_id)

        # bot 间接力(群聊 + 开启 relay + 平台支持):已另发带 <at> 的独立消息 → 撤回流式卡片,只留一条
        if await self.relay.maybe_relay(inbound, resp, self.adapter):
            await streamer.discard()
        else:
            await streamer.finalize(fallback=resp)
