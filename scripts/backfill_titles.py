#!/usr/bin/env python3
"""一次性脚本：扫所有 jsonl 会话，给"没有 custom-title 也没有 ai-title"的会话补一条 custom-title。

用途：飞书/Telegram 走 `claude --print` 的会话不会被 CLI 自动生成 ai-title，
导致这些会话在 CLI /resume 列表里没有可读标题，看起来像"找不到"。
本脚本扫所有 jsonl，发现缺标题的就用第一条真实用户消息的前 30 字补一个 custom-title。

无 TARGET 列表 — 默认扫全部。可加参数 --dry-run 只打印不写入。
"""
import glob
import json
import os
import sys
import time

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def has_title(jsonl_path: str) -> bool:
    """jsonl 里已有 custom-title 或 ai-title 就视为已有标题。"""
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if '"custom-title"' not in line and '"ai-title"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") in ("custom-title", "ai-title"):
                        if d.get("customTitle") or d.get("aiTitle") or d.get("title"):
                            return True
                except Exception:
                    pass
    except Exception:
        return False
    return False


_CLI_INJECTED_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "Caveat:",
)


def _is_cli_injected(text: str) -> bool:
    t = text.lstrip()
    return any(t.startswith(p) for p in _CLI_INJECTED_PREFIXES)


def first_user_text(jsonl_path: str) -> str:
    """取 jsonl 里第一条真实用户文本。排除 tool_result、CLI 注入开场白、空文本。"""
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '"type":"user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                candidate = ""
                if isinstance(content, str) and content.strip():
                    candidate = content.strip()
                elif isinstance(content, list):
                    has_tool_result = any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )
                    if has_tool_result:
                        continue
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = (b.get("text") or "").strip()
                            if txt:
                                candidate = txt
                                break
                if not candidate or _is_cli_injected(candidate):
                    continue
                return candidate
    except Exception:
        return ""
    return ""


def session_id_from_path(jsonl_path: str) -> str:
    return os.path.splitext(os.path.basename(jsonl_path))[0]


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("[DRY-RUN] 只打印不写入")

    paths = []
    for path in glob.iglob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        if "/subagents/" in path:
            continue
        paths.append(path)
    print(f"[INFO] 扫到 {len(paths)} 个 jsonl")

    fixed = 0
    skipped_has_title = 0
    skipped_no_text = 0
    for path in paths:
        sid = session_id_from_path(path)
        if has_title(path):
            skipped_has_title += 1
            continue
        text = first_user_text(path)
        if not text:
            skipped_no_text += 1
            continue
        title = text.splitlines()[0][:30]
        if not title:
            skipped_no_text += 1
            continue
        print(f"[FIX] {sid} → {title!r}")
        if dry_run:
            fixed += 1
            continue
        entry = {
            "type": "custom-title",
            "sessionId": sid,
            "customTitle": title,
            "timestamp": int(time.time() * 1000),
        }
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fixed += 1
        except Exception as e:
            print(f"[WARN] 写入失败 {sid}: {e}")

    print()
    print(f"[DONE] 修复 {fixed} 个 / 已有标题跳过 {skipped_has_title} / 无文本跳过 {skipped_no_text}")


if __name__ == "__main__":
    main()
