#!/usr/bin/env python3
"""
Claude Code Feishu (Lark) Gateway - 长连接模式
手机/飞书发消息/图片 → 本地 Claude Code 执行 → 飞书收到回复

使用飞书开放平台「长连接」事件订阅方式，无需公网地址和 nginx 反代。
本地直接通过 WebSocket 连接飞书服务器接收消息。
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime

import httpx
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from claude_core import ClaudeSession, list_sessions, set_session_title, _session_file_exists, transcribe_audio

# 加载 .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN", "")
if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
    print("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请在 .env 中设置")
    sys.exit(1)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
MAX_MSG_LEN = 3800

# 每个 open_id → ClaudeSession
_sessions: dict[str, ClaudeSession] = {}


# ── 飞书 API 同步封装 ─────────────────────────────────────────


def _get_tenant_access_token() -> str:
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=30,
    )
    data = resp.json()
    return data.get("tenant_access_token", "")


def _send_message_sync(receive_id: str, msg_type: str, content: dict, receive_id_type: str = "open_id") -> dict:
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] send_message failed: {data}")
    return data


def _build_card(text: str) -> dict:
    """把纯文本包成飞书 interactive 卡片（可被 PATCH 更新）。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": text or " "},
            }
        ],
    }


def _send_card_sync(receive_id: str, text: str, receive_id_type: str = "open_id") -> dict:
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(_build_card(text), ensure_ascii=False),
    }
    resp = httpx.post(
        f"{FEISHU_BASE_URL}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] send_card failed: {data}")
    return data


def _update_card_sync(message_id: str, text: str) -> dict:
    """PATCH 更新 interactive 卡片。只有 interactive 消息能被更新。"""
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"content": json.dumps(_build_card(text), ensure_ascii=False)}
    resp = httpx.patch(
        f"{FEISHU_BASE_URL}/im/v1/messages/{message_id}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] update_card failed: {data}")
    return data


def _download_resource_sync(message_id: str, file_key: str, rtype: str, save_path: str) -> bool:
    """下载消息里的图片 / 文件（语音）。rtype ∈ {'image', 'file'}。"""
    token = _get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(
        f"{FEISHU_BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": rtype},
        headers=headers,
        timeout=60,
    )
    if resp.status_code == 200:
        with open(save_path, "wb") as f:
            f.write(resp.content)
        return True
    print(f"[WARN] download resource failed: status={resp.status_code} body={resp.text[:300]}")
    return False


# ── 流式输出推送 ──────────────────────────────────────────────


class FeishuStreamer:
    """把 Claude Code 的 stream-json 事件推送到飞书消息。"""

    MAX_LEN = 3500
    THROTTLE = 2.0

    def __init__(self, receive_id: str, message_id: str):
        self.receive_id = receive_id
        self.message_id = message_id
        self.text = ""
        self.status = ""
        self.last_edit = 0.0
        self.has_content = False
        self.partial_mode = False

    def _compose(self) -> str:
        parts = []
        body = self.text
        if self.status:
            body = (body + "\n" + self.status) if body else self.status
        if body:
            parts.append(body)
        if not parts:
            return "⏳ 处理中..."
        text = "\n\n".join(parts)
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 1] + "…"
        return text

    async def _do_edit(self):
        text = self._compose()
        try:
            await asyncio.to_thread(_update_card_sync, self.message_id, text)
        except Exception:
            pass
        self.last_edit = time.monotonic()

    async def _schedule(self):
        elapsed = time.monotonic() - self.last_edit
        if elapsed >= self.THROTTLE:
            await self._do_edit()

    async def append(self, chunk: str):
        if not chunk:
            return
        self.has_content = True
        budget = self.MAX_LEN - len(self.status) - 32
        if len(self.text) + len(chunk) > budget:
            await self._do_edit()
            try:
                result = await asyncio.to_thread(
                    _send_card_sync, self.receive_id, "⏳ 继续..."
                )
                self.message_id = result.get("data", {}).get("message_id", self.message_id)
            except Exception:
                pass
            self.text = ""
            self.status = ""
        self.text += chunk
        await self._schedule()

    async def set_status(self, line: str):
        if line == self.status:
            return
        self.status = line
        await self._schedule()

    async def clear_status(self):
        if self.status:
            self.status = ""
            await self._schedule()

    async def finalize(self, fallback: str = ""):
        self.status = ""
        if not self.has_content:
            text = (fallback or "").strip()
            if not text:
                await asyncio.to_thread(
                    _update_card_sync, self.message_id, "✅ 完成（无文字输出）"
                )
                return
            await asyncio.to_thread(
                _update_card_sync, self.message_id, text[:self.MAX_LEN]
            )
            for i in range(self.MAX_LEN, len(text), MAX_MSG_LEN):
                await asyncio.to_thread(
                    _send_card_sync, self.receive_id, text[i:i + MAX_MSG_LEN]
                )
            return
        await self._do_edit()


# ── 会话管理 ─────────────────────────────────────────────────


def _get_session(open_id: str) -> ClaudeSession:
    if open_id not in _sessions:
        _sessions[open_id] = ClaudeSession()
    return _sessions[open_id]


def _run_async(coro):
    """在新事件循环中运行协程。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 命令处理 ─────────────────────────────────────────────────


def _handle_command(sender: str, text: str) -> bool:
    text = text.strip()
    if text.startswith("/new"):
        session = _get_session(sender)
        session.set_new_session()
        _send_message_sync(sender, "text", {"text": "🔄 下一条消息将开启全新对话"})
        return True
    elif text.startswith("/sessions"):
        sessions = list_sessions(10)
        if not sessions:
            _send_message_sync(sender, "text", {"text": "❌ 没有找到历史会话"})
            return True
        lines = ["📋 最近会话（/resume <编号> 或 /resume <sessionId>）\n"]
        for i, s in enumerate(sessions, 1):
            t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
            summary = s["summary"] or "（无文字内容）"
            lines.append(
                f"{i}. [{t}] {summary}\n"
                f"    📁 {s['proj']}\n"
                f"    🔖 {s['id']}"
            )
        _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/resume"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": "用法：/resume <编号> 或 /resume <sessionId>\n先用 /sessions 查看"},
            )
            return True
        arg = parts[1].strip()
        session = _get_session(sender)
        if arg.isdigit():
            idx = int(arg) - 1
            sessions = list_sessions(10)
            if idx < 0 or idx >= len(sessions):
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 编号超出范围，共 {len(sessions)} 个会话"},
                )
                return True
            s = sessions[idx]
            session.set_resume_session(s["id"])
            t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
            summary = s["summary"] or "（无文字内容）"
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已切换到会话 {idx+1}\n[{t}] {summary}\n🔖 {s['id']}\n\n发消息继续这个会话吧"},
            )
        elif _session_file_exists(arg):
            session.set_resume_session(arg)
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已切换到会话\n🔖 {arg}\n\n发消息继续这个会话吧"},
            )
        else:
            # 按标题/summary 匹配（在更大的池子里找）
            pool = list_sessions(100)
            matches = [s for s in pool if (s["summary"] or "").strip() == arg]
            if not matches:
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 未找到 session：{arg}\n可输入编号、sessionId 或 /sessions 里的标题"},
                )
            elif len(matches) == 1:
                s = matches[0]
                session.set_resume_session(s["id"])
                t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                _send_message_sync(
                    sender, "text",
                    {"text": f"✅ 已切换到会话\n[{t}] {s['summary']}\n🔖 {s['id']}\n\n发消息继续这个会话吧"},
                )
            else:
                lines = [f"⚠️ 标题「{arg}」匹配到 {len(matches)} 个，请用 sessionId 明确指定："]
                for s in matches[:5]:
                    t = datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%m-%d %H:%M")
                    lines.append(f"[{t}] 🔖 {s['id']}")
                _send_message_sync(sender, "text", {"text": "\n".join(lines)})
        return True
    elif text.startswith("/rename"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            _send_message_sync(
                sender, "text",
                {"text": "用法：\n/rename <新名称>           # 重命名当前会话\n/rename <sessionId> <新名称>  # 重命名指定会话"},
            )
            return True
        session = _get_session(sender)
        if len(parts) == 2:
            # /rename <新名称>
            title = parts[1].strip()
            sid = session.current_session_id
            if not sid:
                _send_message_sync(
                    sender, "text",
                    {"text": "❌ 当前无活跃会话，先发一条消息或用 /resume 切换，再 /rename"},
                )
                return True
        else:
            # /rename <sessionId> <新名称>
            sid = parts[1].strip()
            title = parts[2].strip()
            if not _session_file_exists(sid):
                _send_message_sync(
                    sender, "text",
                    {"text": f"❌ 未找到 session：{sid}"},
                )
                return True
        if not title:
            _send_message_sync(sender, "text", {"text": "❌ 名称不能为空"})
            return True
        if set_session_title(sid, title):
            _send_message_sync(
                sender, "text",
                {"text": f"✅ 已重命名\n🔖 {sid}\n📝 {title}"},
            )
        else:
            _send_message_sync(
                sender, "text",
                {"text": f"❌ 重命名失败（未找到 session 文件）：{sid}"},
            )
        return True
    elif text.startswith("/start"):
        _send_message_sync(
            sender,
            "text",
            {
                "text": (
                    "🤖 Claude Code Gateway 已就绪\n\n"
                    "支持：文字 / 图片 / 语音\n\n"
                    "命令：\n"
                    "/sessions — 查看历史会话列表\n"
                    "/resume <编号|sessionId> — 切换到指定会话\n"
                    "/rename <新名称> — 重命名当前会话\n"
                    "/rename <sessionId> <新名称> — 重命名指定会话\n"
                    "/new — 开启全新会话\n\n"
                    "默认启动时自动接续最近一次会话"
                )
            },
        )
        return True
    return False


# ── 消息处理 ─────────────────────────────────────────────────


async def _handle_text_message(sender: str, text: str):
    if not text or not sender:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "⏳ 处理中...")
    reply_id = result.get("data", {}).get("message_id", "")

    session = _get_session(sender)
    streamer = FeishuStreamer(sender, reply_id) if reply_id else None

    try:
        response = await session.run_claude(text, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


async def _handle_image_message(sender: str, message_id: str, file_key: str, caption: str = ""):
    if not file_key or not sender or not message_id:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "🖼️ 处理图片中...")
    reply_id = result.get("data", {}).get("message_id", "")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    downloaded = await asyncio.to_thread(
        _download_resource_sync, message_id, file_key, "image", tmp_path
    )
    if not downloaded:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 图片下载失败")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": "❌ 图片下载失败"}
            )
        return

    session = _get_session(sender)
    streamer = FeishuStreamer(sender, reply_id) if reply_id else None

    prompt = caption.strip() or "请描述这张图片的内容"
    response = ""
    try:
        response = await session.run_claude_with_image(tmp_path, prompt, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


async def _handle_audio_message(sender: str, message_id: str, file_key: str):
    if not file_key or not sender or not message_id:
        return

    result = await asyncio.to_thread(_send_card_sync, sender, "🎙️ 识别语音中...")
    reply_id = result.get("data", {}).get("message_id", "")

    # 飞书语音默认是 opus
    with tempfile.NamedTemporaryFile(suffix=".opus", delete=False) as tmp:
        tmp_path = tmp.name

    downloaded = await asyncio.to_thread(
        _download_resource_sync, message_id, file_key, "file", tmp_path
    )
    if not downloaded:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 语音下载失败")
        return

    try:
        text = await asyncio.to_thread(transcribe_audio, tmp_path)
    except Exception as e:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, f"❌ 语音识别失败：{e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if not text:
        if reply_id:
            await asyncio.to_thread(_update_card_sync, reply_id, "❌ 语音识别为空，请重试")
        return

    print(f"\n🎙️  语音识别：{text}")
    if reply_id:
        await asyncio.to_thread(_update_card_sync, reply_id, f"🎙️ 已识别：{text}\n\n⏳ 处理中...")

    session = _get_session(sender)
    streamer = FeishuStreamer(sender, reply_id) if reply_id else None
    # 让 streamer 的首次 append 不再被 partial_mode 吞；同时让头部继续显示识别结果
    if streamer:
        streamer.text = f"🎙️ 已识别：{text}\n\n"

    try:
        response = await session.run_claude(text, streamer)
    except Exception as e:
        if streamer:
            await streamer.finalize(fallback=f"❌ 出错了：{e}")
        else:
            await asyncio.to_thread(
                _send_message_sync, sender, "text", {"text": f"❌ 出错了：{e}"}
            )
        return

    if streamer:
        await streamer.finalize(fallback=response)
    else:
        for i in range(0, len(response), MAX_MSG_LEN):
            await asyncio.to_thread(
                _send_message_sync, sender, "text",
                {"text": response[i:i + MAX_MSG_LEN]},
            )


def _process_message_event(event: P2ImMessageReceiveV1):
    """在后台线程中处理消息事件（避免阻塞 WebSocket 读取循环）。"""
    try:
        data = event.event
        message = data.message
        msg_type = message.message_type
        message_id = message.message_id or ""
        sender_id = data.sender.sender_id.open_id if data.sender and data.sender.sender_id else ""
        content = json.loads(message.content) if message.content else {}

        print(f"[DEBUG] 收到消息: msg_type={msg_type}, sender_id={sender_id}, message_id={message_id}, content={content}")

        if msg_type == "text":
            text = content.get("text", "").strip()
            if text.startswith("/"):
                if _handle_command(sender_id, text):
                    return
            _run_async(_handle_text_message(sender_id, text))
        elif msg_type == "image":
            file_key = content.get("image_key", "")
            _run_async(_handle_image_message(sender_id, message_id, file_key))
        elif msg_type == "audio":
            file_key = content.get("file_key", "")
            _run_async(_handle_audio_message(sender_id, message_id, file_key))
        else:
            print(f"[DEBUG] 暂不支持的消息类型: {msg_type}")
    except Exception as e:
        print(f"❌ 处理消息事件出错：{e}")
        traceback.print_exc()


def _handle_message_event(event: P2ImMessageReceiveV1):
    """长连接消息事件回调 — 只做解析，实际处理放到后台线程。"""
    threading.Thread(
        target=_process_message_event,
        args=(event,),
        daemon=True,
    ).start()


# ── 主入口 ───────────────────────────────────────────────────


def main():
    print("🤖 Claude Code Feishu Gateway 启动中（长连接模式）...")
    print(f"   App ID: {FEISHU_APP_ID[:8]}...")
    print("   等待飞书消息...\n")

    handler = (
        EventDispatcherHandler.builder(
            encrypt_key="",
            verification_token=FEISHU_VERIFY_TOKEN or "",
        )
        .register_p2_im_message_receive_v1(_handle_message_event)
        .build()
    )

    client = WsClient(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=LogLevel.INFO,
    )
    client.start()


if __name__ == "__main__":
    main()
