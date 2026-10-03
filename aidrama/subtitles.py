"""中文字幕：ASS（烧录用）+ SRT（上传平台/剪映导入用）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cue:
    start: float
    end: float
    text: str
    speaker: str = ""


def _ts_ass(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ts_srt(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_PUNCT_END = "。！？!?…"
_PUNCT_SOFT = "，,、；;：:"
_PUNCT_ALL = _PUNCT_END + _PUNCT_SOFT + "。.\"'“”‘’」』）)—"


def wrap_zh(text: str, max_chars: int = 14) -> list[str]:
    """把一句中文切成适合竖屏的若干短句（每条字幕最多两行，每行 ≤ max_chars）。

    竖屏短剧的惯例：字幕不带句末标点，逗号处断开，每屏 1~2 行。
    """
    text = re.sub(r"\s+", " ", text.strip())
    # 先按强标点切句
    parts: list[str] = []
    for p in re.split(f"(?<=[{_PUNCT_END}])", text):
        if not p.strip():
            continue
        if parts and all(ch in _PUNCT_ALL for ch in p.strip()):
            parts[-1] += p.strip()          # “！！”“……！”“”等纯标点并回上一句
        else:
            parts.append(p)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        while len(p) > max_chars:
            # 优先在弱标点处断开
            cut = -1
            for i in range(min(len(p) - 1, max_chars), max(0, max_chars // 2) - 1, -1):
                if p[i] in _PUNCT_SOFT:
                    cut = i + 1
                    break
            if cut <= 0:
                cut = max_chars
                while cut < len(p) and p[cut] in _PUNCT_ALL:
                    cut += 1                # 硬切时不让下一行以标点开头
            out.append(p[:cut])
            p = p[cut:].strip()
        if p:
            out.append(p)
    # 去掉每段末尾的标点（短剧字幕常规做法），保留问号/感叹号的语气
    cleaned = []
    for seg in out:
        seg = seg.rstrip("，,。、；;：: ")
        if not seg:
            continue
        # 极短的碎句（“好。好。”）并进上一条，用空格隔开，避免字幕一闪而过
        if cleaned and (len(seg) <= 3 or len(cleaned[-1]) <= 3) and len(cleaned[-1]) + 1 + len(seg) <= max_chars:
            cleaned[-1] = f"{cleaned[-1]} {seg}"
        else:
            cleaned.append(seg)
    return cleaned


def split_cue(cue: Cue, max_chars: int = 14) -> list[Cue]:
    segs = wrap_zh(cue.text, max_chars)
    if len(segs) <= 1:
        return [Cue(cue.start, cue.end, segs[0] if segs else cue.text, cue.speaker)]
    total = sum(len(s) for s in segs)
    dur = cue.end - cue.start
    t = cue.start
    res = []
    for s in segs:
        d = dur * len(s) / total
        res.append(Cue(t, t + d, s, cue.speaker))
        t += d
    return res


def write_srt(cues: list[Cue], path: str | Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_ts_srt(c.start)} --> {_ts_srt(c.end)}", c.text, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)


def write_ass(
    cues: list[Cue],
    path: str | Path,
    width: int = 1080,
    height: int = 1920,
    font: str = "Source Han Sans SC",
    font_size: int = 64,
    margin_v: int = 520,
    outline: float = 4.0,
) -> Path:
    """ASS 字幕。margin_v 是距底边像素：竖屏平台底部约 20%~25% 会被标题/简介/进度条遮挡，
    默认 520px（1920 高）让字幕落在画面下三分之一、UI 遮挡区之上。"""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,{outline},1.5,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for c in cues:
        text = c.text.replace("\n", "\\N").replace("{", "（").replace("}", "）")
        ev.append(f"Dialogue: 0,{_ts_ass(c.start)},{_ts_ass(c.end)},Default,{c.speaker},0,0,0,,{text}")
    Path(path).write_text(header + "\n".join(ev) + "\n", encoding="utf-8-sig")
    return Path(path)
