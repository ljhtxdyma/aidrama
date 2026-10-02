"""Thin ffmpeg/ffprobe helpers (subprocess based, no Python deps)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    exe = os.environ.get("FFMPEG", "ffmpeg")
    if not shutil.which(exe) and not Path(exe).exists():
        raise FFmpegError("找不到 ffmpeg，请安装并加入 PATH（Windows: winget install Gyan.FFmpeg）")
    return exe


def ffprobe_bin() -> str:
    return os.environ.get("FFPROBE", "ffprobe")


def run(args: list[str], quiet: bool = True) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"] + (["-loglevel", "error"] if quiet else []) + args
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({p.returncode}): {' '.join(cmd)[:2000]}\n{p.stderr[-4000:]}")


def probe(path: str | Path) -> dict:
    p = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}: {p.stderr}")
    return json.loads(p.stdout)


def duration(path: str | Path) -> float:
    info = probe(path)
    d = info.get("format", {}).get("duration")
    if d is None:
        for s in info.get("streams", []):
            if s.get("duration"):
                return float(s["duration"])
        raise FFmpegError(f"cannot read duration of {path}")
    return float(d)


def has_audio(path: str | Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def video_size(path: str | Path) -> tuple[int, int]:
    for s in probe(path).get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise FFmpegError(f"no video stream in {path}")


def last_frame(video: str | Path, out_png: str | Path) -> Path:
    """抽取视频最后一帧（用于下一个镜头续写 / 首帧衔接）。"""
    run(["-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", str(out_png)])
    return Path(out_png)


def frame_at(video: str | Path, t: float, out_png: str | Path) -> Path:
    """抽取 t 秒处的一帧（-ss 放在 -i 前面是关键帧快速定位，再精确解码到 t）。"""
    run(["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1", "-update", "1", str(out_png)])
    if not Path(out_png).exists():      # t 超出视频长度时退回最后一帧
        return last_frame(video, out_png)
    return Path(out_png)


def lufs(path: str | Path) -> float | None:
    """Integrated loudness (EBU R128) of a file's audio."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"]
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    val = None
    for line in p.stderr.splitlines():
        line = line.strip()
        if line.startswith("I:") and "LUFS" in line:
            try:
                val = float(line.split()[1])
            except ValueError:
                pass
    return val
