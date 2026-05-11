#!/usr/bin/env python3
"""一次性脚本：把指定 sessionId 的 jsonl 里所有真实用户消息回填到 ~/.claude/history.jsonl。

用途：飞书网关此前没把用户消息记到 history.jsonl，导致 CLI /resume 和飞书
/sessions 都看不到这些会话。本脚本把已存在 jsonl 里的真实用户输入补进 history。
"""
import glob
import json
import os
import sys
from datetime import datetime

HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

TARGET_SIDS = [
    "9b85a2e4-6aba-4bd7-b3a9-8f0d49a2b3a7",  # app开发
    "df233fd6-775c-4144-87e6-35253cbeb3a4",  # 各个平台简介讨论 (今天的部分)
]


def find_jsonl(sid: str) -> str | None:
    for path in glob.iglob(f"{PROJECTS_DIR}/**/{sid}.jsonl", recursive=True):
        if "/subagents/" in path:
            continue
        return path
    return None


def iso_to_ms(ts: str) -> int | None:
    if not ts:
        return None
    try:
        # 形如 2026-04-28T00:14:03.261Z
        dt = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
        return int(dt.timestamp() * 1000)
    except Exception:
        try:
            dt = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            return int(dt.timestamp() * 1000)
        except Exception:
            return None


def is_real_user_msg(d: dict) -> bool:
    if d.get("type") != "user":
        return False
    msg = d.get("message") or {}
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        # 排除全是 tool_result 的；只要有任一 tool_result 元素，就当作非用户输入
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                return False
        # 至少要有一段文本
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text", "").strip():
                return True
        return False
    return False


def extract_display(d: dict) -> str:
    msg = d.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return (item.get("text") or "")[:200]
    return ""


def already_logged_keys() -> set[tuple[str, int]]:
    """已经在 history.jsonl 里出现过的 (sessionId, timestamp) 集合，避免重复追加。"""
    keys: set[tuple[str, int]] = set()
    if not os.path.exists(HISTORY_PATH):
        return keys
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                sid = e.get("sessionId")
                ts = e.get("timestamp")
                if sid and isinstance(ts, int):
                    keys.add((sid, ts))
            except Exception:
                pass
    return keys


def main() -> None:
    existing = already_logged_keys()
    print(f"[INFO] history.jsonl 现有条目：{len(existing)}")

    appended_total = 0
    with open(HISTORY_PATH, "a", encoding="utf-8") as out:
        for sid in TARGET_SIDS:
            jsonl = find_jsonl(sid)
            if not jsonl:
                print(f"[SKIP] 找不到会话文件：{sid}")
                continue
            print(f"[SCAN] {sid} → {jsonl}")
            cwd = None
            entries: list[dict] = []
            with open(jsonl, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if cwd is None and d.get("cwd"):
                        cwd = d.get("cwd")
                    if not is_real_user_msg(d):
                        continue
                    ts_ms = iso_to_ms(d.get("timestamp", ""))
                    if ts_ms is None:
                        continue
                    display = extract_display(d).strip()
                    if not display:
                        continue
                    entries.append({"display": display, "ts": ts_ms})

            if not cwd:
                print(f"[WARN] {sid} 找不到 cwd，跳过")
                continue
            print(f"[INFO] {sid} cwd={cwd} 真实用户消息 {len(entries)} 条")

            appended = 0
            for e in entries:
                key = (sid, e["ts"])
                if key in existing:
                    continue
                line = json.dumps({
                    "display": e["display"],
                    "pastedContents": {},
                    "timestamp": e["ts"],
                    "project": cwd,
                    "sessionId": sid,
                }, ensure_ascii=False)
                out.write(line + "\n")
                existing.add(key)
                appended += 1
            print(f"[DONE] {sid} 追加 {appended} 条")
            appended_total += appended

    print(f"\n[DONE] 总计追加 {appended_total} 条到 {HISTORY_PATH}")


if __name__ == "__main__":
    main()
