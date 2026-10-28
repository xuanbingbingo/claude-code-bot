#!/usr/bin/env python3
"""统一入口 —— 按 BOT_PLATFORM × BOT_BACKEND 装配并启动 bot。

  BOT_PLATFORM=wecom BOT_BACKEND=claude-code python run.py
平台(adapter)、智能体(backend)解耦:加平台只写 adapters/<p>/,加后端只写 backends/<b>.py。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_env, BotConfig            # noqa: E402
from core.runtime_rules import RUNTIME_RULES           # noqa: E402
from core import memory as role_memory                 # noqa: E402
from core.session_manager import SessionManager        # noqa: E402
from core.commands import CommandRouter                # noqa: E402
from core.relay import RelayManager                    # noqa: E402
from core.gateway import Gateway                       # noqa: E402
from core.tts import VoiceService                      # noqa: E402
from backends import make_backend                      # noqa: E402
from adapters import make_adapter                      # noqa: E402


def main():
    load_env()
    config = BotConfig.from_env()
    if not config.platform:
        print("❌ 未设置 BOT_PLATFORM(feishu/wecom/telegram/dingtalk)")
        sys.exit(1)

    adapter = make_adapter(config.platform, config)
    relay = RelayManager(config.relay_enabled, config.relay_max_hops, config.relay_teammates)

    # backend 的 append-system-prompt = 人设 + 平台工具提示(飞书 send-file) + 运行时约束;
    # relay 队友提示仅群聊 backend 注入 —— 单聊没有队友,注入只会逼 bot 每轮对空气 @ 队友(纯噪音)。
    # conv_id 与会话类型稳定绑定(群 chat_id 恒群、单聊 chat_id 恒单聊),故按会话建 backend 时定死即可。
    # RUNTIME_RULES 放最后:一次性进程/禁 run_in_background 是物理事实,不许被人设覆盖。
    # 角色 bot(BOT_ROLE / bots/<role>/persona.md):记忆纪律由网关统一生成进系统提示(内容不变,首轮生效即可),
    # 记忆内容每轮拼进用户消息(会变的东西不能放系统提示:--resume 只认首轮)。见 core/memory.py。
    discipline = role_memory.discipline(config.role, where="飞书") if config.role else ""
    turn_prefix = (lambda prompt: role_memory.wrap_turn(config.role, prompt)) if config.role else None
    turn_log = (lambda u, a: role_memory.log_turn(config.role, "飞书", u, a)) if config.role else None

    def backend_factory(is_group: bool = False):
        relay_prompt = relay.system_prompt() if is_group else ""
        append_prompt = "\n\n".join(p for p in (config.persona, discipline, relay_prompt,
                                                adapter.tool_prompt(), RUNTIME_RULES) if p)
        return make_backend(config.backend, cwd=config.cwd, append_prompt=append_prompt, turn_prefix=turn_prefix, turn_log=turn_log)

    # state_key 按平台取 bot 唯一标识(飞书 app_id / 企微 bot_id / TG botid),用于跨重启会话续接落盘
    # stamp_fn:人设文件变了 → 该会话下一条消息自动开新会话(续接会话吃不到新人设)
    sessions = SessionManager(backend_factory, state_key=adapter.state_key(),
                              stamp_fn=lambda: role_memory.persona_stamp(config.persona_files))

    # 语音回复:仅支持的平台(目前飞书)且未显式关掉时才装;会话级开关按 bot 隔离落盘
    voice = None
    if config.voice_enabled and getattr(adapter, "supports_voice", False):
        voice = VoiceService(enabled_default=True, voice_name=config.voice_name,
                             max_chars=config.voice_max_chars, state_key=adapter.state_key())
    gateway = Gateway(adapter, sessions, CommandRouter(voice=voice), relay, voice=voice)

    print(f"🤖 启动 {config.platform} × {config.backend}  bot={config.bot_name or '(未命名)'}")
    if config.persona:
        print(f"   🎭 人设 {len(config.persona)} 字")
    if config.role:
        print(f"   🧠 角色记忆 bots/{config.role}/(纪律 {len(discipline)} 字,记忆每轮注入)")
    if sessions.loaded_count():
        print(f"   🔖 已加载 {sessions.loaded_count()} 条会话指针(重启自动续接)")
    if voice:
        print(f"   🔊 语音回复已开启（音色 {config.voice_name or 'hfvoice 默认'}，"
              f"上限 {config.voice_max_chars} 字，/voice off 可关）")
    if config.relay_enabled and config.relay_teammates:
        print(f"   🔗 relay 队友:{list(config.relay_teammates)}")
    asyncio.run(adapter.connect(gateway))


if __name__ == "__main__":
    main()
