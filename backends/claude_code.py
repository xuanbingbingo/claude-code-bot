"""ClaudeCodeBackend —— 包一层现有 claude_core.ClaudeSession(零改动复用 832 行核心)。
能力全开:会话/图片/subagent/cwd/model/mode/stop。"""
import os

import claude_core as cc          # 根目录唯一核心(run.py 已把项目根加入 sys.path)
from .base import (AgentBackend, CAP_SESSIONS, CAP_IMAGES, CAP_SUBAGENTS,
                   CAP_CWD, CAP_MODEL, CAP_MODE, CAP_STOP)


class ClaudeCodeBackend(AgentBackend):
    capabilities = {CAP_SESSIONS, CAP_IMAGES, CAP_SUBAGENTS, CAP_CWD, CAP_MODEL, CAP_MODE, CAP_STOP}

    def __init__(self, cwd: str | None = None, append_prompt: str = "", base_env: dict | None = None):
        self.session = cc.ClaudeSession(cwd=cwd)
        if append_prompt:
            self.session.extra_append_prompt = append_prompt
        if base_env:
            self.session.extra_env.update(base_env)

    # ---- 执行 ----
    async def run(self, prompt: str, streamer) -> str:
        return await self.session.run_claude(prompt, streamer)

    async def run_with_image(self, image_path: str, caption: str, streamer) -> str:
        return await self.session.run_claude_with_image(image_path, caption, streamer)

    # ---- 平台环境注入(飞书 send-file 用)----
    def inject_env(self, env: dict) -> None:
        self.session.extra_env.update(env)

    # ---- 会话/控制 ----
    def set_new_session(self) -> None:
        self.session.set_new_session()

    def set_resume_session(self, sid: str) -> None:
        self.session.set_resume_session(sid)

    def set_cwd(self, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        self.session.set_cwd(path)
        return True

    def set_model(self, name: str | None) -> None:
        self.session.set_model(name)

    def set_mode(self, name: str) -> bool:
        return self.session.set_mode(name)

    async def stop(self) -> bool:
        return await self.session.stop()

    # ---- 会话列表(走 claude_core 模块函数,按当前 cwd 过滤)----
    def list_sessions(self, limit: int = 10) -> list:
        return cc.list_sessions(limit, cwd=self.session.cwd)

    def list_agents(self) -> list:
        return cc.list_agents(cwd=self.session.cwd)

    def session_file_exists(self, sid: str) -> bool:
        return cc._session_file_exists(sid)

    def set_session_title(self, sid: str, title: str) -> bool:
        return cc.set_session_title(sid, title)

    # ---- 访问器 ----
    @property
    def cwd(self) -> str | None:
        return self.session.cwd

    @property
    def model(self):
        return self.session.model

    @property
    def mode(self):
        return self.session.mode

    @property
    def current_session_id(self):
        return self.session.current_session_id

    @property
    def resumable_session_id(self):
        return self.session.resumable_session_id

    def status_lines(self) -> list[str]:
        s = self.session
        running = s.current_proc is not None and s.current_proc.returncode is None
        return [
            f"📁 目录:{s.cwd}",
            f"🤖 模型:{s.model or '默认'}",
            f"🔐 模式:{s.mode}",
            f"🔖 会话:{s.current_session_id or '（新会话）'}",
            f"⚙️ 任务:{'运行中' if running else '空闲'}",
        ]
