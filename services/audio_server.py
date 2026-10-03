#!/usr/bin/env python
"""本地音频服务（只用 Python 标准库做 HTTP，避免和各模型的依赖冲突）。

同一个脚本按 --engines 加载不同模型，分别跑在各自的 Python 环境里：

  # 环境 A：IndexTTS 官方仓库的 uv 环境（对白配音，情绪控制）
  cd index-tts && uv run python ../aidrama/services/audio_server.py --engines indextts --port 9001 \
        --indextts-dir . --indextts-version 2.5

  # 环境 B：pip install qwen-tts qwen-asr（角色音色设计 + 识别 + 字幕强制对齐）
  python services/audio_server.py --engines voicedesign,asr --port 9002

接口（JSON，文件用本机路径传递）：
  GET  /health
  POST /tts     {text, spk_audio, out, lang?, emo_audio?, emo_alpha?, emo_vector?, emo_text?, duration_factor?}
  POST /design  {text, instruct, out, language?}
  POST /asr     {audio, language?}
  POST /align   {audio, text, language?}
  POST /unload  {}            释放显存（流水线在 ComfyUI 阶段之前自动调用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()  # 一块 GPU，串行推理
ENGINES: dict = {}
ARGS = None


def _wav_duration(path: str) -> float:
    try:
        import wave

        with wave.open(path) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        import soundfile as sf

        info = sf.info(path)
        return info.frames / float(info.samplerate)


class _Mock:
    """--mock：不加载任何模型，按字数生成正弦波，用于联调接口（云端 CI 也用它）。"""

    @staticmethod
    def tone(out: str, seconds: float, freq: float = 220.0, sr: int = 24000) -> None:
        import math
        import struct
        import wave

        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        n = int(seconds * sr)
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / sr))) for i in range(n)))

    def infer(self, spk_audio_prompt, text, output_path, **kw):
        self.tone(output_path, 0.25 * len(text) * float(kw.get("duration_factor", 1.0)) + 0.3)



def load_indextts():
    sys.path.insert(0, os.path.abspath(ARGS.indextts_dir))
    if ARGS.indextts_version == "2":
        from indextts.infer_v2 import IndexTTS2

        model_dir = os.path.join(ARGS.indextts_dir, ARGS.indextts_model_dir or "checkpoints_2")
        return IndexTTS2(cfg_path=os.path.join(model_dir, "config.yaml"), model_dir=model_dir,
                         use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
    from indextts.infer_v2_5 import IndexTTS2

    model_dir = os.path.join(ARGS.indextts_dir, ARGS.indextts_model_dir or "checkpoints")
    # use_cuda_kernel=False：不现场编译 BigVGAN 的 CUDA 扩展（编译中途被中断会留下锁文件，下次加载卡住）
    return IndexTTS2(cfg_path=os.path.join(model_dir, "config.yaml"), model_dir=model_dir,
                     use_bf16=True, use_cuda_kernel=False, use_qwen_emo=ARGS.qwen_emo)


def load_voicedesign():
    import torch
    from qwen_tts import Qwen3TTSModel

    return Qwen3TTSModel.from_pretrained(ARGS.voicedesign_model, device_map="cuda:0", dtype=torch.bfloat16)


def load_asr():
    import torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

    aligner = Qwen3ForcedAligner.from_pretrained(ARGS.aligner_model, dtype=torch.bfloat16, device_map="cuda:0")
    asr = Qwen3ASRModel.from_pretrained(ARGS.asr_model, dtype=torch.bfloat16, device_map="cuda:0")
    return {"asr": asr, "aligner": aligner}


LOADERS = {"indextts": load_indextts, "voicedesign": load_voicedesign, "asr": load_asr}


LOAD_LOCK = threading.RLock()   # 两个并发的首次请求只加载一次模型；/unload 不会和加载交错


def engine(name: str):
    if ARGS.mock:
        return ENGINES.setdefault(name, _Mock())
    with LOAD_LOCK:
        if name not in ENGINES:
            if name not in ARGS.engines:
                raise RuntimeError(f"engine '{name}' 未在本服务启用（--engines {','.join(ARGS.engines)}）")
            print(f"[audio_server] loading {name} ...", flush=True)
            ENGINES[name] = LOADERS[name]()
        return ENGINES[name]


def _weights_present(path: str) -> bool | None:
    """本地模型目录里是否真的有权重文件（None = 用的是 HuggingFace 仓库名，首次请求时才下载）。"""
    if not os.path.isdir(path):
        return None if "/" in path and not os.path.isabs(path) and not path.startswith(".") else False
    for _root, _dirs, files in os.walk(path):
        if any(f.endswith((".safetensors", ".pth", ".bin", ".pt")) for f in files):
            return True
    return False


def model_checks() -> dict:
    out = {}
    for e in ARGS.engines:
        if e == "indextts":
            d = os.path.join(ARGS.indextts_dir, ARGS.indextts_model_dir or ("checkpoints_2" if ARGS.indextts_version == "2" else "checkpoints"))
            out["indextts"] = bool(os.path.exists(os.path.join(d, "config.yaml")) and _weights_present(d))
        elif e == "voicedesign":
            out["voicedesign"] = _weights_present(ARGS.voicedesign_model)
        elif e == "asr":
            out["asr"] = _weights_present(ARGS.asr_model)
            out["aligner"] = _weights_present(ARGS.aligner_model)
    return out


LANG_IDX = {"zh": "ZH", "en": "EN", "ja": "JA", "es": "ES", "ar": "AR"}
LANG_QWEN = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "yue": "Cantonese"}


def do_tts(p: dict) -> dict:
    tts = engine("indextts")
    out = p["out"]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    kw = dict(spk_audio_prompt=p["spk_audio"], text=p["text"], output_path=out, verbose=False)
    if ARGS.indextts_version != "2" or ARGS.mock:
        kw["lang"] = LANG_IDX.get(p.get("lang", "zh"), "ZH")
        kw["duration_factor"] = float(p.get("duration_factor", 1.0))
    if p.get("emo_audio"):
        kw["emo_audio_prompt"] = p["emo_audio"]
        kw["emo_alpha"] = float(p.get("emo_alpha", 0.8))
    elif p.get("emo_vector"):
        kw["emo_vector"] = [float(x) for x in p["emo_vector"]]
        kw["emo_alpha"] = float(p.get("emo_alpha", 1.0))
    elif p.get("emo_text"):
        kw["use_emo_text"] = True
        kw["emo_text"] = p["emo_text"]
        kw["emo_alpha"] = float(p.get("emo_alpha", 0.6))
    with LOCK:
        tts.infer(**kw)
    return {"out": out, "duration": _wav_duration(out)}


def do_design(p: dict) -> dict:
    if ARGS.mock:
        _Mock.tone(p["out"], 0.22 * len(p["text"]) + 0.5, 180.0)
        return {"out": p["out"], "duration": _wav_duration(p["out"])}
    import soundfile as sf

    m = engine("voicedesign")
    with LOCK:
        wavs, sr = m.generate_voice_design(text=p["text"], instruct=p["instruct"],
                                           language=LANG_QWEN.get(p.get("language", "zh"), "Chinese"))
    os.makedirs(os.path.dirname(os.path.abspath(p["out"])), exist_ok=True)
    sf.write(p["out"], wavs[0], sr)
    return {"out": p["out"], "duration": _wav_duration(p["out"])}


def do_asr(p: dict) -> dict:
    if ARGS.mock:
        return {"text": p.get("expect", ""), "language": "Chinese"}
    m = engine("asr")["asr"]
    lang = LANG_QWEN.get(p.get("language", "zh"), None)
    with LOCK:
        r = m.transcribe(audio=p["audio"], language=lang)[0]
    return {"text": r.text, "language": r.language}


def do_align(p: dict) -> dict:
    if ARGS.mock:
        dur = _wav_duration(p["audio"])
        chars = [c for c in p["text"] if c.strip()]
        step = dur / max(1, len(chars))
        return {"items": [{"text": c, "start": i * step, "end": (i + 1) * step} for i, c in enumerate(chars)]}
    al = engine("asr")["aligner"]
    with LOCK:
        r = al.align(audio=p["audio"], text=p["text"], language=LANG_QWEN.get(p.get("language", "zh"), "Chinese"))[0]
    return {"items": [{"text": it.text, "start": float(it.start_time), "end": float(it.end_time)} for it in r]}


def do_unload(p: dict) -> dict:
    """释放显存：流水线切到 ComfyUI 生成画面前调用；下次请求时自动重新加载（约 10~30 秒）。"""
    with LOCK, LOAD_LOCK:
        names = list(ENGINES)
        ENGINES.clear()
        import gc

        gc.collect()
        if "torch" in sys.modules:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    return {"unloaded": names}


ROUTES = {"/tts": do_tts, "/design": do_design, "/asr": do_asr, "/align": do_align, "/unload": do_unload}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "engines": ARGS.engines, "loaded": list(ENGINES),
                             "models": {} if ARGS.mock else model_checks()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, fn(payload))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *a):
        sys.stderr.write("[audio_server] " + (fmt % a) + "\n")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="indextts", help="逗号分隔：indextts,voicedesign,asr")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--indextts-dir", default=".")
    ap.add_argument("--indextts-version", default="2.5", choices=["2.5", "2"])
    ap.add_argument("--indextts-model-dir", default=None)
    ap.add_argument("--qwen-emo", action="store_true", help="IndexTTS-2.5 启用情绪文本（emo_text）需要额外加载 Qwen 情绪模型")
    ap.add_argument("--voicedesign-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    ap.add_argument("--asr-model", default="Qwen/Qwen3-ASR-1.7B")
    ap.add_argument("--aligner-model", default="Qwen/Qwen3-ForcedAligner-0.6B")
    ap.add_argument("--preload", action="store_true")
    ap.add_argument("--mock", action="store_true", help="不加载模型，生成测试音频（联调用）")
    ARGS = ap.parse_args()
    ARGS.engines = [e.strip() for e in ARGS.engines.split(",") if e.strip()]
    if ARGS.preload:
        for e in ARGS.engines:
            engine(e)
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"[audio_server] engines={ARGS.engines} listening on http://{ARGS.host}:{ARGS.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
