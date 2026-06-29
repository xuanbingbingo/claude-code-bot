"""智能体后端工厂。"""


def make_backend(name: str, cwd=None, append_prompt: str = "", base_env: dict | None = None):
    if name in ("claude-code", "claude", "claudecode"):
        from .claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend(cwd=cwd, append_prompt=append_prompt, base_env=base_env)
    if name == "hermes":
        from .hermes import HermesBackend
        return HermesBackend(append_prompt=append_prompt)
    raise SystemExit(f"❌ 未知 BOT_BACKEND: {name}（可选 claude-code / hermes）")
