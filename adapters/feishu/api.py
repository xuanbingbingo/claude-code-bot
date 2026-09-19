"""飞书 REST 封装 —— 同步 httpx(streamer 用 asyncio.to_thread 调,避免堵 claude 读取循环)。
从旧 claude-feishu.py L66-187 抽出。"""
import json
import os

import httpx

BASE = "https://open.feishu.cn/open-apis"


def _build_card(text: str) -> dict:
    """interactive 卡片(可 PATCH 更新),markdown 组件渲染 GFM。"""
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{"tag": "markdown", "content": text or " "}]}


class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    def token(self) -> str:
        resp = httpx.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                          json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=30)
        return resp.json().get("tenant_access_token", "")

    def send_message(self, receive_id: str, msg_type: str, content: dict, receive_id_type="open_id") -> dict:
        resp = httpx.post(
            f"{BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
            json={"receive_id": receive_id, "msg_type": msg_type,
                  "content": json.dumps(content, ensure_ascii=False)}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            print(f"[WARN] send_message failed: {data}")
        return data

    def upload_opus(self, path: str, duration_ms: int) -> str:
        """上传语音文件,返回 file_key。

        🔴 飞书语音条只认 file_type="opus"(单声道 ogg/opus);传 wav 也能上传成功,
        但发出来会渲染成「文件」附件而不是可播放的语音条 —— 转码在 core/tts.py 做。
        """
        with open(path, "rb") as f:
            resp = httpx.post(
                f"{BASE}/im/v1/files",
                headers={"Authorization": f"Bearer {self.token()}"},
                data={"file_type": "opus", "file_name": os.path.basename(path),
                      "duration": str(max(1, int(duration_ms)))},
                files={"file": (os.path.basename(path), f, "audio/opus")}, timeout=120)
        data = resp.json()
        if data.get("code") != 0:
            print(f"[WARN] upload_opus failed: {data}")
            return ""
        return data.get("data", {}).get("file_key", "")

    def send_audio(self, receive_id: str, path: str, duration_ms: int,
                   receive_id_type="open_id") -> bool:
        """发一条语音消息(上传 + 发送两步)。"""
        file_key = self.upload_opus(path, duration_ms)
        if not file_key:
            return False
        data = self.send_message(receive_id, "audio", {"file_key": file_key}, receive_id_type)
        return data.get("code") == 0

    def send_card(self, receive_id: str, text: str, receive_id_type="open_id") -> str:
        """发卡片,返回 message_id(供后续 PATCH)。"""
        data = self.send_message(receive_id, "interactive", _build_card(text), receive_id_type)
        return data.get("data", {}).get("message_id", "")

    def update_card(self, message_id: str, text: str) -> dict:
        resp = httpx.patch(
            f"{BASE}/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
            json={"content": json.dumps(_build_card(text), ensure_ascii=False)}, timeout=30)
        return resp.json()

    def delete_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        try:
            resp = httpx.delete(f"{BASE}/im/v1/messages/{message_id}",
                                headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
            return resp.json().get("code") == 0
        except Exception as e:
            print(f"[WARN] delete_message 异常: {e}")
            return False

    def download_resource(self, message_id: str, file_key: str, rtype: str, save_path: str) -> bool:
        resp = httpx.get(f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}",
                         params={"type": rtype},
                         headers={"Authorization": f"Bearer {self.token()}"}, timeout=60)
        if resp.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return True
        print(f"[WARN] download resource failed: {resp.status_code}")
        return False
