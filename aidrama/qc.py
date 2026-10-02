"""自动质检：对每条生成结果做技术检查 + 对白回读，给出是否通过与问题清单。

对应平台审片硬伤：静止/重复/掉帧、黑场、口型/台词错位、机械配音……
  L0 技术：时长、分辨率、是否有声音、黑场（blackdetect）、冻帧（freezedetect）
  L2 对白：用 Qwen3-ASR 回读本段音频，与剧本台词算字错率 CER
人脸一致性（ArcFace）与 VLM 打分不在默认依赖里，留作扩展（见 docs/05-质检与返工.md）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import ffmpeg_utils as ff

_PUNCT = re.compile(r"[\s，。！？、,.!?…“”\"'：:；;（）()《》<>—\-~～]")


def cer(ref: str, hyp: str) -> float:
    a, b = _PUNCT.sub("", ref), _PUNCT.sub("", hyp)
    if not a:
        return 0.0 if not b else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] / len(a)


def _detect(path: str | Path, vf: str, key: str) -> list[str]:
    cmd = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-nostats", "-i", str(path), "-vf", vf, "-an", "-f", "null", "-"]
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [ln for ln in p.stderr.splitlines() if key in ln]


def technical(path: str | Path, expect_seconds: float, expect_audio: bool = True) -> list[str]:
    issues = []
    try:
        d = ff.duration(path)
    except ff.FFmpegError as e:
        return [f"无法读取: {e}"]
    if d + 0.15 < expect_seconds:
        issues.append(f"时长 {d:.2f}s 短于预期 {expect_seconds:.2f}s")
    if expect_audio and not ff.has_audio(path):
        issues.append("没有音轨")
    if _detect(path, "blackdetect=d=0.4:pix_th=0.08", "black_start"):
        issues.append("有黑场")
    freezes = _detect(path, "freezedetect=n=0.002:d=1.5", "freeze_start")
    if freezes:
        issues.append(f"有 {len(freezes)} 处冻帧（≥1.5s 画面静止）")
    return issues


def detect_cuts(path: str | Path, threshold: float = 0.35) -> list[float]:
    """用 ffmpeg 场景变化分数找硬切点（秒）。H3 段内的 [Shot N] 切镜是硬切，分数通常 > 0.5。"""
    cmd = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-nostats", "-i", str(path), "-an",
           "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = []
    for ln in p.stderr.splitlines():
        if "showinfo" in ln and "pts_time:" in ln:
            try:
                out.append(round(float(ln.split("pts_time:")[1].split()[0]), 3))
            except (IndexError, ValueError):
                pass
    return out


def cuts(path: str | Path, planned: list[float], tolerance: float = 0.35) -> list[str]:
    """对照规划的切点（段内各镜头起点，不含 0）检查：缺切 = 硬伤；多出来的切点只作提示（运镜、闪光也可能触发）。"""
    want = [t for t in planned if t > 0.05]
    if not want:
        return []
    got = detect_cuts(path)
    issues = []
    missing = [t for t in want if not any(abs(t - g) <= tolerance for g in got)]
    if missing:
        near = ", ".join(f"{t:.2f}s" for t in missing)
        issues.append(f"切镜缺失或偏移：计划在 {near} 切镜，未检测到（检测到 {[round(g, 2) for g in got]}）")
    extra = [g for g in got if not any(abs(g - t) <= tolerance for t in want)]
    if extra:
        issues.append(f"提示：检测到计划外的画面突变 {[round(g, 2) for g in extra]}（可能是多切或闪烁）")
    return issues


def is_hard(issue: str) -> bool:
    """判定是否算不合格：提示类和“ASR 不可用”不算。"""
    return not (issue.startswith("提示") or issue.startswith("ASR 不可用"))


def dialogue(audio_client, clip: str | Path, lines: list[tuple[str, float, float]], language: str = "zh",
             max_cer: float = 0.15, work: str | Path | None = None) -> tuple[list[str], float | None]:
    """lines: [(台词, 起点秒, 终点秒)]（相对片段）。整段回读后与全部台词拼接比对。"""
    if not lines:
        return [], None
    work = Path(work or Path(clip).parent)
    work.mkdir(parents=True, exist_ok=True)
    wav = work / (Path(clip).stem + "_qc.wav")
    start = max(0.0, min(s for _, s, _ in lines) - 0.2)
    end = max(e for _, _, e in lines) + 0.4
    ff.run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    expect = "".join(t for t, _, _ in lines)
    try:
        hyp = audio_client.asr(wav, language=language, expect=expect)
    except Exception as e:  # noqa: BLE001
        return [f"ASR 不可用，跳过对白质检: {e}"], None
    c = cer(expect, hyp)
    issues = [f"对白字错率 {c:.0%} > {max_cer:.0%}（识别：{hyp}）"] if c > max_cer else []
    return issues, c
