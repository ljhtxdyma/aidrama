"""配音：调用本地音频服务（services/audio_server.py），以及把多句台词排成与镜头/段等长的对白音轨。"""
from __future__ import annotations

from pathlib import Path

import requests

from . import net
from . import ffmpeg_utils as ff
from .mock import tone_wav

# 剧本情绪标签 → IndexTTS 8 维情绪向量 [喜, 怒, 哀, 惧, 厌恶, 低落, 惊讶, 平静]（经验起点，可在 Line.emo_vector 覆盖）
EMO_VECTORS: dict[str, list[float]] = {
    "neutral": [0, 0, 0, 0, 0, 0, 0, 0.6],
    "happy": [0.7, 0, 0, 0, 0, 0, 0.1, 0],
    "angry": [0, 0.8, 0, 0, 0.1, 0, 0, 0],
    "sad": [0, 0, 0.6, 0, 0, 0.3, 0, 0],
    "cry": [0, 0, 0.7, 0, 0, 0.3, 0, 0],
    "fear": [0, 0, 0.15, 0.75, 0, 0, 0.05, 0],
    "surprise": [0.05, 0, 0, 0.15, 0, 0, 0.7, 0],
    "disgust": [0, 0.15, 0, 0, 0.7, 0, 0, 0],
    "contempt": [0.1, 0.15, 0, 0, 0.55, 0, 0, 0.15],
    "whisper": [0, 0, 0, 0.1, 0, 0.2, 0, 0.5],
}


class AudioError(RuntimeError):
    pass


class AudioClient:
    def __init__(self, cfg: dict, mock: bool = False):
        self.cfg = cfg
        self.mock = mock

    def _post(self, base: str, path: str, payload: dict, timeout: float = 900) -> dict:
        try:
            r = net.post(base.rstrip("/") + path, json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise AudioError(f"音频服务 {base} 无法连接（是否已启动 services/audio_server.py？）: {e}") from e
        if r.status_code != 200:
            raise AudioError(f"{base}{path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def health(self) -> dict:
        out = {}
        for k in ("tts_url", "design_url", "asr_url"):
            try:
                out[k] = net.get(self.cfg[k].rstrip("/") + "/health", timeout=5).json()
            except Exception as e:  # noqa: BLE001
                out[k] = {"ok": False, "error": str(e)}
        return out

    def unload(self) -> None:
        """让音频服务释放显存（服务没启动也不报错）。"""
        if self.mock:
            return
        for base in dict.fromkeys(self.cfg[k] for k in ("tts_url", "design_url", "asr_url") if self.cfg.get(k)):
            try:
                net.post(base.rstrip("/") + "/unload", json={}, timeout=30)
            except Exception:  # noqa: BLE001
                pass

    def design_voice(self, text: str, instruct: str, out: str | Path, language: str = "zh") -> Path:
        if self.mock:
            return tone_wav(out, 0.2 * len(text) + 0.5, 180.0)
        self._post(self.cfg["design_url"], "/design", {"text": text, "instruct": instruct, "out": str(Path(out).resolve()), "language": language})
        return Path(out)

    def tts(self, text: str, spk_audio: str | Path, out: str | Path, emotion: str = "neutral",
            emo_vector: list[float] | None = None, emo_audio: str | None = None, lang: str = "zh") -> tuple[Path, float]:
        if self.mock:
            p = tone_wav(out, 0.22 * len(text) + 0.3, 200.0 + 20 * (hash(text) % 5))
            return p, ff.duration(p)
        payload = {"text": text, "spk_audio": str(Path(spk_audio).resolve()), "out": str(Path(out).resolve()), "lang": lang}
        if emo_audio:
            payload["emo_audio"] = str(Path(emo_audio).resolve())
            payload["emo_alpha"] = 0.8
        else:
            payload["emo_vector"] = emo_vector or EMO_VECTORS.get(emotion, EMO_VECTORS["neutral"])
            payload["emo_alpha"] = 1.0
        r = self._post(self.cfg["tts_url"], "/tts", payload)
        return Path(r["out"]), float(r["duration"])

    def asr(self, audio: str | Path, language: str = "zh", expect: str = "") -> str:
        if self.mock:
            return expect
        # expect 只给 --mock 服务用（真实识别不看它，避免“提示答案”掩盖错读）
        return self._post(self.cfg["asr_url"], "/asr", {"audio": str(Path(audio).resolve()), "language": language,
                                                        "expect": expect})["text"]

    def align(self, audio: str | Path, text: str, language: str = "zh") -> list[dict]:
        if self.mock:
            d = ff.duration(audio)
            chars = [c for c in text if c.strip()]
            step = d / max(1, len(chars))
            return [{"text": c, "start": i * step, "end": (i + 1) * step} for i, c in enumerate(chars)]
        return self._post(self.cfg["asr_url"], "/align", {"audio": str(Path(audio).resolve()), "text": text, "language": language})["items"]


def trim_silence(src: str | Path, dst: str | Path) -> Path:
    """去掉 TTS 首尾静音，统一 48k 单声道，单句响度归一到 -18 LUFS（之后整体混音再统一到 -14）。"""
    dst = Path(dst)
    tmp = dst.with_name(dst.stem + "_trim.wav")
    # 分两遍：ffmpeg 里 areverse 后面直接接 loudnorm 会卡在 EOF 不退出
    ff.run(["-i", str(src), "-af",
            "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.08,areverse",
            "-ac", "1", "-c:a", "pcm_s16le", str(tmp)])
    try:
        ff.run(["-i", str(tmp), "-af", "loudnorm=I=-18:TP=-2:LRA=11,aresample=48000",
                "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def build_track(items: list[tuple[str | Path, float]], total: float, out: str | Path) -> Path:
    """把 (音频, 起始秒) 排到一条 total 秒长的 48k 立体声音轨上。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        ff.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(out)])
        return out
    args, parts = [], []
    for i, (p, start) in enumerate(items):
        args += ["-i", str(p)]
        ms = int(round(start * 1000))
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(items)))
    parts.append(f"{mix}amix=inputs={len(items)}:duration=longest:normalize=0,apad=whole_dur={total:.3f},atrim=0:{total:.3f}[out]")
    ff.run(args + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", str(out)])
    return out
