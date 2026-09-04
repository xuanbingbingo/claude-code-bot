"""Gateway —— 三层汇合的编排器。
adapter 收到平台消息 → 构造 InboundMessage → await gateway.handle(inbound):
  命令 → CommandRouter;否则 → backend.run(streamer) 流式 → relay 接力 → finalize。
平台与后端都通过接口注入,Gateway 本身平台无关、后端无关。

⚠️ 同会话并发 = 抢占,不是排队(_RunSlot):
同一个 conv_id 共用同一个 backend(同一个 ClaudeSession / 同一条 claude 会话),两轮真并发
就会有两个 claude 进程往同一个 jsonl 交织写、current_session_id 互相覆盖。所以后到的消息
一律抢占前一轮:先 bump epoch(让旧轮自己作废)+ stop() 杀掉已 spawn 的进程,再抢 slot 锁
等旧轮彻底退干净才开跑。旧轮进程还没 spawn 时 stop() 是 no-op,靠 epoch 兜住 —— 这正是
「抢跑窗口」(handle 进来到 subprocess 起来之间隔着一次发卡片的网络往返)的堵法。
被抢占的那轮不许静默收尾:半截内容留在卡片上但显式标注中断,否则半截答案会冒充完整答案。
"""
import asyncio


class _RunSlot:
    """每个 conv_id 一个:串行锁 + 轮次号 + 作废原因。"""
    __slots__ = ("lock", "epoch", "note")

    def __init__(self):
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.note = ""

    def bump(self, note: str) -> int:
        """作废当前在跑的那轮,返回新轮次号。"""
        self.epoch += 1
        self.note = note
        return self.epoch


class Gateway:
    PREEMPT_NOTE = "⏹ 已被新消息中断"
    STOP_NOTE = "🛑 已手动中断（/stop）"

    def __init__(self, adapter, session_manager, command_router, relay):
        self.adapter = adapter
        self.sessions = session_manager
        self.commands = command_router
        self.relay = relay
        self._slots: dict[str, _RunSlot] = {}

    def _slot(self, conv_id: str) -> _RunSlot:
        slot = self._slots.get(conv_id)
        if slot is None:
            slot = _RunSlot()
            self._slots[conv_id] = slot
        return slot

    async def handle(self, inbound):
        backend = self.sessions.get_or_create(inbound.conv_id, inbound.is_group)
        slot = self._slot(inbound.conv_id)

        # 平台环境注入(飞书 send-file 需要发起人 id + bot 凭证;其他平台默认空)
        env = self.adapter.backend_env(inbound)
        if env:
            backend.inject_env(env)

        # 命令不进锁:长任务跑着时 /stop 必须能立刻生效,排在它后面等于自己把自己堵死。
        # /stop 会真杀掉在跑的进程 → 顺手作废那一轮,让它的卡片收尾成「已手动中断」而不是空回复。
        if inbound.is_command:
            if inbound.text.strip().startswith("/stop"):
                slot.bump(self.STOP_NOTE)
            if await self.commands.dispatch(inbound, backend, self.adapter):
                # /new /resume /cwd 等会改变会话指针 → 落盘(persist 内部对未变化的会话自动跳过)
                self.sessions.persist(inbound.conv_id)
                return

        # 抢占上一轮:先作废(没 spawn 的靠 epoch 自己退)再杀(已 spawn 的立刻断流)
        my_epoch = slot.bump(self.PREEMPT_NOTE)
        if slot.lock.locked():
            try:
                await backend.stop()
            except Exception as e:
                print(f"[WARN] 抢占前中断上一轮失败:{e}")

        # 等上一轮彻底退干净,保证同一条 claude 会话同一时刻只有一个进程
        async with slot.lock:
            if my_epoch != slot.epoch:
                return                      # 等锁期间又被更新的消息抢占,本轮直接作废

            streamer = self.adapter.make_streamer(inbound)
            try:
                await streamer.first_frame()
            except Exception:
                pass
            # 发卡片是一次网络往返,这期间可能又来消息 —— spawn 前最后一道闸
            if my_epoch != slot.epoch:
                await streamer.interrupted(slot.note)
                return

            try:
                if inbound.images:
                    resp = await backend.run_with_image(
                        inbound.images[0], inbound.text or "请描述这张图片的内容", streamer)
                else:
                    resp = await backend.run(inbound.text, streamer)
            except Exception as e:
                await streamer.finalize(fallback=f"❌ 出错了：{e}")
                return
            finally:
                # 本轮会话指针落盘:成功失败都落——异常路径往往也已拿到新 session_id,
                # 不落盘的话进程一重启就接不回,正是「bot 突然失忆」的来源之一。
                # 被抢占那轮同样要落:它的 session_id 就是下一轮 --resume 要接的那条。
                self.sessions.persist(inbound.conv_id)

            # 被抢占 → backend.run 是被杀出来的,resp 为空,不能按正常收尾冒充完整答案
            if my_epoch != slot.epoch:
                await streamer.interrupted(slot.note)
                return

            # bot 间接力(群聊 + 开启 relay + 平台支持):已另发带 <at> 的独立消息 → 撤回流式卡片,只留一条
            if await self.relay.maybe_relay(inbound, resp, self.adapter):
                await streamer.discard()
            else:
                await streamer.finalize(fallback=resp)
