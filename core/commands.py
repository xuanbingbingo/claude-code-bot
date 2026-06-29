"""CommandRouter —— 平台无关的命令路由(三个旧渠道里 90% 重复的命令逻辑收口)。

dispatch(inbound, backend, adapter) -> bool:True=已作为命令处理;False=透传给 backend.run。
回复统一经 adapter.send_text;命令对 backend 的操作按 backend.capabilities 门控
(不支持的命令回提示而非报错,这样 Hermes 等后端也能优雅降级)。
"""
import os
from datetime import datetime

_HELP = (
    "🤖 Claude Code Gateway 已就绪\n\n"
    "支持:文字 / 图片 / 语音(视平台)\n\n"
    "会话:/new /sessions /resume <编号|id> /rename [<id>] <名> \n"
    "运行:/stop /model [opus|sonnet|haiku|default] /mode [bypass|plan|default|accept]\n"
    "目录:/cwd [<路径>]   ·   状态:/status\n"
    "Agent:/agents [关键词] /agent <name> <任务>\n"
    "其它 /xxx 透传给后端(如官方 skill)"
)


class CommandRouter:
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

        if text.startswith("/start"):
            await reply(_HELP); return True

        return False   # 未注册 /xxx → 透传给后端(官方 skill 等)
