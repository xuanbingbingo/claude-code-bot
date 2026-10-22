"""人设加载 —— 三个旧渠道里逐字相同的 _load_bot_persona,收口到此。"""
import os


def load_bot_persona() -> str:
    """加载本 bot 人设,作为 --append-system-prompt 注入。
    优先级:BOT_PERSONA(内联) > BOT_PERSONA_FILE(角色 .md,自动去 YAML frontmatter)。
    BOT_PERSONA_FILE 可用逗号分隔多个文件(如 角色 agent.md + 本 bot 记忆纪律),按顺序拼接。"""
    inline = os.environ.get("BOT_PERSONA", "").strip()
    if inline:
        return inline
    paths = [p.strip() for p in os.environ.get("BOT_PERSONA_FILE", "").split(",") if p.strip()]
    return "\n\n---\n\n".join(t for t in (_read_persona_file(p) for p in paths) if t)


def _read_persona_file(path: str) -> str:
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        print(f"[WARN] 读取人设文件失败 {path}: {e}")
        return ""
    if raw.startswith("---"):                  # 去掉 YAML frontmatter,只留正文
        seg = raw.split("---", 2)
        if len(seg) == 3:
            raw = seg[2]
    return raw.strip()
