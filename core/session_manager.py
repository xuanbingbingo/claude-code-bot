"""SessionManager —— conv_id → AgentBackend 实例,会话隔离 + 跨重启会话续接。

backend_factory 由 run.py 注入(造 ClaudeCodeBackend / HermesBackend,已带好 cwd+人设)。

跨重启续接:进程重启后内存里的 conv_id→backend 全丢,默认每个会话首条消息会开新会话。
为此按「bot 唯一标识」(state_key,如飞书 app_id)隔离落盘一份 {conv_id: session_id} 指针,
get_or_create 首次建 backend 时若该会话有持久化指针且其 jsonl 仍在,自动 set_resume_session
接回;指针在每次跑完 prompt / 切换会话(由 Gateway 调 persist)时更新。

⚠️ 为什么按 state_key 隔离而非按 cwd:多个 bot 常共用同一 CLAUDE_CWD(如 ~/aiProjects),
会话在 history.jsonl 里交织,按目录取「最近会话」会串到别的 bot;必须按 conv_id 精确落盘。
state_key 为空(平台没有稳定唯一标识)时整套持久化静默关闭,行为回退到纯内存。
"""
import json
import os
import threading


class SessionManager:
    def __init__(self, backend_factory, state_key: str = "", state_dir: str | None = None):
        self._backends: dict = {}
        self._factory = backend_factory
        self._lock = threading.Lock()
        self._pointers: dict[str, str] = {}      # conv_id -> session_id
        self._state_file: str | None = None
        if state_key:
            base = state_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._state_file = os.path.join(base, f".sessions-{state_key}.json")
            self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            if self._state_file and os.path.isfile(self._state_file):
                with open(self._state_file, encoding="utf-8") as f:
                    self._pointers = json.load(f) or {}
        except Exception as e:
            print(f"[WARN] 加载会话指针失败:{e}")
            self._pointers = {}

    def loaded_count(self) -> int:
        return len(self._pointers)

    def persist(self, conv_id: str) -> None:
        """把某会话当前 session_id 原子落盘;为空(如 /new 后)则删除该条。
        state_key 未设(_state_file=None)时 no-op。由 Gateway 在跑完/命令后调用。"""
        if not self._state_file or not conv_id:
            return
        backend = self._backends.get(conv_id)
        # 优先 resumable_session_id(其 jsonl 保证还在磁盘):失败轮会把
        # current_session_id 推到残缺会话上,直接落盘它重启就接不回了
        sid = None
        if backend:
            sid = (getattr(backend, "resumable_session_id", None)
                   or getattr(backend, "current_session_id", None))
        with self._lock:
            if sid:
                if self._pointers.get(conv_id) == sid:
                    return                       # 没变化,免去重复写盘
                self._pointers[conv_id] = sid
            else:
                if conv_id not in self._pointers:
                    return
                self._pointers.pop(conv_id, None)
            try:
                tmp = f"{self._state_file}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._pointers, f, ensure_ascii=False)
                os.replace(tmp, self._state_file)
            except Exception as e:
                print(f"[WARN] 持久化会话指针失败:{e}")

    # ---- 会话隔离 ----
    def get_or_create(self, conv_id: str, is_group: bool = False):
        # is_group 用于让 factory 决定是否注入群聊专属提示(如 relay 队友)。
        # 依赖不变式:conv_id 与会话类型稳定绑定(群 chat_id 恒群、单聊 open_id 恒单聊),
        # 故 backend 首建时按 is_group 定死人设即可,不会中途变类型。
        if conv_id not in self._backends:
            backend = self._factory(is_group)
            # 重启恢复:该会话若有持久化指针且其 jsonl 仍在磁盘,自动接回重启前的会话;
            # jsonl 已删则照常开新会话(set_resume_session 不被调用)。后端不支持会话
            # (session_file_exists 默认 False,如 Hermes)时该分支天然跳过。
            sid = self._pointers.get(conv_id)
            if sid and backend.session_file_exists(sid):
                backend.set_resume_session(sid)
            self._backends[conv_id] = backend
        return self._backends[conv_id]

    def get(self, conv_id: str):
        return self._backends.get(conv_id)
