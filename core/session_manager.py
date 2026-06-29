"""SessionManager —— conv_id → AgentBackend 实例,会话隔离。
backend_factory 由 run.py 注入(造 ClaudeCodeBackend / HermesBackend,已带好 cwd+人设)。"""


class SessionManager:
    def __init__(self, backend_factory):
        self._backends: dict = {}
        self._factory = backend_factory

    def get_or_create(self, conv_id: str):
        if conv_id not in self._backends:
            self._backends[conv_id] = self._factory()
        return self._backends[conv_id]

    def get(self, conv_id: str):
        return self._backends.get(conv_id)
