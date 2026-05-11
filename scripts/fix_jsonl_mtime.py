#!/usr/bin/env python3
"""一次性脚本：把 jsonl 的 mtime 修回"最后一条真实业务消息"的时间戳。

背景：set_session_title / ensure_session_title 会在 jsonl 末尾追加一条 custom-title，
而这条记录用的是 `int(time.time() * 1000)`——批量补标题会让所有 jsonl 的 mtime
被压缩到同一时刻，导致 CLI /resume 列表（按 mtime 倒序）失去真实时间感。

本脚本扫所有 jsonl，找"非 custom-title / ai-title / type 为 user|assistant|attachment|
queue-operation 等"的最后一条业务记录，提取其 timestamp，os.utime 回去。

支持 --dry-run 只打印不改。
"""
import glob
import json
import os
import sys
import time
from datetime import datetime

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# 这些 type 是"标题修补类"，应该被忽略，不能用它们的 ts 当业务时间
META_TYPES = {"custom-title", "ai-title"}

# 这些 type 是"业务/会话"事件，它们的 timestamp 是真实活跃时间
BUSINESS_TYPES = {
    "user", "assistant", "attachment", "queue-operation",
    "system", "thinking", "text", "create", "direct", "message",
    "last-prompt", "permission-mode", "file-history-snapshot",
    "agent-name", "image", "update", "summary",
}


def parse_ts_to_epoch(value) -> float | None:
    """支持毫秒整数或 ISO8601 字符串。返回 epoch 秒（float），失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 毫秒整数（13 位）or 秒
        v = float(value)
        if v > 1e12:  # 毫秒
            return v / 1000.0
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.timestamp()
        except Exception:
            try:
                return float(s)
            except Exception:
                return None
    return None


def last_business_ts(jsonl_path: str) -> float | None:
    """倒序读 jsonl，返回最后一条业务记录的 timestamp（epoch 秒）。"""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 200_000)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if t in META_TYPES:
            continue
        # 排除非业务类型（保守：白名单 + 兜底任何带 timestamp 的非 META 行）
        ts = d.get("timestamp")
        epoch = parse_ts_to_epoch(ts)
        if epoch is None:
            continue
        return epoch
    return None


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    threshold_ts = None
    # 可选：--only-touched-after EPOCH 只处理 mtime 在某时间之后的（避免误改正常文件）
    for i, a in enumerate(sys.argv):
        if a == "--only-touched-after" and i + 1 < len(sys.argv):
            try:
                threshold_ts = float(sys.argv[i + 1])
            except Exception:
                pass

    if dry_run:
        print("[DRY-RUN] 只打印不改 mtime")
    if threshold_ts:
        print(f"[INFO] 只处理 mtime > {datetime.fromtimestamp(threshold_ts)} 的 jsonl")

    paths = []
    for path in glob.iglob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        if "/subagents/" in path:
            continue
        paths.append(path)
    print(f"[INFO] 扫到 {len(paths)} 个 jsonl")

    fixed = 0
    skipped = 0
    no_ts = 0
    for path in paths:
        cur_mtime = os.path.getmtime(path)
        if threshold_ts and cur_mtime <= threshold_ts:
            skipped += 1
            continue
        real_ts = last_business_ts(path)
        if real_ts is None:
            no_ts += 1
            continue
        if abs(real_ts - cur_mtime) < 2:  # 已接近真实，不动
            skipped += 1
            continue
        sid = os.path.splitext(os.path.basename(path))[0]
        delta = cur_mtime - real_ts
        print(f"[FIX] {sid} mtime {datetime.fromtimestamp(cur_mtime)} → "
              f"{datetime.fromtimestamp(real_ts)} (Δ={delta:+.0f}s)")
        if dry_run:
            fixed += 1
            continue
        try:
            os.utime(path, (real_ts, real_ts))
            fixed += 1
        except Exception as e:
            print(f"[WARN] {sid} utime 失败：{e}")

    print()
    print(f"[DONE] 修复 {fixed} / 跳过 {skipped} / 无 ts {no_ts}")


if __name__ == "__main__":
    main()
