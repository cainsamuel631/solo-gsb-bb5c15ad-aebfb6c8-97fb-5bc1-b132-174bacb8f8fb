"""WebVTT / SRT 解析、分词、语言检测与时间码工具。

该模块不依赖 Web 框架，便于单独测试。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# ---------------------------------------------------------------------------
# 时间码
# ---------------------------------------------------------------------------

# 同时兼容 HH:MM:SS.mmm / MM:SS.mmm，以及 SRT 使用的逗号
_TS_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)


def parse_timestamp(value: str) -> int:
    """把时间码解析为毫秒，无法解析时抛出 ValueError。"""
    m = _TS_RE.search(value.strip())
    if not m:
        raise ValueError(f"无法解析时间码: {value!r}")
    hh = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    frac = m.group(4).ljust(3, "0")
    return hh * 3_600_000 + mm * 60_000 + ss * 1_000 + int(frac)


def format_timestamp(ms: int) -> str:
    """格式化为 WebVTT 时间码 HH:MM:SS.mmm。"""
    ms = max(0, int(round(ms)))
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, milli = divmod(rem, 1_000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{milli:03d}"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str

    def to_dict(self) -> dict:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text}


# ---------------------------------------------------------------------------
# WebVTT / SRT 解析
# ---------------------------------------------------------------------------

_TIME_LINE_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>")          # <c> <b> <00:00:01.000> 等
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")    # {\an8} 一类覆盖标签


def _clean_text_line(line: str) -> str:
    line = _TAG_RE.sub("", line)
    line = _ASS_TAG_RE.sub("", line)
    return line.strip()


def parse_subtitles(content: str) -> List[Cue]:
    """解析 WebVTT 或 SRT 内容，按时间顺序返回 cue 列表。

    支持：WEBVTT 文件头、NOTE/STYLE/REGION 块、cue 标识符、SRT 数字序号、
    内联样式标签、\\r\\n 换行、BOM。
    """
    content = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    is_vtt = content.lstrip().upper().startswith("WEBVTT")

    cues: List[Cue] = []
    blocks = re.split(r"\n[ \t]*\n", content.strip("\n"))

    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        header = lines[0].upper()
        if is_vtt and (
            header.startswith("WEBVTT")
            or header.startswith("NOTE")
            or header.startswith("STYLE")
            or header.startswith("REGION")
        ):
            continue

        time_idx = -1
        for i, line in enumerate(lines):
            if "-->" in line and _TIME_LINE_RE.search(line):
                time_idx = i
                break
        if time_idx < 0:
            continue

        m = _TIME_LINE_RE.search(lines[time_idx])
        start_ms = _build_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end_ms = _build_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        if end_ms <= start_ms:
            # 异常时间码：保持宽容，给出 1ms，避免后续除零
            end_ms = start_ms + 1

        text_lines = [_clean_text_line(ln) for ln in lines[time_idx + 1 :]]
        text = "\n".join(t for t in text_lines if t)
        if not text:
            continue
        cues.append(Cue(start_ms=start_ms, end_ms=end_ms, text=text))

    cues.sort(key=lambda c: c.start_ms)
    return cues


def _build_ms(hh, mm, ss, frac) -> int:
    return (
        int(hh or 0) * 3_600_000
        + int(mm) * 60_000
        + int(ss) * 1_000
        + int(frac.ljust(3, "0"))
    )


# ---------------------------------------------------------------------------
# 分词 / 重组
# ---------------------------------------------------------------------------

# 拉丁字母词（含缩写与连字符）| CJK 单字（含假名/谚文及标点）| 其余非空白字符
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*"
    r"|[一-鿿㐀-䶿぀-ヿ가-힯　-〿＀-￯]"
    r"|[^\s]"
)


def tokenize(text: str) -> List[str]:
    """把 cue 文本切成 token：拉丁语单词为整体，CJK 单字成 token。"""
    flat = text.replace("\n", " ")
    return _TOKEN_RE.findall(flat)


def is_latin_word(token: str) -> bool:
    ch = token[0]
    return ch.isascii() and ch.isalnum()


def is_cjk_letter(token: str) -> bool:
    """汉字 / 假名 / 谚文（不含标点）。"""
    ch = token[0]
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


# 不宜出现在行首的收尾标点（中英）
NO_LEAD_PUNCT = set(".,;:!?)]}，。！？；：、”’）】》…")


def needs_space(prev: str, tok: str) -> bool:
    """两个 token 之间是否需要空格。"""
    if is_latin_word(prev) and is_latin_word(tok):
        return True
    # 拉丁词与汉字/假名/谚文相邻：按排版惯例补空格
    if (is_latin_word(prev) and is_cjk_letter(tok)) or (
        is_cjk_letter(prev) and is_latin_word(tok)
    ):
        return True
    # ASCII 标点（句号、逗号等）后接拉丁词：补空格（"end. Start"）
    if tok and is_latin_word(tok) and prev in NO_LEAD_PUNCT and prev.isascii():
        return True
    return False


def join_tokens(tokens: List[str]) -> str:
    """tokenize 的逆操作。"""
    parts: List[str] = []
    prev = ""
    for tok in tokens:
        if parts and needs_space(prev, tok):
            parts.append(" ")
        parts.append(tok)
        prev = tok
    return "".join(parts)


def token_weight(token: str) -> int:
    """token 对行长的贡献；拉丁词预留一个尾随空格。"""
    return len(token) + (1 if is_latin_word(token) else 0)


def wrap_lines(text: str, max_chars: int) -> List[str]:
    """按 token 贪心折行（尽量不破坏单词），返回行列表。"""
    tokens = tokenize(text)
    if not tokens:
        return []
    lines: List[str] = []
    current: List[str] = []
    current_len = 0
    for tok in tokens:
        gap = 1 if current and needs_space(current[-1], tok) else 0
        overflow = current and current_len + gap + len(tok) > max_chars
        # 收尾标点不宜落到行首：允许超 1 字附在行尾
        if overflow and tok in NO_LEAD_PUNCT and current_len + gap + len(tok) <= max_chars + 1:
            overflow = False
        if overflow:
            lines.append(join_tokens(current))
            current = [tok]
            current_len = len(tok)
        else:
            current.append(tok)
            current_len += gap + len(tok)
    if current:
        lines.append(join_tokens(current))
    return lines


# ---------------------------------------------------------------------------
# 语言检测（基于 Unicode 区段的启发式）
# ---------------------------------------------------------------------------

LANG_LABELS = {
    "zh": "中文（汉字）",
    "ja": "日语",
    "ko": "韩语",
    "en": "拉丁字母",
    "ru": "西里尔字母",
    "ar": "阿拉伯字母",
    "other": "其他",
}


def _script_scores(text: str) -> dict:
    scores = {"zh": 0, "ja": 0, "ko": 0, "en": 0, "ru": 0, "ar": 0}
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:          # 平假名 / 片假名
            scores["ja"] += 3
        elif 0xAC00 <= code <= 0xD7AF:        # 谚文音节
            scores["ko"] += 3
        elif 0x1100 <= code <= 0x11FF:        # 谚文字母
            scores["ko"] += 3
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            scores["zh"] += 1                  # 汉字（日语汉字需靠假名判定）
        elif 0x0400 <= code <= 0x04FF:
            scores["ru"] += 1
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            scores["ar"] += 1
        elif ch.isalpha():
            scores["en"] += 1
    return scores


def detect_lang(text: str) -> str:
    scores = _script_scores(text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"
