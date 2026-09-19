"""VoiceService —— 文本转语音收口(平台无关)。

回复文本 → 清洗(去 markdown/代码块/URL) → 多音字修正 → hfvoice 合成 wav →
ffmpeg 转 opus → 返回 VoiceClip(路径 + 毫秒时长)。平台怎么发由各 adapter 的 send_voice 决定。

⚠️ 三条来自实测的硬约束:
1. 飞书语音消息只认 opus(单声道/16k),wav 直接传上去能上传成功但发出来是「文件」不是语音条。
2. edge-tts 在本机约 50% 概率 TLS reset,且**必须去掉 HTTP(S)_PROXY**(走代理必挂)——
   故合成一律在剥干净代理的子环境里跑,并重试 3 次。sami(剪映)引擎不受代理影响但属逆向接口。
3. 合成是秒级阻塞(短句 ~3.6s),绝不能挡住对话主流程 —— Gateway 以 create_task 发射即忘。
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

# hfvoice 可执行文件:默认取 PATH 里的 hfvoice;HFVOICE_BIN 可指到具体路径
# (某些机器上 /opt/homebrew/bin/hfvoice 是从别处拷来的壳,内部路径写死指向别人的家目录)
_HFVOICE_BIN = os.environ.get("HFVOICE_BIN", "").strip() or "hfvoice"
_POLYPHONES = os.path.expanduser("~/aiProjects/koubo-subtitle-kit/polyphones.json")
_VOICES_JSON = os.path.expanduser("~/aiProjects/hf-voice/voices.json")
_KOKORO_MODELS = os.path.expanduser("~/aiProjects/hf-voice/kokoro_models")

# 代理相关变量全部剥掉:edge-tts 走 AntProxy 必然握手失败(见 hfvoice 记忆)
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")


def kokoro_available() -> bool:
    """kokoro 是本地引擎,模型文件曾在一次清理里丢失 —— 缺文件时选它会直接报错没声音。
    故动态探测:模型在就放行,不在就从可选音色里摘掉(而不是写死「kokoro 不可用」)。"""
    return (os.path.isdir(_KOKORO_MODELS)
            and any(f.endswith(".bin") for f in os.listdir(_KOKORO_MODELS)))


def load_voice_catalog() -> list[dict]:
    """读 hfvoice 的 voices.json(唯一真相源) → [{name, engine, alias, usable}]。"""
    try:
        with open(_VOICES_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] 读取音色表失败:{e}")
        return []
    kok = kokoro_available()
    out = []
    for name, meta in data.items():
        if name.startswith("_") or not isinstance(meta, dict):
            continue
        engine = meta.get("engine", "")
        out.append({"name": name, "engine": engine,
                    "alias": [str(a) for a in (meta.get("alias") or [])],
                    "usable": kok if engine == "kokoro" else True})
    return out


def resolve_voice(query: str) -> dict | None:
    """把用户输入(音色名/别名/编号)解析成目录项;解析不到返回 None。"""
    q = (query or "").strip().lower()
    if not q:
        return None
    for v in load_voice_catalog():
        if q == v["name"].lower() or q in [a.lower() for a in v["alias"]]:
            return v
    return None


@dataclass
class VoiceClip:
    path: str
    duration_ms: int


# ---------- 文本清洗:念给人听的稿子 ≠ 屏幕上的 markdown ----------
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_URL = re.compile(r"https?://\S+")
_IMG_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_HEADING = re.compile(r"^#{1,6}\s*", re.M)
_LIST_MARK = re.compile(r"^\s*[-*+]\s+", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|~~)")
# 常见装饰性 emoji / 符号(念出来是噪音);范围覆盖表情、符号、旗帜、装饰箭头
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
                    "←-⇿⬀-⯿️✅❌⚠]")
_MULTI_NL = re.compile(r"\n{2,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def clean_for_speech(text: str) -> str:
    """把 markdown 回复压成能念的白话。代码块整块丢弃 —— 逐字念代码毫无意义还极长。"""
    t = text or ""
    t = _CODE_BLOCK.sub("（代码略）", t)
    t = _IMG_LINK.sub(r"\1", t)          # [文字](链接) 只留文字
    t = _URL.sub("（链接）", t)
    t = _TABLE_ROW.sub("", t)            # 表格念不出来,整行删
    t = _INLINE_CODE.sub(r"\1", t)
    t = _HEADING.sub("", t)
    t = _LIST_MARK.sub("", t)
    t = _EMPHASIS.sub("", t)
    t = _EMOJI.sub("", t)
    t = t.replace("---", "").replace("|", " ")
    t = _MULTI_SPACE.sub(" ", t)
    t = _MULTI_NL.sub("\n", t)
    return t.strip()


def _strip_lookaround(pattern: str) -> str:
    """剥掉 (?=...) (?!...) (?<=...) (?<!...) 整段,用于判断「真正消耗字符的捕获组」。

    为什么要这么费劲:词典规则如 `一行(?!(代码|没动|判断))`,lookahead 内部也算一个捕获组,
    直接用 re.groups 判会把它误判成「带捕获组」而跳过 —— 而它实际上只匹配「一行」两个字,
    整体替换是安全的。真正危险的是 `塞(进|回|到)` 这种:整体替换会把「进」一起吃掉(丢字)。
    """
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("(?", i) and (
                pattern[i + 2:i + 3] in ("=", "!") or pattern[i + 2:i + 4] in ("<=", "<!")):
            depth, j = 0, i
            while j < n:                       # 括号配对扫描,跳过转义的括号
                if pattern[j] == "\\":
                    j += 2
                    continue
                if pattern[j] == "(":
                    depth += 1
                elif pattern[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        out.append(pattern[i])
        i += 1
    return "".join(out)


def _load_autofix_rules():
    """只取词典里 verdict=auto_fix 的规则(有确定解法的)。

    needs_review 那类(儿化音等)要靠语义判断,实时对话没法停下来问用户 —— 放行不拦,
    否则 bot 每条回复都可能因为一个「这儿」发不出语音。词典是唯一真相源,缺失就静默跳过。

    🔴 带「消耗性捕获组」的规则一律跳过:整体替换会把捕获的那个字吃掉(塞进→腮,丢了「进」)。
    这类改音不改字的规则走下面的 _BUILTIN_SPEECH_MAP,显式写清楚保留哪部分。
    """
    try:
        with open(_POLYPHONES, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for r in data.get("rules", []):
        if r.get("verdict") != "auto_fix":
            continue
        rep = r.get("replace") or []
        pat = r.get("pattern")
        if not pat or not rep:
            continue
        try:
            if re.compile(_strip_lookaround(pat)).groups:
                continue                       # 见上:防丢字
            out.append((re.compile(pat), str(rep[0])))
        except re.error:
            continue
    return out


# 改音不改字的内置补充:词典里 replace 为空(它记的是读音判例,不是文本替换),
# 但合成稿必须改,否则念成 sè。🔴 只进 TTS,绝不进发给用户的文字。
_BUILTIN_SPEECH_MAP = [
    (re.compile(r"塞(?=[进回到满对]|不下|得下)"), "腮"),
]


_AUTOFIX = None


def fix_polyphones(text: str) -> str:
    """应用多音字自动修正(如 一行→一句、塞进→腮进)。

    🔴 这是「改音不改字」的合成稿专用处理,只进 TTS,不影响发给用户的文字消息。
    """
    global _AUTOFIX
    if _AUTOFIX is None:
        _AUTOFIX = _load_autofix_rules()
    for pat, rep in list(_AUTOFIX) + _BUILTIN_SPEECH_MAP:
        try:
            text = pat.sub(rep, text)
        except Exception:
            continue
    return text


class VoiceService:
    """会话级开关 + 合成管线。开关按 conv_id 落盘,重启不丢。"""

    def __init__(self, enabled_default: bool = True, voice_name: str = "",
                 max_chars: int = 600, state_key: str = "", state_dir: str | None = None):
        self.enabled_default = enabled_default
        self.voice_name = voice_name          # 空 = 用 hfvoice 自己的默认音色(清爽男声)
        self.max_chars = max(0, max_chars)
        self._prefs: dict[str, dict] = {}     # conv_id -> {"on": bool, "voice": str}
        self._state_file = None
        if state_key:
            base = state_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._state_file = os.path.join(base, f".voice-{state_key}.json")
            self._load()

    # ---- 开关持久化 ----
    def _load(self):
        """兼容旧格式:早期落盘是 {conv_id: bool},现在是 {conv_id: {on, voice}}。"""
        try:
            if not (self._state_file and os.path.isfile(self._state_file)):
                return
            with open(self._state_file, encoding="utf-8") as f:
                raw = json.load(f) or {}
            for k, v in raw.items():
                self._prefs[k] = ({"on": bool(v), "voice": ""} if isinstance(v, bool)
                                  else {"on": bool(v.get("on", True)),
                                        "voice": str(v.get("voice") or "")})
        except Exception as e:
            print(f"[WARN] 加载语音偏好失败:{e}")

    def _save(self):
        if not self._state_file:
            return
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._prefs, f, ensure_ascii=False)
        except Exception as e:
            print(f"[WARN] 保存语音偏好失败:{e}")

    def _entry(self, conv_id: str) -> dict:
        return self._prefs.get(conv_id) or {}

    def is_on(self, conv_id: str) -> bool:
        return bool(self._entry(conv_id).get("on", self.enabled_default))

    def set(self, conv_id: str, on: bool):
        e = self._entry(conv_id)
        self._prefs[conv_id] = {"on": bool(on), "voice": e.get("voice", "")}
        self._save()

    def voice_for(self, conv_id: str) -> str:
        """会话级音色 > 本 bot 的 BOT_VOICE_NAME > hfvoice 默认(空串)。"""
        return self._entry(conv_id).get("voice") or self.voice_name

    def set_voice(self, conv_id: str, name: str):
        e = self._entry(conv_id)
        self._prefs[conv_id] = {"on": bool(e.get("on", self.enabled_default)), "voice": name or ""}
        self._save()

    # ---- 合成 ----
    def prepare_text(self, text: str) -> str:
        """清洗 + 修正 + 截断。返回空串表示这条不值得念(纯代码/纯链接等)。"""
        t = fix_polyphones(clean_for_speech(text))
        if len(t) < 2:
            return ""
        if self.max_chars and len(t) > self.max_chars:
            cut = t[:self.max_chars]
            # 尽量断在句末,避免半句戛然而止
            for sep in ("。", "！", "？", "\n", "；", "，"):
                pos = cut.rfind(sep)
                if pos > self.max_chars * 0.6:
                    cut = cut[:pos + 1]
                    break
            t = cut + "后面还有内容，请看文字。"
        return t

    async def synthesize(self, text: str, voice: str | None = None) -> VoiceClip | None:
        speech = self.prepare_text(text)
        if not speech:
            return None
        return await asyncio.to_thread(self._synth_blocking, speech,
                                       self.voice_name if voice is None else voice)

    # ---- 以下在线程里跑(subprocess 阻塞) ----
    def _synth_blocking(self, speech: str, voice: str = "") -> VoiceClip | None:
        wav = opus = ""
        try:
            wav = self._tts(speech, voice)
            if not wav:
                return None
            opus = self._to_opus(wav)
            if not opus:
                return None
            return VoiceClip(path=opus, duration_ms=self._duration_ms(opus))
        except Exception as e:
            print(f"[WARN] 语音合成失败:{e}")
            return None
        finally:
            if wav:
                _unlink(wav)

    def _tts(self, speech: str, voice: str = "") -> str:
        """hfvoice 合成 wav。剥掉代理(edge-tts 走代理必挂) + 重试 3 次(TLS reset 常见)。"""
        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="botvoice_")
        os.close(fd)
        env = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
        cmd = [_HFVOICE_BIN, speech, wav]
        if voice:
            cmd += ["-v", voice]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
                if r.returncode == 0 and os.path.getsize(wav) > 1024:
                    return wav
                print(f"[WARN] hfvoice 第{attempt+1}次失败:{(r.stderr or r.stdout).strip()[:160]}")
            except subprocess.TimeoutExpired:
                print(f"[WARN] hfvoice 第{attempt+1}次超时")
            except Exception as e:
                print(f"[WARN] hfvoice 第{attempt+1}次异常:{e}")
        _unlink(wav)
        return ""

    @staticmethod
    def _to_opus(wav: str) -> str:
        """飞书语音条要求 opus;单声道 16k 32kbps 足够人声,体积也小(2.6s ≈ 8KB)。"""
        fd, opus = tempfile.mkstemp(suffix=".opus", prefix="botvoice_")
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", wav,
             "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "16000", "-vbr", "on", opus],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(opus) or os.path.getsize(opus) < 128:
            print(f"[WARN] opus 转码失败:{r.stderr.strip()[:200]}")
            _unlink(opus)
            return ""
        return opus

    @staticmethod
    def _duration_ms(path: str) -> int:
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", path],
                               capture_output=True, text=True, timeout=30)
            return max(1, int(float(r.stdout.strip()) * 1000))
        except Exception:
            return 1000


def _unlink(path: str):
    try:
        os.unlink(path)
    except Exception:
        pass
