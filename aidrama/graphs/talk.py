"""InfiniteTalk（Wan2.1-I2V-14B + InfiniteTalk 补丁，ComfyUI 原生节点）：音频驱动口型 —— 兜底方案。

什么时候用：H3 对白段反复抽卡口型/台词仍不过关、单人独白超过 15 秒、需要逐字锁定已有配音。
画质：480p 级（之后由 SeedVR2 超分），表演自然度不如 H3，但口型稳定、时长不限。
参数复刻官方模板 video_wan2_1_infinitetalk：lightx2v 蒸馏 LoRA，euler / normal 6 步 / cfg 1，25fps，
每块 81 帧、motion_frame_count 9，后续块接 previous_frames 并按 trim_image 去掉重叠帧。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .. import models as M
from ..graph import Graph


@dataclass
class InfiniteTalkJob:
    image: str                   # 起始画面（关键帧，ComfyUI input 名）
    audio: str                   # 对白音频（ComfyUI input 名）
    prompt: str = "A person is talking naturally to someone off-screen."
    audio_seconds: float = 3.0
    width: int = 480
    height: int = 832                # Wan 2.1 480p 标准竖屏 480x832
    steps: int = 6
    audio_scale: float = 1.0
    seed: int = 0
    prefix: str = "aidrama/talk"


def build_infinitetalk(job: InfiniteTalkJob) -> dict:
    fps = 25
    total = int(math.ceil(job.audio_seconds * fps)) + 1
    chunks = 1 + max(0, math.ceil((total - 81) / 72))
    g = Graph()
    base = g.add("UNETLoader", unet_name=M.WAN21_I2V_480, weight_dtype="default").out
    base = g.add("LoraLoaderModelOnly", model=base, lora_name=M.WAN_LIGHTX2V_480, strength_model=1.0).out
    patch = g.add("ModelPatchLoader", name=M.INFINITETALK_PATCH).out
    clip = g.add("CLIPLoader", clip_name=M.WAN_UMT5, type="wan", device="default").out
    vae = g.add("VAELoader", vae_name=M.WAN_VAE).out
    pos = g.add("CLIPTextEncode", text=job.prompt, clip=clip).out
    neg = g.add("ConditioningZeroOut", conditioning=pos).out
    enc = g.add("AudioEncoderLoader", audio_encoder_name=M.WAV2VEC_ZH).out
    audio = g.add("LoadAudio", audio=job.audio).out
    aenc = g.add("AudioEncoderEncode", audio_encoder=enc, audio=audio).out
    start = g.add("LoadImage", image=job.image)[0]
    start = g.add("ResizeImageMaskNode", input=start, resize_type="scale dimensions", scale_method="area",
                  **{"resize_type.width": job.width, "resize_type.height": job.height, "resize_type.crop": "center"}).out
    pieces = []
    prev = None
    for i in range(chunks):
        it = g.add("WanInfiniteTalkToVideo", mode="single_speaker", model=base, model_patch=patch, positive=pos, negative=neg,
                   vae=vae, width=job.width, height=job.height, length=81, audio_encoder_output_1=aenc,
                   motion_frame_count=9, audio_scale=job.audio_scale, start_image=start, previous_frames=prev)
        guider = g.add("CFGGuider", model=it[0], positive=it[1], negative=it[2], cfg=1.0).out
        sigmas = g.add("BasicScheduler", model=it[0], scheduler="normal", steps=job.steps, denoise=1.0).out
        sampler = g.add("KSamplerSelect", sampler_name="euler").out
        noise = g.add("RandomNoise", noise_seed=job.seed + i).out
        lat = g.add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=it[3])[0]
        frames = g.add("VAEDecode", samples=lat, vae=vae).out
        if i > 0:
            frames = g.add("ImageFromBatch", image=frames, batch_index=it[4], length=4096).out
        pieces.append(frames)
        prev = frames if i == 0 else g.add("BatchImagesNode", **{f"images.image{k}": p for k, p in enumerate(pieces)}).out
    allf = pieces[0] if len(pieces) == 1 else g.add("BatchImagesNode", **{f"images.image{k}": p for k, p in enumerate(pieces)}).out
    video = g.add("CreateVideo", images=allf, audio=audio, fps=float(fps)).out
    g.add("SaveVideo", video=video, filename_prefix=job.prefix, format="mp4",
          **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 12.0})
    return g.to_api()
