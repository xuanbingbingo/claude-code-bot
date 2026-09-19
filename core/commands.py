"""CommandRouter —— 平台无关的命令路由(三个旧渠道里 90% 重复的命令逻辑收口)。

dispatch(inbound, backend, adapter) -> bool:True=已作为命令处理;False=透传给 backend.run。
回复统一经 adapter.send_text;命令对 backend 的操作按 backend.capabilities 门控
(不支持的命令回提示而非报错,这样 Hermes 等后端也能优雅降级)。
"""
import asyncio
import os
from datetime import datetime

from .tts import load_voice_catalog, resolve_voice

_HELP = (
    "🤖 Claude Code Gateway 已就绪\n\n"
    "支持:文字 / 图片 / 语音(视平台)\n\n"
    "会话:/new /sessions /resume <编号|id> /rename [<id>] <名> \n"
    "运行:/stop /model [opus|sonnet|haiku|default] /mode [bypass|plan|default|accept]\n"
    "目录:/cwd [<路径>]   ·   状态:/status\n"
    "语音:/voice [on|off] · /voice <音色名> 换音色 · /voice list 看全部\n"
    "Agent:/agents [关键词] /agent <name> <任务>\n"
    "其它 /xxx 透传给后端(如官方 skill)"
)


class CommandRouter:
    PREVIEW_TEXT = "这是音色试听。你好，我是你的飞书助手，现在用的就是这个声音。"

    def __init__(self, voice=None):
        self.voice = voice          # VoiceService | None;None = 本 bot 没开语音能力

    def _voice_list_text(self) -> str:
        """按引擎分组列出音色。

        只列能用的 —— 不可用的(如模型文件缺失的本地音色)对用户是纯噪音,列出来只会让人
        选了才发现没声音;真选中了 /voice 分支还是会明确拦下并说明原因。
        """
        cat = [v for v in load_voice_catalog() if v["usable"]]
        if not cat:
            return "❌ 读不到可用音色（检查 ~/aiProjects/hf-voice/voices.json）"
        groups: dict[str, list[str]] = {}
        for v in cat:
            label = v["name"] + (f"（{v['alias'][0]}）" if v["alias"] else "")
            groups.setdefault(v["engine"], []).append(label)
        lines = [f"🎙 可用音色（{len(cat)} 个）"]
        for engine, names in groups.items():
            tag = {"sami": "剪映", "edge": "微软", "kokoro": "本地"}.get(engine, engine)
            lines.append(f"\n【{tag}】" + "、".join(names))
        lines.append("\n\n换音色:/voice <音色名>　（名字/别名/编号都认）")
        return "\n".join(lines)

    async def _voice_preview(self, adapter, conv_id: str, chat_type: str, voice_name: str):
        """切完音色当场发一条试听;失败只提示,不影响已经生效的设置。"""
        try:
            clip = await self.voice.synthesize(self.PREVIEW_TEXT, voice_name)
            if not clip:
                await adapter.send_text(conv_id, chat_type, "⚠️ 试听合成失败（音色已切，下条回复会用它）")
                return
            try:
                await adapter.send_voice(conv_id, chat_type, clip.path, clip.duration_ms)
            finally:
                try:
                    os.unlink(clip.path)
                except Exception:
                    pass
        except Exception as e:
            print(f"[WARN] 音色试听失败:{e}")

    async def dispatch(self, inbound, backend, adapter) -> bool:
        text = inbound.text.strip()
        cid, ct = inbound.conv_id, inbound.chat_type

        async def reply(msg: str):
            await adapter.send_text(cid, ct, msg)

        if text.startswith("/new"):
            backend.set_new_session()
            await reply("🔄 下一条消息将开启全新对话")
            return True

        if text.startswith("/sessions"):
            if not backend.has("sessions"):
                await reply("ℹ️ 当前后端不支持会话列表"); return True
            ss = backend.list_sessions(10)
            if not ss:
                await reply("❌ 没有历史会话"); return True
            lines = ["📋 最近会话（/resume <编号>）\n"]
            for i, s in enumerate(ss, 1):
                t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                lines.append(f"{i}. [{t}] {s['summary'] or '（无内容）'}\n    🔖 {s['id']}")
            await reply("\n".join(lines)); return True

        if text.startswith("/resume"):
            if not backend.has("sessions"):
                await reply("ℹ️ 当前后端不支持会话恢复"); return True
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await reply("用法:/resume <编号|sessionId>"); return True
            arg = parts[1].strip()
            if arg.isdigit():
                ss = backend.list_sessions(10); idx = int(arg) - 1
                if 0 <= idx < len(ss):
                    backend.set_resume_session(ss[idx]["id"])
                    await reply(f"✅ 已切到会话 {idx+1}\n🔖 {ss[idx]['id']}")
                else:
                    await reply(f"❌ 编号超范围（共 {len(ss)}）")
            elif backend.session_file_exists(arg):
                backend.set_resume_session(arg); await reply(f"✅ 已切到会话\n🔖 {arg}")
            else:
                await reply(f"❌ 未找到 session：{arg}")
            return True

        if text.startswith("/rename"):
            if not backend.has("sessions"):
                await reply("ℹ️ 当前后端不支持"); return True
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                await reply("用法:/rename <新名称> 或 /rename <sessionId> <新名称>"); return True
            if len(parts) == 2:
                sid, title = backend.current_session_id, parts[1].strip()
                if not sid:
                    await reply("❌ 当前无活跃会话,先发一条消息或 /resume"); return True
            else:
                sid, title = parts[1].strip(), parts[2].strip()
                if not backend.session_file_exists(sid):
                    await reply(f"❌ 未找到 session：{sid}"); return True
            await reply(f"✅ 已重命名\n🔖 {sid}\n📝 {title}" if backend.set_session_title(sid, title)
                        else f"❌ 重命名失败:{sid}")
            return True

        if text.startswith("/cwd"):
            if not backend.has("cwd"):
                await reply("ℹ️ 当前后端不支持切换工作目录"); return True
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await reply(f"📁 当前目录:{backend.cwd}\n用法:/cwd <绝对路径>"); return True
            target = os.path.abspath(os.path.expanduser(parts[1].strip()))
            await reply(f"✅ 已切换工作目录\n{target}\n（下条消息将在此目录、新会话开始）"
                        if backend.set_cwd(target) else f"❌ 目录不存在:{target}")
            return True

        if text.startswith("/model"):
            if not backend.has("model"):
                await reply("ℹ️ 当前后端不支持切换模型"); return True
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await reply(f"🤖 当前模型:{getattr(backend, 'model', None) or '默认'}\n"
                            "切换:/model opus|sonnet|haiku|default"); return True
            name = parts[1].strip()
            backend.set_model(None if name in ("default", "默认", "reset", "clear") else name)
            await reply(f"✅ 模型:{name}（下条生效）"); return True

        if text.startswith("/mode"):
            if not backend.has("mode"):
                await reply("ℹ️ 当前后端不支持权限模式"); return True
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await reply(f"🔐 当前模式:{getattr(backend, 'mode', '')}\n"
                            "切换:/mode bypass|plan|default|accept"); return True
            ok = backend.set_mode(parts[1].strip().lower())
            await reply(f"✅ 模式:{parts[1].strip()}（下条生效）" if ok else "❌ 未知模式"); return True

        if text.startswith("/stop"):
            killed = await backend.stop()
            await reply("🛑 已中断当前任务" if killed else "ℹ️ 当前没有运行中的任务"); return True

        if text.startswith("/status"):
            lines = backend.status_lines() or ["（无状态信息）"]
            await reply("📊 当前状态\n" + "\n".join(lines)); return True

        if text.startswith("/agents"):
            if not backend.has("subagents"):
                await reply("ℹ️ 当前后端不支持 subagent"); return True
            parts = text.split(maxsplit=1)
            kw = parts[1].strip().lower() if len(parts) > 1 else ""
            agents = backend.list_agents()
            if kw:
                agents = [a for a in agents if kw in a["name"].lower() or kw in a["description"].lower()]
            if not agents:
                await reply("❌ 未找到可用 agent"); return True
            lines = [f"🤖 可用 Agent ({len(agents)} 个)"]
            for a in agents:
                icon = "📂" if a.get("scope") == "project" else "🌍"
                lines.append(f"\n{icon} {a['name']}\n   {a['description'][:80]}")
            lines.append("\n调用:/agent <name> <任务>")
            await reply("\n".join(lines)); return True

        if text.startswith("/agent"):
            if not backend.has("subagents"):
                await reply("ℹ️ 当前后端不支持 subagent"); return True
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                await reply("用法:/agent <name> <任务>  （先 /agents 看列表）"); return True
            name = parts[1].strip()
            match = next((a for a in backend.list_agents() if a["name"] == name), None)
            if not match:
                await reply(f"❌ 未找到 agent：{name}"); return True
            if len(parts) < 3:
                await reply(f"🤖 {match['name']}\n{match['description']}\n\n调用:/agent {name} <任务>"); return True
            # 改写成派发 prompt,返回 False 继续走 backend.run(带 streamer)
            inbound.text = f'请调用 subagent "{name}" 完成以下任务,并把它的结果原样返回:\n\n{parts[2].strip()}'
            return False

        if text.startswith("/voice"):
            if not self.voice or not getattr(adapter, "supports_voice", False):
                await reply("ℹ️ 当前平台/配置未启用语音回复"); return True
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            low = arg.lower()

            if not arg:
                state = "开启" if self.voice.is_on(cid) else "关闭"
                cur = self.voice.voice_for(cid) or "hfvoice 默认"
                await reply(f"🔊 语音回复：{state}\n🎙 当前音色：{cur}\n\n"
                            "切换开关:/voice on|off\n换音色:/voice <音色名>\n看全部:/voice list")
                return True

            if low in ("on", "开", "1", "true"):
                self.voice.set(cid, True); await reply("🔊 已开启语音回复"); return True
            if low in ("off", "关", "0", "false"):
                self.voice.set(cid, False); await reply("🔇 已关闭语音回复（仍照常发文字）"); return True

            if low in ("list", "列表", "ls"):
                await reply(self._voice_list_text()); return True

            # 其余一律当音色名解析(名字/别名/编号都认)
            hit = resolve_voice(arg)
            if not hit:
                await reply(f"❌ 没有这个音色：{arg}\n发 /voice list 看全部"); return True
            if not hit["usable"]:
                # kokoro 模型文件丢过一次,选了会静默没声音 —— 提前拦下,别让用户以为是网关坏了
                await reply(f"⚠️ 音色「{hit['name']}」当前不可用（本地 kokoro 模型文件缺失）\n"
                            "请换 sami / edge 的音色，/voice list 看全部"); return True
            self.voice.set_voice(cid, hit["name"])
            if not self.voice.is_on(cid):
                self.voice.set(cid, True)
            await reply(f"🎙 音色已切到「{hit['name']}」（{hit['engine']}）\n正在合成一条试听…")
            # 试听异步发:合成要几秒,不该把命令响应吊在这里
            asyncio.create_task(self._voice_preview(adapter, cid, ct, hit["name"]))
            return True

        if text.startswith("/start"):
            await reply(_HELP); return True

        return False   # 未注册 /xxx → 透传给后端(官方 skill 等)
