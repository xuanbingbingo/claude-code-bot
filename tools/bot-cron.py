#!/usr/bin/env python3
"""定时任务触发器 —— 让某个网关 bot 在定时点主动跑一轮,把结果推回指定飞书会话。

网关本身只被动响应消息;cc-connect 时代的 cron(老陈盘前/盘后简报、选题官早报)迁到这里,
由 launchd 按时调用:

    bot-cron.py --role laochen --to ou_xxx --prompt-file ~/aiProjects/bots/laochen/cron/premarket.txt

- 人设/工作目录/飞书凭证全部读 .env.<role>,与该 bot 在网关里的行为一致
- 该会话在网关里有持久化指针(.sessions-<app_id>.json)且 jsonl 还在时,--resume 接进同一条会话,
  用户事后追问「展开讲讲」网关能接上上下文;没有就开新会话
- 注入 FEISHU_SENDER_OPEN_ID 等,bot 跑 feishu-send-file 时文件直接发到 --to 这个会话
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHUNK = 8000          # 飞书卡片 markdown 单条留足余量,超长分条发


def load_env_file(path: str) -> dict:
    env = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("  #", 1)[0].strip().strip('"')
    return env


def resume_args(app_id: str, conv_id: str) -> list:
    import claude_core as cc
    state = os.path.join(ROOT, f".sessions-{app_id}.json")
    try:
        with open(state, encoding="utf-8") as f:
            sid = (json.load(f) or {}).get(conv_id)
    except Exception:
        return []
    return ["--resume", sid] if sid and cc._session_file_exists(sid) else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, help="读取 .env.<role>")
    ap.add_argument("--to", required=True, help="推送目标:单聊 open_id(ou_) 或群 chat_id(oc_)")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    os.environ.update(load_env_file(os.path.join(ROOT, f".env.{args.role}")))
    from core.persona import load_bot_persona
    from core.runtime_rules import RUNTIME_RULES
    from adapters.feishu.api import FeishuAPI

    app_id, secret = os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"]
    id_type = "chat_id" if args.to.startswith("oc_") else "open_id"
    tool = os.path.join(ROOT, "tools", "feishu-send-file.py")
    append = "\n\n".join(p for p in (
        load_bot_persona(),
        f"你运行在飞书机器人网关里(本轮是定时任务触发,最终回复会自动推送给用户)。"
        f"要把本地文件/图片/视频发到飞书,直接执行:\n    python3 {tool} <文件绝对路径>",
        RUNTIME_RULES) if p)
    with open(os.path.expanduser(args.prompt_file), encoding="utf-8") as f:
        prompt = f.read().strip()

    cmd = (["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions",
            "--append-system-prompt", append] + resume_args(app_id, args.to))
    env = dict(os.environ, FEISHU_SENDER_OPEN_ID=args.to, FEISHU_SENDER_ID_TYPE=id_type)
    cwd = os.environ.get("CLAUDE_CWD") or os.path.expanduser("~")
    api = FeishuAPI(app_id, secret)
    try:
        out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=args.timeout)
        data = json.loads(out.stdout or "{}")
        text = data.get("result") or f"⚠️ 定时任务没有产出(退出码 {out.returncode})\n{out.stderr[-1500:]}"
    except Exception as e:
        text = f"⚠️ 定时任务失败:{e}"
    for i in range(0, len(text), CHUNK):
        api.send_card(args.to, text[i:i + CHUNK], receive_id_type=id_type)
    print(f"[bot-cron] role={args.role} sent {len(text)} chars: {text[:200]!r}")


if __name__ == "__main__":
    main()
