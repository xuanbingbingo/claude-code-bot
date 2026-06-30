"""飞书 bot 间接力 —— 名册(从群消息 mentions 被动积累)+ try_relay 钩子。

名册落盘到共享文件 roster-cache.json,结构 {app_id: {chat_id: {名字: open_id}}}:
- 按 app_id 分区:open_id 是飞书按 app 隔离的,同一个人各 bot 看到的 open_id 不同,混用会 @ 错人。
- 单文件多进程共写:flock 跨进程独占锁 + 「读整体-改本段-临时文件-原子替换」,防并发写坏。
重启即从文件恢复,不再失忆(否则名册清空 → try_relay 拿不到 open_id → 静默 @ 不到人)。
"""
import asyncio
import json
import os
import threading

try:
    import fcntl
except ImportError:                       # 非 POSIX(理论上)退化为仅进程内锁
    fcntl = None

# 落盘在项目根目录(本文件在 adapters/feishu/ 下,需上溯三级),与旧版/各 bot 共用同一文件
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROSTER_FILE = os.path.join(_ROOT, "roster-cache.json")
_ROSTER_LOCK = threading.Lock()           # 进程内线程锁(flock 管跨进程)


class FeishuRelay:
    def __init__(self, api):
        self.api = api
        self.app_id = getattr(api, "app_id", "")
        self.roster: dict = {}            # chat_id → {显示名: open_id}
        self._load_roster()

    # ---- 名册持久化 ----
    def _read_file(self) -> dict:
        """读整个 roster-cache.json;坏/缺返回 {}。"""
        try:
            if os.path.isfile(_ROSTER_FILE):
                with open(_ROSTER_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception as e:
            print(f"[WARN] 读名册文件失败: {e}")
        return {}

    def _load_roster(self) -> None:
        """把共享文件里【本 app】那段合并进内存(不清空,只补充)。"""
        mine = self._read_file().get(self.app_id, {})
        if isinstance(mine, dict):
            for chat, members in mine.items():
                if isinstance(members, dict):
                    self.roster.setdefault(chat, {}).update(members)
        if self.roster:
            print(f"   📇 名册已恢复: {sum(len(v) for v in self.roster.values())} 个成员")

    def _save_roster(self) -> None:
        """读整体 → 只改本 app 段 → 临时文件 → 原子替换;flock 把读-改-写在所有 bot 进程间串行化。"""
        with _ROSTER_LOCK:
            lockpath = f"{_ROSTER_FILE}.lock"
            lf = None
            try:
                lf = open(lockpath, "w")
            except Exception as e:
                print(f"[WARN] 名册锁打开失败: {e}")
            try:
                if lf and fcntl:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                data = self._read_file()              # 锁内重读,拿别人最新写的
                data[self.app_id] = self.roster
                tmp = f"{_ROSTER_FILE}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, _ROSTER_FILE)
            except Exception as e:
                print(f"[WARN] 保存名册失败: {e}")
            finally:
                if lf:
                    try:
                        if fcntl:
                            fcntl.flock(lf, fcntl.LOCK_UN)
                        lf.close()
                    except Exception:
                        pass

    def harvest(self, chat_id: str, mentions: list) -> None:
        """把消息 mentions 里的 名字→open_id 学进本群名册;有新增/变更才落盘。"""
        if not chat_id:
            return
        book = self.roster.setdefault(chat_id, {})
        changed = False
        for m in mentions or []:
            name = m.get("name", "")
            oid = m.get("open_id", "")
            if name and oid and book.get(name) != oid:
                book[name] = oid
                changed = True
        if changed:
            self._save_roster()

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
