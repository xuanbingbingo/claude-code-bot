"""飞书 bot 间接力 —— 名册(从群消息 mentions 被动积累)+ try_relay 钩子。
比旧版精简:内存名册(单进程);跨 bot 共享 open_id 的 flock 文件持久化留作后续。
"""
import asyncio


class FeishuRelay:
    def __init__(self, api):
        self.api = api
        self.roster: dict = {}        # chat_id → {显示名: open_id}

    def harvest(self, chat_id: str, mentions: list) -> None:
        """把消息 mentions 里的 名字→open_id 学进本群名册。"""
        if not chat_id:
            return
        book = self.roster.setdefault(chat_id, {})
        for m in mentions or []:
            name = m.get("name", "")
            oid = m.get("open_id", "")
            if name and oid:
                book[name] = oid

    async def try_relay(self, inbound, response: str, relay_mgr) -> bool:
        """回复里 @ 了队友且名册有其 open_id、未超跳数 → 发接力消息(<at>)。"""
        chat_id = inbound.conv_id
        hop = int(inbound.raw.get("_hop", 0))
        if hop >= relay_mgr.max_hops:
            return False
        roster = self.roster.get(chat_id, {})
        repl: dict = {}
        for alias, disp in relay_mgr.teammates.items():
            oid = roster.get(disp) or roster.get(alias)
            mentioned = [nm for nm in {alias, disp} if f"@{nm}" in response]
            if mentioned and oid:
                for nm in mentioned:
                    repl[nm] = oid
        if not repl:
            return False
        out = response
        for name, oid in repl.items():
            out = out.replace(f"@{name}", f'<at user_id="{oid}"></at>')
        out += f"\n〔接力 {hop + 1}/{relay_mgr.max_hops}〕"
        try:
            await asyncio.to_thread(self.api.send_message, chat_id, "text", {"text": out}, "chat_id")
            print(f"[RELAY] hop {hop}->{hop+1} @ {list(repl)}")
            return True
        except Exception as e:
            print(f"[WARN] relay 发送失败: {e}")
            return False
