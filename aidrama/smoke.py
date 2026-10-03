"""真机冒烟测试：装好之后先跑它（约 10~15 分钟），每个模型用最小参数真正推理一次。

    python -m aidrama smoke                 # 全部
    python -m aidrama smoke --only image,h3 # 只测部分：image / h3 / ref2va / upscale / music / audio

它回答的是“装没装对、模型能不能加载、显存够不够”，不评价画质；画质请跑示例工程。
产物在 <仓库>/projects/_smoke/，最后打印每一项的耗时和结果。
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import ffmpeg_utils as ff
from .audio import AudioClient, trim_silence
from .comfy_client import ComfyClient
from .config import ROOT, load_config
from .graphs.h3 import Guide, H3Job, build_h3
from .graphs.post import MusicJob, UpscaleJob, build_music, build_seedvr2
from .graphs.qwen_edit import QwenEditJob, QwenT2IJob, build_qwen2511_edit, build_qwen2512_t2i
from .mock import placeholder_image

STEPS = ("image", "h3", "ref2va", "upscale", "music", "audio")
LINE = "这封信，写的是我的名字。"


def smoke(only: list[str] | None = None, out_dir: str | Path | None = None) -> bool:
    cfg = load_config(None)
    out = Path(out_dir or ROOT / "projects" / "_smoke")
    out.mkdir(parents=True, exist_ok=True)
    want = [s for s in STEPS if not only or s in only]
    comfy = ComfyClient(cfg["comfy"]["url"])
    audio = AudioClient(cfg["audio"])
    results: list[tuple[str, bool, float, str]] = []
    state: dict[str, Path] = {}

    def step(name: str, fn) -> None:
        if name not in want:
            return
        print(f"\n[smoke] {name} …", flush=True)
        t0 = time.time()
        try:
            note = fn() or ""
            results.append((name, True, time.time() - t0, note))
            print(f"[smoke] {name} ✓ {time.time() - t0:.0f}s {note}", flush=True)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            results.append((name, False, time.time() - t0, f"{type(e).__name__}: {str(e)[:300]}"))

    def free():
        try:
            comfy.free()
        except Exception:  # noqa: BLE001
            pass

    # 1. 图像：Qwen-Image-2512 文生图 → Qwen-Image-Edit-2511 以它为参考出一张新图
    def image():
        api = build_qwen2512_t2i(QwenT2IJob(
            prompt="Vertical portrait photograph, head and shoulders, a young East Asian woman with a low black ponytail, "
                   "ivory silk blouse, plain grey studio backdrop, soft light, photorealistic.",
            width=576, height=1008, steps=12, seed=1, prefix="aidrama/smoke_t2i"))
        p1 = comfy.run(api, out, "t2i")[0]
        ref = comfy.upload(p1)
        api = build_qwen2511_edit(QwenEditJob(
            prompt="The woman from Picture 1 sits at a wooden desk in a dim archive room, holding a yellowed envelope, "
                   "warm lamp light. Keep her face, hairstyle and clothing exactly. Vertical film still.",
            refs=[ref], width=576, height=1008, steps=12, seed=2, prefix="aidrama/smoke_edit"))
        p2 = comfy.run(api, out, "keyframe")[0]
        state["keyframe"] = p2
        return f"→ {p2.name}"

    # 2. H3 FL2VA 图生视频（4 秒，480x864，8 步，不依赖加速 LoRA）
    def h3():
        kf = state.get("keyframe") or placeholder_image(out / "kf_placeholder.png", 512, 896, "smoke")
        free()
        job = H3Job(prompt=("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
                            "integrated_multimodal_description: [Shot 1] Live-action, cinematic, starting exactly from <Picture 1>. "
                            "The woman slowly lifts the envelope and reads it; the camera pushes in slightly. No subtitles.\n\n"
                            "overall_soundscape: Quiet room tone and the rustle of paper.\n\nnon_diegetic_music: N/A"),
                    seconds=5, width=512, height=896, mode="fl2va", first_frame=comfy.upload(kf), steps=8,
                    scheduler="simple", attention=cfg["video"].get("attention") or None, seed=3, prefix="aidrama/smoke_h3")
        p = comfy.run(build_h3(job), out, "h3_fl2va")[0]
        state["video"] = p
        return f"→ {p.name}，{ff.duration(p):.2f}s，{'有' if ff.has_audio(p) else '无'}音轨"

    # 3. H3 Ref2VA + 对白音轨驱动（需要音频服务；不可用时用静音轨只测模型加载）
    def ref2va():
        kf = state.get("keyframe") or placeholder_image(out / "kf_placeholder.png", 512, 896, "smoke")
        wav = state.get("line")
        if wav is None:
            wav = out / "silence.wav"
            ff.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5", str(wav)])
        free()
        img, aud = comfy.upload(kf), comfy.upload(wav)
        prompt = ("subject_definitions:\n<Subject 1> (S1) is the person in <Picture 1>.\n<Picture 1> is the first frame of [Shot 1].\n"
                  "<Audio 1> is the pre-recorded dialogue track of the target video, containing the spoken lines of <Subject 1> (S1).\n\n"
                  "summary:\nThe target video is a 1-shot scene. It begins exactly from <Picture 1>; <Audio 1> supplies the dialogue.\n\n"
                  "retention_analysis:\n<Subject 1>: fully_preserved.\n<Audio 1>: partially_copy - every spoken word is reused in sync.\n\n"
                  f"detailed_description:\n[Shot 1] The shot begins from <Picture 1>. <Subject 1> (S1) says quietly: <d>[Chinese] {LINE}</d> "
                  "Every syllable follows <Audio 1> exactly.\n\noverall_soundscape:\nQuiet room tone.\n\nnon_diegetic_music:\nN/A")
        job = H3Job(prompt=prompt, seconds=5, width=512, height=896, mode="ref2va", ref_images=[img], ref_audios=[aud],
                    guides=[Guide(0, image=img)], anchor_first_frame=False, steps=8, scheduler="simple",
                    attention=cfg["video"].get("attention") or None, seed=4, prefix="aidrama/smoke_ref2va")
        p = comfy.run(build_h3(job), out, "h3_ref2va")[0]
        state.setdefault("video", p)
        return f"→ {p.name}"

    # 4. SeedVR2 超分（3B，×1.5，只测 2 秒以内的片段）
    def upscale():
        src = state.get("video")
        if src is None:
            raise RuntimeError("没有可超分的视频（先跑 h3）")
        short = out / "short.mp4"
        ff.run(["-i", str(src), "-t", "1.5", "-c:v", "libx264", "-crf", "12", "-c:a", "aac", str(short)])
        free()
        api = build_seedvr2(UpscaleJob(video=comfy.upload(short), scale=1.5, model="3b", prefix="aidrama/smoke_up"))
        p = comfy.run(api, out, "upscaled")[0]
        w, h = ff.video_size(p)
        return f"→ {w}x{h}"

    # 5. 配乐（MiniMax Music 3，10 秒）
    def music():
        free()
        api = build_music(MusicJob(caption="Minimal suspense underscore, soft piano and low strings, slow tempo, instrumental.",
                                   seconds=10, seed=5, prefix="aidrama/smoke_bgm"))
        p = comfy.run(api, out, "bgm")[0]
        return f"→ {p.name}，{ff.duration(p):.1f}s"

    # 6. 音频：音色设计 → 情绪配音 → 识别回读
    def audio_():
        free()
        voice = audio.design_voice("这批信在库房里放了整整三年，没有人来认领。", "二十五岁女性，声音清亮偏冷，语速偏慢", out / "voice_raw.wav")
        ref = trim_silence(voice, out / "voice.wav")
        wav, dur = audio.tts(LINE, ref, out / "line_raw.wav", emotion="surprise")
        line = trim_silence(wav, out / "line.wav")
        state["line"] = line
        hyp = audio.asr(line)
        audio.unload()
        return f"配音 {dur:.2f}s，识别：{hyp}"

    # 音频放在 Ref2VA 之前，这样 Ref2VA 能用真实台词驱动
    order = ["image", "audio", "h3", "ref2va", "upscale", "music"]
    fns = {"image": image, "h3": h3, "ref2va": ref2va, "upscale": upscale, "music": music, "audio": audio_}
    if not comfy.alive():
        print(f"ComfyUI {cfg['comfy']['url']} 未启动，请先运行 start_all")
        return False
    for name in order:
        step(name, fns[name])
    free()

    print("\n================ 冒烟测试结果 ================")
    for name, ok, sec, note in results:
        print(f"  {'✓' if ok else '✗'} {name:8s} {sec:6.0f}s  {note}")
    print(f"产物目录：{out}")
    allok = all(ok for _, ok, _, _ in results)
    print("结论：" + ("全部通过，可以跑示例工程了 ✅" if allok else "有失败项，按上面的报错和 docs/06-常见问题.md 排查 ❌"))
    return allok
