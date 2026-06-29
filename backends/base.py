"""AgentBackend —— 智能体后端抽象。把用户 prompt 跑成流式输出。

把"调哪个智能体"从平台逻辑里解耦:ClaudeCodeBackend 调 claude CLI,HermesBackend 调
OpenAI 兼容 API,后续可加别的。capabilities 声明本后端支持哪些能力,CommandRouter 据此
决定 /cwd /model /sessions 等命令是否可用(不支持就回提示,而非报错)。
"""
from abc import ABC, abstractmethod

# 能力标志
CAP_SESSIONS = "sessions"    # 历史会话 列表/恢复/重命名
CAP_IMAGES = "images"        # 多模态图片
CAP_SUBAGENTS = "subagents"  # /agents /agent
CAP_CWD = "cwd"              # 切换工作目录
CAP_MODEL = "model"          # 切换模型
CAP_MODE = "mode"            # 权限模式
CAP_STOP = "stop"            # 中断任务


class AgentBackend(ABC):
    capabilities: set = set()

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    # ---- 必须实现:跑 prompt,流式回调 streamer,返回最终文本 ----
    @abstractmethod
    async def run(self, prompt: str, streamer) -> str: ...

    async def run_with_image(self, image_path: str, caption: str, streamer) -> str:
        raise NotImplementedError("该后端不支持图片")

    # ---- 平台注入(如飞书 send-file 工具需要的会话发起人 id + bot 凭证)----
    def inject_env(self, env: dict) -> None: ...

    # ---- 会话/控制(默认 no-op,有对应 capability 的子类覆盖)----
    def set_new_session(self) -> None: ...
    def set_resume_session(self, sid: str) -> None: ...
    def set_cwd(self, path: str) -> bool: return False
    def set_model(self, name: str | None) -> None: ...
    def set_mode(self, name: str) -> bool: return False
    async def stop(self) -> bool: return False

    # ---- 会话列表(capability sessions)----
    def list_sessions(self, limit: int = 10) -> list: return []
    def list_agents(self) -> list: return []
    def session_file_exists(self, sid: str) -> bool: return False
    def set_session_title(self, sid: str, title: str) -> bool: return False

    # ---- /status 展示用 ----
    def status_lines(self) -> list[str]: return []
    # ---- 当前工作目录(命令/会话列表用,无则 None)----
    @property
    def cwd(self) -> str | None: return None
