"""智能体后端工厂。"""


def make_backend(name: str, cwd=None, append_prompt: str = "", base_env: dict | None = None, turn_prefix=None, turn_log=None):
    """turn_prefix: 可调用 (prompt:str)->str,每轮把注入块拼到用户消息前(角色记忆);None=原样。"""
    if name in ("claude-code", "claude", "claudecode"):
        from .claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend(cwd=cwd, append_prompt=append_prompt, base_env=base_env, turn_prefix=turn_prefix, turn_log=turn_log)
    if name == "hermes":
        from .hermes import HermesBackend
        return HermesBackend(append_prompt=append_prompt, turn_prefix=turn_prefix, turn_log=turn_log)
    raise SystemExit(f"❌ 未知 BOT_BACKEND: {name}（可选 claude-code / hermes）")
