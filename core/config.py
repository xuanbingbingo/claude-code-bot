"""统一配置 —— .env 加载 + 平台无关的通用环境变量(BotConfig)。
平台凭证(FEISHU_APP_ID / WECOM_BOT_ID / TELEGRAM_BOT_TOKEN 等)由各 adapter 自行读取。"""
import os
from dataclasses import dataclass, field

from .persona import load_bot_persona


def load_env(script_dir: str | None = None) -> None:
    """加载脚本同目录的 .env(已存在的环境变量优先,与三个旧渠道同款逻辑)。"""
    base = script_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def parse_teammates(raw: str) -> dict:
    """解析 BOT_TEAMMATES:'别名:显示名,...' → {别名: 显示名};省略冒号则别名=显示名。"""
    out: dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        alias, _, disp = item.partition(":")
        alias, disp = alias.strip(), disp.strip()
        if alias:
            out[alias] = disp or alias
    return out


@dataclass
class BotConfig:
    platform: str                       # BOT_PLATFORM(feishu/wecom/telegram/...)
    backend: str                        # BOT_BACKEND(默认 claude-code)
    cwd: str | None                     # CLAUDE_CWD
    persona: str                        # 已解析的人设正文
    bot_name: str                       # BOT_NAME(日志显示)
    relay_enabled: bool                 # BOT_RELAY=1
    relay_max_hops: int                 # BOT_MAX_HOPS
    relay_teammates: dict = field(default_factory=dict)  # 别名→显示名

    @classmethod
    def from_env(cls) -> "BotConfig":
        try:
            max_hops = max(1, int(os.environ.get("BOT_MAX_HOPS", "5") or "5"))
        except ValueError:
            max_hops = 5
        return cls(
            platform=os.environ.get("BOT_PLATFORM", "").strip().lower(),
            backend=os.environ.get("BOT_BACKEND", "claude-code").strip().lower(),
            cwd=os.environ.get("CLAUDE_CWD") or None,
            persona=load_bot_persona(),
            bot_name=os.environ.get("BOT_NAME", "").strip(),
            relay_enabled=os.environ.get("BOT_RELAY", "") == "1",
            relay_max_hops=max_hops,
            relay_teammates=parse_teammates(os.environ.get("BOT_TEAMMATES", "")),
        )
