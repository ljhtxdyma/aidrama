"""成片合成：镜头规范化 → 转场拼接 → BGM 闪避混音 → 字幕/AI 标识 → 响度标准化 → 交付编码。

全部用 ffmpeg 完成，输出：
  episode.mp4         平台交付版（1080x1920, H.264 High, AAC 48k, -14 LUFS）
  episode_clean.mp4   无字幕版（给剪映/达芬奇精修，或平台外挂字幕）
  episode.srt / .ass  字幕
  timeline.json       每个镜头在成片中的起止时间（方便回到工程定位返工镜头）
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_utils as ff
from .subtitles import Cue, split_cue, write_ass, write_srt

XFADE = {"fade": "fade", "dissolve": "dissolve", "flash": "fadewhite"}


@dataclass
class Clip:
    path: str
    duration: float | None = None          # 目标时长；None = 用素材原长
    transition: str = "cut"                # 进入下一个镜头的转场: cut/fade/dissolve/flash
    transition_dur: float = 0.4
    cues: list[Cue] = field(default_factory=list)  # 相对本镜头开头的字幕
    shot_id: str = ""


@dataclass
class AssembleOptions:
    width: int = 1080
    height: int = 1920
    fps: int = 24
    bgm: str | None = None
    bgm_db: float = -16.0                  # BGM 基础电平（相对），对白时再自动压低
    duck: bool = True
    target_lufs: float = -14.0             # 抖音/快手/红果常用 -14 LUFS，True Peak -1 dBTP
    burn_subtitles: bool = True
    subtitle_font: str = "Source Han Sans SC"
    subtitle_size: int = 64
    subtitle_margin_v: int = 520
    fonts_dir: str | None = None
    ai_label: str | None = "本内容由AI生成"   # 显式标识（《人工智能生成合成内容标识办法》/ GB 45438-2025）
    ai_label_size: int = 64                # GB 45438 5.4 f)：文字高度 ≥ 画面最短边 5%（1080 宽 → ≥54px）；中文字形高约 0.9em
    aigc_producer: str = "aidrama-local"   # 隐式标识 ContentProducer（附录 E：只能用 ASCII 可见字符、不含空格）
    aigc_produce_id: str | None = None     # 隐式标识 ProduceID（不填则自动生成）
    crf: int = 16
    preset: str = "slow"
    audio_bitrate: str = "320k"
    keep_temp: bool = False


def _norm_clip(src: str, dst: Path, dur: float | None, o: AssembleOptions) -> float:
    src_dur = ff.duration(src)
    # 对齐到整帧：否则硬切拼接时每段多出不到一帧，字幕会越往后越偏
    target = max(1, round((dur or src_dur) * o.fps)) / o.fps
    vf = (
        f"scale={o.width}:{o.height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={o.width}:{o.height},setsar=1,fps={o.fps},format=yuv420p"
    )
    if target > src_dur + 0.02:  # 素材短于目标：冻结最后一帧补齐
        vf += f",tpad=stop_mode=clone:stop_duration={target - src_dur:.3f}"
    args = ["-i", src]
    if ff.has_audio(src):
        af = f"aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={target:.3f}"
        args += ["-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a"]
    args += ["-t", f"{target:.3f}", "-c:v", "libx264", "-crf", "10", "-preset", "fast",
             "-c:a", "pcm_s16le", "-ar", "48000", str(dst)]
    ff.run(args)
    return target


def _timeline(norm: list[Path], durs: list[float], clips: list[Clip], out: Path) -> list[float]:
    """拼接并返回每个镜头在成片中的起始时间。"""
    starts = [0.0]
    for i in range(1, len(norm)):
        prev = clips[i - 1]
        d = prev.transition_dur if prev.transition in XFADE else 0.0
        starts.append(starts[-1] + durs[i - 1] - d)
    if len(norm) == 1:
        shutil.copy(norm[0], out)
        return starts

    if all(c.transition not in XFADE for c in clips[:-1]):
        # 全是硬切：concat demuxer，最快且无损
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in norm), encoding="utf-8")
        ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        return starts

    inputs: list[str] = []
    for p in norm:
        inputs += ["-i", str(p)]
    # xfade 要求两路输入时间基一致，所以每一路都先统一到 AVTB
    parts = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[iv{i}];[{i}:a]asettb=AVTB,asetpts=PTS-STARTPTS[ia{i}]"
             for i in range(len(norm))]
    vprev, aprev = "[iv0]", "[ia0]"
    acc = durs[0]
    for i in range(1, len(norm)):
        prev = clips[i - 1]
        vout, aout = f"[v{i}]", f"[a{i}]"
        if prev.transition in XFADE:
            d = prev.transition_dur
            off = acc - d
            parts.append(f"{vprev}[iv{i}]xfade=transition={XFADE[prev.transition]}:duration={d}:offset={off:.3f},settb=AVTB{vout}")
            parts.append(f"{aprev}[ia{i}]acrossfade=d={d}:c1=tri:c2=tri,asettb=AVTB{aout}")
            acc = acc - d + durs[i]
        else:
            parts.append(f"{vprev}{aprev}[iv{i}][ia{i}]concat=n=2:v=1:a=1[cv{i}][ca{i}];"
                         f"[cv{i}]settb=AVTB{vout};[ca{i}]asettb=AVTB{aout}")
            acc += durs[i]
        vprev, aprev = vout, aout
    ff.run(inputs + ["-filter_complex", ";".join(parts), "-map", vprev, "-map", aprev,
                     "-c:v", "libx264", "-crf", "10", "-preset", "fast", "-c:a", "pcm_s16le", str(out)])
    return starts


def aigc_metadata(o: AssembleOptions, name: str) -> str:
    """GB 45438-2025 附录 E 文件元数据隐式标识：{"AIGC": {...}}，全文件只保留这一份。
    首次写入时传播方 = 制作方、传播编号 = 制作编号；ReservedCode1 存元数据摘要用于完整性校验（附录 F.4 的做法）。"""
    produce_id = o.aigc_produce_id or f"{_ascii(name)}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    producer = _ascii(o.aigc_producer) or "aidrama-local"
    fields = {"Label": "1", "ContentProducer": producer, "ProduceID": produce_id}
    fields["ReservedCode1"] = hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    fields.update({"ContentPropagator": producer, "PropagateID": produce_id, "ReservedCode2": ""})
    return json.dumps({"AIGC": fields}, separators=(",", ":"))


def _ascii(s: str) -> str:
    """附录 E j)：取值只用 0x21、0x23~0x5B、0x5D~0x7E（可见 ASCII，去掉空格、双引号和反斜杠）。"""
    return "".join(c if (c == "!" or 0x23 <= ord(c) <= 0x5B or 0x5D <= ord(c) <= 0x7E) else "_" for c in s).strip("_")


def _escape_filter_path(p: Path) -> str:
    s = p.resolve().as_posix()
    return s.replace(":", r"\:").replace("'", r"\'")


def assemble(clips: list[Clip], out_dir: str | Path, name: str = "episode", opts: AssembleOptions | None = None) -> dict:
    o = opts or AssembleOptions()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="aidrama_asm_", dir=out_dir))
    try:
        norm, durs = [], []
        for i, c in enumerate(clips):
            dst = tmp / f"n{i:03d}.mkv"
            durs.append(_norm_clip(c.path, dst, c.duration, o))
            norm.append(dst)
        tl = tmp / "timeline.mkv"
        starts = _timeline(norm, durs, clips, tl)
        total = ff.duration(tl)

        # ---------------- subtitles
        cues: list[Cue] = []
        for c, st in zip(clips, starts):
            for q in c.cues:
                for s in split_cue(q):
                    cues.append(Cue(st + s.start, min(st + s.end, total), s.text, s.speaker))
        srt = write_srt(cues, out_dir / f"{name}.srt")
        ass = write_ass(cues, out_dir / f"{name}.ass", o.width, o.height, o.subtitle_font, o.subtitle_size, o.subtitle_margin_v)

        # ---------------- audio: dialogue/ambience bus + BGM with sidechain ducking
        inputs = ["-i", str(tl)]
        if o.bgm:
            inputs += ["-stream_loop", "-1", "-i", o.bgm]
            fade_out = max(0.0, total - 2.0)
            bgm = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume={o.bgm_db}dB,afade=t=in:d=1.5,afade=t=out:st={fade_out:.3f}:d=2[bgm]"
            if o.duck:
                afilter = (f"{bgm};[0:a]asplit=2[dx][key];"
                           f"[bgm][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[bgmd];"
                           f"[dx][bgmd]amix=inputs=2:duration=first:normalize=0[mix]")
            else:
                afilter = f"{bgm};[0:a][bgm]amix=inputs=2:duration=first:normalize=0[mix]"
        else:
            afilter = "[0:a]anull[mix]"

        # 两遍 loudnorm：先测量
        meas_cmd = inputs + ["-filter_complex", afilter + f";[mix]loudnorm=I={o.target_lufs}:TP=-1.0:LRA=11:print_format=json[out]",
                             "-map", "[out]", "-f", "null", "-"]
        import subprocess
        p = subprocess.run([ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"] + meas_cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
        meas = json.loads(m.group(0)) if m else None
        if meas and not all(math.isfinite(float(meas[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh")):
            meas = "silent"     # 全片无声（例如没有配乐的动作迁移段）：不做响度归一，否则 -inf 会让 ffmpeg 报错
        if meas == "silent":
            ln = "anull"
        elif meas:
            ln = (f"loudnorm=I={o.target_lufs}:TP=-1.0:LRA=11:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
                  f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true")
        else:
            ln = f"loudnorm=I={o.target_lufs}:TP=-1.0:LRA=11"
        afinal = afilter + f";[mix]{ln},aresample=48000[aout]"

        # ---------------- video: AI label (+ subtitles)
        vchain = "[0:v]"
        vf = []
        if o.ai_label:
            fontfile = _find_font(o.fonts_dir)
            ff_font = f":fontfile='{_escape_filter_path(fontfile)}'" if fontfile else ""
            label_size = max(o.ai_label_size, math.ceil(min(o.width, o.height) * 0.05 / 0.85))
            vf.append(f"drawtext=text='{o.ai_label}'{ff_font}:fontsize={label_size}:fontcolor=white@0.85:"
                      f"borderw=2:bordercolor=black@0.6:x=w-tw-40:y=140")
        clean_vf = ",".join(vf) if vf else "null"
        enc = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", str(o.crf), "-preset", o.preset,
               "-r", str(o.fps), "-c:a", "aac", "-b:a", o.audio_bitrate, "-ar", "48000",
               "-movflags", "+faststart+use_metadata_tags", "-metadata", f"AIGC={aigc_metadata(o, name)}"]

        clean = out_dir / f"{name}_clean.mp4"
        ff.run(inputs + ["-filter_complex", f"{vchain}{clean_vf}[vout];{afinal}", "-map", "[vout]", "-map", "[aout]"] + enc + [str(clean)])

        final = out_dir / f"{name}.mp4"
        if o.burn_subtitles and cues:
            sub = f"subtitles='{_escape_filter_path(ass)}'"
            if o.fonts_dir:
                sub += f":fontsdir='{_escape_filter_path(Path(o.fonts_dir))}'"
            vf_full = ",".join([sub] + vf)
            ff.run(inputs + ["-filter_complex", f"{vchain}{vf_full}[vout];{afinal}", "-map", "[vout]", "-map", "[aout]"] + enc + [str(final)])
        else:
            shutil.copy(clean, final)

        timeline = [
            {"shot": c.shot_id, "src": c.path, "start": round(st, 3), "end": round(st + d, 3), "transition": c.transition}
            for c, st, d in zip(clips, starts, durs)
        ]
        (out_dir / f"{name}_timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"final": str(final), "clean": str(clean), "srt": str(srt), "ass": str(ass),
                "duration": ff.duration(final), "lufs": ff.lufs(final), "timeline": timeline}
    finally:
        if not o.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)


def _find_font(fonts_dir: str | None) -> Path | None:
    cands = []
    if fonts_dir:
        cands += sorted(Path(fonts_dir).glob("*.[ot]t[fc]"))
    cands += [Path(p) for p in (
        "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/System/Library/Fonts/PingFang.ttc",
    )]
    for c in cands:
        if c.exists():
            return c
    return None
