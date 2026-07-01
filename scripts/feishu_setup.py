#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书扫码自动建应用（设备码注册流）。

用手机飞书扫一个二维码，就在飞书账号中心一键创建一个企业自建应用、
预配好机器人能力/权限/事件订阅，直接拿到 App ID / App Secret —— 免去手动
进开放平台建应用的步骤。

原理：调用飞书账号中心的设备码注册端点
    POST https://accounts.feishu.cn/oauth/v1/app/registration   (国际版 accounts.larksuite.com)
走 init → begin → poll 三段式（Device Authorization Grant），archetype=PersonalAgent。
这是飞书给 OpenClaw / lark-cli 生态开的注册模板，lark-oapi SDK 不含此端点，故用 httpx 直连。
（实现参考 cc-connect 的 cmd/cc-connect/feishu.go runRegistrationFlow）

约定（供 new-agent.sh 捕获）：
    - 成功时 **stdout 只打印一行**： "<app_id> <app_secret> <owner_open_id>"
    - 所有提示 / 二维码 / 进度一律走 **stderr**，不污染 stdout
    - 失败时非 0 退出码 + stderr 错误信息

用法：
    python3 scripts/feishu_setup.py [--role NAME] [--platform feishu|lark] [--timeout 600]
"""
import argparse
import sys
import time

import httpx

FEISHU_BASE = "https://accounts.feishu.cn"
LARK_BASE = "https://accounts.larksuite.com"
REG_PATH = "/oauth/v1/app/registration"


def eprint(*args, **kwargs):
    """提示信息一律打到 stderr，保持 stdout 干净。"""
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def _call(client: httpx.Client, base: str, action: str, extra: dict) -> dict:
    """向注册端点发一个 form-urlencoded 请求，返回解析后的 JSON。"""
    data = {"action": action, **extra}
    resp = client.post(
        base + REG_PATH,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"注册端点返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")


def _show_qr(url: str, qr_image: str = ""):
    """
    展示扫码链接的二维码。
    - 默认渲染成终端 ASCII 二维码（打到 stderr）——适合用户自己在终端敲命令的场景。
    - 若给了 qr_image 路径，则额外存一张 PNG（需 pillow）——适合 skill / Claude 代跑的场景，
      由上层 open 这张图给用户扫（终端 ASCII 在代跑时用户看不到）。
    无论哪种都打印原始 URL 兜底。
    """
    saved_png = False
    if qr_image:
        try:
            import qrcode

            qrcode.make(url).save(qr_image)  # qrcode.make 走 PIL image，需 pillow
            saved_png = True
            eprint(f"🖼  二维码已存为图片：{qr_image}")
        except Exception as e:  # noqa: BLE001
            eprint(f"（PNG 二维码生成失败：{e}，回退终端二维码）")
    if not saved_png:
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(out=sys.stderr, invert=True)
        except Exception as e:  # noqa: BLE001
            eprint(f"（终端二维码渲染失败：{e}，请用下面的链接手动打开/扫码）")
    eprint("\n用【手机飞书】扫二维码，或在飞书里打开这个链接授权：")
    eprint(f"  {url}\n")


def run_registration(platform: str = "feishu", timeout: int = 600, role: str = "", qr_image: str = "") -> dict:
    """执行完整设备码注册流，成功返回 {app_id, app_secret, open_id, platform}。"""
    base = LARK_BASE if platform == "lark" else FEISHU_BASE
    tag = f"[{role}] " if role else ""

    with httpx.Client() as client:
        # ---------- 1. init：探测是否支持 client_secret 认证 ----------
        eprint(f"{tag}① 探测注册端点 ...")
        init_res = _call(client, base, "init", {})
        if init_res.get("error"):
            raise RuntimeError(f"init 失败：{init_res['error']} {init_res.get('error_description', '')}")
        methods = [m.lower() for m in (init_res.get("supported_auth_methods") or [])]
        if methods and "client_secret" not in methods:
            raise RuntimeError(
                f"该飞书账号环境不支持 client_secret 认证（仅 {methods}），无法用本方式建应用。"
            )

        # ---------- 2. begin：发起设备码，拿二维码 ----------
        eprint(f"{tag}② 发起扫码建应用 ...")
        begin_res = _call(
            client,
            base,
            "begin",
            {
                "archetype": "PersonalAgent",
                "auth_method": "client_secret",
                "request_user_info": "open_id",
            },
        )
        if begin_res.get("error"):
            raise RuntimeError(f"begin 失败：{begin_res['error']} {begin_res.get('error_description', '')}")
        device_code = begin_res.get("device_code")
        verify_uri = begin_res.get("verification_uri_complete") or begin_res.get("verification_uri")
        if not device_code or not verify_uri:
            raise RuntimeError(f"begin 未返回 device_code / verification_uri：{begin_res}")

        interval = begin_res.get("interval") or 5
        if interval <= 0:
            interval = 5
        # 实测飞书返回字段为 expires_in（带 s）；兼容 cc-connect 文档里的 expire_in 拼写
        expire_in = begin_res.get("expires_in") or begin_res.get("expire_in") or timeout
        deadline = time.time() + min(expire_in, timeout)

        _show_qr(verify_uri, qr_image)
        user_code = begin_res.get("user_code")
        if user_code:
            eprint(f"{tag}   （如页面要求手输配对码：{user_code}）")
        eprint(f"{tag}③ 等待授权（有效期约 {int(min(expire_in, timeout))}s）...")

        # ---------- 3. poll：轮询直到用户扫码授权 ----------
        while time.time() < deadline:
            poll_res = _call(client, base, "poll", {"device_code": device_code})

            # 国内/国际版切换：扫码人属 lark 租户则切端点继续轮询
            brand = (poll_res.get("user_info") or {}).get("tenant_brand", "").lower()
            if brand == "lark" and base != LARK_BASE:
                base = LARK_BASE
                platform = "lark"
                eprint(f"{tag}   检测到 Lark 租户，切换到国际版端点继续 ...")
                continue

            client_id = poll_res.get("client_id")
            client_secret = poll_res.get("client_secret")
            if client_id and client_secret:
                open_id = (poll_res.get("user_info") or {}).get("open_id", "")
                eprint(f"{tag}✅ 应用创建成功：{client_id}")
                return {
                    "app_id": client_id,
                    "app_secret": client_secret,
                    "open_id": open_id,
                    "platform": platform,
                }

            err = poll_res.get("error", "")
            if err in ("", "authorization_pending"):
                pass  # 继续等
            elif err == "slow_down":
                interval += 5
            elif err == "access_denied":
                raise RuntimeError("用户拒绝了授权。")
            elif err == "expired_token":
                raise RuntimeError("二维码/会话已过期，请重跑。")
            else:
                raise RuntimeError(f"poll 失败：{err} {poll_res.get('error_description', '')}")

            time.sleep(interval)

        raise RuntimeError("等待授权超时（未在有效期内扫码）。")


def main():
    parser = argparse.ArgumentParser(description="飞书扫码自动建应用（设备码注册流）")
    parser.add_argument("--role", default="", help="角色名，仅用于提示显示")
    parser.add_argument("--platform", default="feishu", choices=["feishu", "lark"], help="默认 feishu，poll 时自动切")
    parser.add_argument("--timeout", type=int, default=600, help="等待授权超时（秒），默认 600")
    parser.add_argument("--qr-image", default="", help="把二维码额外存成 PNG（需 pillow），供代跑时 open 给用户扫")
    args = parser.parse_args()

    try:
        result = run_registration(
            platform=args.platform, timeout=args.timeout, role=args.role, qr_image=args.qr_image
        )
    except KeyboardInterrupt:
        eprint("\n已取消。")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        eprint(f"❌ {e}")
        sys.exit(1)

    # stdout 只有这一行，供 shell `read` 捕获
    print(f"{result['app_id']} {result['app_secret']} {result['open_id']}")


if __name__ == "__main__":
    main()
