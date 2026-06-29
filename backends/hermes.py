"""HermesBackend —— 通过 OpenAI 兼容 API 接入 Nous Research 的 Hermes Agent。

Hermes 暴露 OpenAI 兼容端点(API_SERVER_ENABLED=true,默认 http://localhost:8642/v1,
model=hermes-agent)。本后端把用户消息走 /chat/completions(stream=true),delta 喂给
streamer.append。Hermes 自管记忆/会话,故不暴露 claude 特有的 cwd/mode/会话列表命令。

这是可运行 stub:需用户先起 hermes API server。详见 hermes-agent.nousresearch.com/docs。
"""
import json
import os

from .base import AgentBackend


class HermesBackend(AgentBackend):
    capabilities = set()   # 纯文本流式;/cwd /model /sessions 等 claude 命令不适用

    def __init__(self, append_prompt: str = "", **_):
        self.base_url = os.environ.get("HERMES_API_URL", "http://localhost:8642/v1").rstrip("/")
        self.api_key = os.environ.get("HERMES_API_KEY", "")
        self.model = os.environ.get("HERMES_MODEL", "hermes-agent")
        self.system = append_prompt          # 人设作为 system 消息
        self.history: list[dict] = []        # 简单多轮历史

    async def run(self, prompt: str, streamer) -> str:
        import httpx
        messages = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages += self.history
        messages.append({"role": "user", "content": prompt})
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        full = ""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", headers=headers,
                    json={"model": self.model, "messages": messages, "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue
                        if delta:
                            full += delta
                            await streamer.append(delta)
        except Exception as e:
            err = (f"❌ Hermes 后端出错:{e}\n"
                   f"(请先启动 hermes API server:API_SERVER_ENABLED=true,"
                   f"端点 {self.base_url};见 hermes-agent.nousresearch.com/docs)")
            await streamer.append(err)
            return err
        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": full})
        return full

    def set_new_session(self) -> None:
        self.history = []

    def status_lines(self) -> list[str]:
        return [f"🤖 后端:Hermes（{self.model}）", f"🔗 端点:{self.base_url}"]
