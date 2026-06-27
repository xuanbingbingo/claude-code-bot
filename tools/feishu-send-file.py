#!/usr/bin/env python3
"""把本地文件发到飞书聊天框 —— 默认自动发到「当前聊天窗口」。

随 claude-gateway 一起交付的自包含版本：不写死任何用户绝对路径，
凭证与收件人均来自运行环境或配置，开箱即用。

用法:
    feishu-send-file <文件路径> [receive_id]

收件人(receive_id)解析优先级:
    1. 命令行第二参显式指定
    2. 环境变量 FEISHU_SENDER_OPEN_ID —— 飞书网关 spawn 本会话 claude 时注入的
       「当前对话发起人 open_id」。这条让「发到当前聊天窗口」自动成立。
    3. 配置文件 ~/.claude/tools/feishu-send-file.conf 的 DEFAULT_RECEIVE_ID（兜底）

凭证(app_id/secret)解析优先级:
    1. 环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET —— 网关注入「当前 bot」凭证
    2. 环境变量 FEISHU_SEND_ENV 指定的 .env 文件
    3. 配置文件 conf 的 GATEWAY_ENV 指向的 .env 文件
    4. 常见默认路径 ~/aiProjects/claude-gateway/.env（兜底）

视频→media，图片→image，其它→file。
视频 > ~28MB 自动 ffmpeg 压到飞书 30MB 硬上限内再发（无 ffmpeg 时原样上传）。
"""
import json
import os
import sys
import uuid
import shutil
import tempfile
import subprocess
import mimetypes
import urllib.request

CONF_PATH = os.path.expanduser(
    os.environ.get("FEISHU_SEND_CONF", "~/.claude/tools/feishu-send-file.conf"))
BASE = "https://open.feishu.cn/open-apis"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
SIZE_LIMIT = 28 * 1024 * 1024  # 留余量，飞书硬上限 30MB


def _load_kv(path):
    kv = {}
    if not os.path.isfile(path):
        return kv
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip('"').strip("'")
    return kv


def _resolve_credentials(conf):
    """返回 (app_id, app_secret, source_desc)。env 优先 = 自动用「当前 bot」凭证。"""
    aid = os.environ.get("FEISHU_APP_ID")
    sec = os.environ.get("FEISHU_APP_SECRET")
    if aid and sec:
        return aid, sec, "环境变量(当前 bot)"
    candidates = [
        os.environ.get("FEISHU_SEND_ENV"),
        conf.get("GATEWAY_ENV"),
        os.path.expanduser("~/aiProjects/claude-gateway/.env"),
    ]
    for p in candidates:
        if not p:
            continue
        p = os.path.expanduser(p)
        kv = _load_kv(p)
        if kv.get("FEISHU_APP_ID") and kv.get("FEISHU_APP_SECRET"):
            return kv["FEISHU_APP_ID"], kv["FEISHU_APP_SECRET"], p
    return None, None, None


def _resolve_receive_id(conf, argv):
    """返回 (receive_id, rid_type, source_desc)。命令行 > 网关注入的当前窗口 > conf 兜底。"""
    if len(argv) > 2 and argv[2].strip():
        return argv[2].strip(), os.environ.get("FEISHU_SENDER_ID_TYPE", "open_id"), "命令行指定"
    inj = os.environ.get("FEISHU_SENDER_OPEN_ID", "").strip()
    if inj:
        return inj, os.environ.get("FEISHU_SENDER_ID_TYPE", "open_id"), "当前聊天窗口(网关注入)"
    rid = conf.get("DEFAULT_RECEIVE_ID", "").strip()
    if rid:
        return rid, conf.get("RECEIVE_ID_TYPE", "open_id"), "配置默认收件人"
    return "", "open_id", None


def _token(app_id, app_secret):
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("tenant_access_token", "")


def _multipart(fields, files):
    boundary = "----feishuSendFileBoundary" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, data, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


def _compress_video(src):
    """把超限视频压到飞书上限内。需要 ffmpeg；返回临时 mp4 路径，失败抛异常。"""
    out = os.path.join(tempfile.gettempdir(), f"fsf_{uuid.uuid4().hex[:8]}.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vf", "scale='min(1080,iw)':-2",
        "-c:v", "libx264", "-b:v", "760k", "-maxrate", "900k", "-bufsize", "1600k",
        "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", out,
    ], check=True, capture_output=True, timeout=600)
    return out


def _video_meta(path):
    """返回 (duration_ms:int, cover_png_path or None)。缺 ffmpeg/ffprobe 时优雅降级。"""
    dur_ms = 0
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30).stdout.strip()
            dur_ms = int(float(out) * 1000)
        except Exception:
            dur_ms = 0
    cover = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        ss = "0.5" if (dur_ms == 0 or dur_ms > 1200) else "0"
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-ss", ss, "-i", path, "-frames:v", "1",
                 "-vf", "scale=480:-2", tmp.name],
                capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.getsize(tmp.name) > 0:
                cover = tmp.name
            else:
                os.remove(tmp.name)
        except Exception:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
    return dur_ms, cover


def _upload(token, path, file_type, extra=None):
    with open(path, "rb") as f:
        data = f.read()
    fname = os.path.basename(path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if file_type == "image":
        body, boundary = _multipart({"image_type": "message"}, {"image": (fname, data, ctype)})
        url = f"{BASE}/im/v1/images"
        key = "image_key"
    else:
        fields = {"file_type": file_type, "file_name": fname}
        if extra:
            fields.update({k: str(v) for k, v in extra.items()})
        body, boundary = _multipart(fields, {"file": (fname, data, ctype)})
        url = f"{BASE}/im/v1/files"
        key = "file_key"
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    if res.get("code") != 0:
        raise RuntimeError(f"upload failed: {res}")
    return res["data"][key]


def _send(token, receive_id, rid_type, msg_type, content):
    body = json.dumps({
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content),
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/im/v1/messages?receive_id_type={rid_type}",
        data=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print("用法: feishu-send-file <文件路径> [receive_id]", file=sys.stderr)
        sys.exit(2)
    path = os.path.expanduser(sys.argv[1])
    if not os.path.isfile(path):
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    conf = _load_kv(CONF_PATH)

    receive_id, rid_type, rid_src = _resolve_receive_id(conf, sys.argv)
    if not receive_id:
        print("没有 receive_id：命令行未传，环境无 FEISHU_SENDER_OPEN_ID，配置也无 DEFAULT_RECEIVE_ID",
              file=sys.stderr)
        sys.exit(1)

    app_id, app_secret, cred_src = _resolve_credentials(conf)
    if not (app_id and app_secret):
        print("未找到 FEISHU_APP_ID/SECRET：环境变量、FEISHU_SEND_ENV、conf.GATEWAY_ENV、默认路径均无",
              file=sys.stderr)
        sys.exit(1)
    print(f"→ 收件人[{rid_src}]={receive_id}  凭证[{cred_src}]", file=sys.stderr)

    token = _token(app_id, app_secret)
    if not token:
        print("拿 tenant_access_token 失败", file=sys.stderr)
        sys.exit(1)

    ext = os.path.splitext(path)[1].lower()
    tmp_compressed = None
    if ext in IMAGE_EXTS:
        key = _upload(token, path, "image")
        res = _send(token, receive_id, rid_type, "image", {"image_key": key})
    elif ext in VIDEO_EXTS:
        # 超限先压缩（需 ffmpeg）；压不动就原样试，飞书会拒就回退普通文件
        if os.path.getsize(path) > SIZE_LIMIT and shutil.which("ffmpeg"):
            mb = os.path.getsize(path) // 1024 // 1024
            print(f"[info] {mb}MB 超 28MB，ffmpeg 压缩中…", file=sys.stderr)
            try:
                tmp_compressed = _compress_video(path)
                path = tmp_compressed
            except Exception as e:
                print(f"[warn] 压缩失败，按原文件继续: {e}", file=sys.stderr)
        dur_ms, cover = _video_meta(path)            # 时长(毫秒) + 封面帧
        extra = {"duration": dur_ms} if dur_ms else None
        key = _upload(token, path, "mp4", extra)
        content = {"file_key": key}
        if cover:
            try:
                content["image_key"] = _upload(token, cover, "image")   # 封面缩略图
            except Exception:
                pass
            finally:
                try:
                    os.remove(cover)
                except OSError:
                    pass
        res = _send(token, receive_id, rid_type, "media", content)
        if res.get("code") != 0:  # media 失败回退普通文件
            key = _upload(token, path, "stream")
            res = _send(token, receive_id, rid_type, "file", {"file_key": key})
    else:
        key = _upload(token, path, "stream")
        res = _send(token, receive_id, rid_type, "file", {"file_key": key})

    if tmp_compressed:
        try:
            os.remove(tmp_compressed)
        except OSError:
            pass

    if res.get("code") == 0:
        print(f"✅ 已发送: {os.path.basename(sys.argv[1])} → {receive_id}")
    else:
        print(f"❌ 发送失败: {res}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
