"""后期类工作流：SeedVR2 视频超分、FILM 补帧、MiniMax Music 3 配乐。

全部复刻 ComfyUI 官方模板（utility_seedvr2_*_upscale_video / utility_video_frame_interpolation /
audio_minimax_music_3）的节点连接，只把子图里的开关换成固定参数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .. import models as M
from ..graph import Graph


@dataclass
class UpscaleJob:
    video: str                       # ComfyUI input 中的视频名
    scale: float = 1.40625           # 768x1344 → 1080x1890（再由 ffmpeg 裁/缩到 1080x1920）
    model: Literal["7b", "3b"] = "7b"
    color_correction: Literal["lab", "wavelet", "adain", "none"] = "lab"
    chunked: bool = True             # 长片段按时间分块，控制显存
    seed: int = 42
    prefix: str = "aidrama/up"


def build_seedvr2(job: UpscaleJob) -> dict:
    g = Graph()
    video = g.add("LoadVideo", file=job.video).out
    comps = g.add("GetVideoComponents", video=video)
    resized = g.add(
        "ResizeImageMaskNode", input=comps[0], resize_type="scale by multiplier",
        scale_method="lanczos", **{"resize_type.multiplier": job.scale},
    ).out
    pre = g.add("SeedVR2Preprocess", resized_images=resized).out
    vae = g.add("VAELoader", vae_name=M.SEEDVR2_VAE).out
    unet = g.add("UNETLoader", unet_name=M.SEEDVR2_7B if job.model == "7b" else M.SEEDVR2_3B, weight_dtype="default").out
    lat = g.add("VAEEncodeTiled", pixels=pre, vae=vae, tile_size=512, overlap=128, temporal_size=64, temporal_overlap=8).out
    if job.chunked:
        # 显存不够被自动切块时，相邻块重叠 2 个潜帧做交叉淡化，避免接缝（官方模板默认不分块）
        ch = g.add("SeedVR2TemporalChunk", latent=lat, temporal_overlap=2, chunking_mode="auto")
        cond_lat, overlap = ch[0], ch[1]
    else:
        cond_lat, overlap = lat, None
    cond = g.add("SeedVR2Conditioning", model=unet, vae_conditioning=cond_lat)
    samples = g.add(
        "KSampler", model=unet, seed=job.seed, steps=1, cfg=1.0, sampler_name="euler", scheduler="simple",
        positive=cond[0], negative=cond[1], latent_image=cond_lat, denoise=1.0,
    ).out
    if job.chunked:
        samples = g.add("SeedVR2TemporalMerge", latents=samples, temporal_overlap=overlap).out
    dec = g.add("VAEDecodeTiled", samples=samples, vae=vae, tile_size=512, overlap=128, temporal_size=64, temporal_overlap=8).out
    post = g.add("SeedVR2PostProcessing", images=dec, original_resized_images=resized, color_correction_method=job.color_correction).out
    out = g.add("CreateVideo", images=post, audio=comps[1], fps=comps[2]).out
    g.add("SaveVideo", video=out, filename_prefix=job.prefix, format="mp4",
          **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 12.0})
    return g.to_api()


@dataclass
class InterpJob:
    video: str
    multiplier: int = 2              # 24fps → 48fps；需要 30/60fps 时在 ffmpeg 阶段重采样
    prefix: str = "aidrama/interp"


def build_interp(job: InterpJob) -> dict:
    g = Graph()
    video = g.add("LoadVideo", file=job.video).out
    comps = g.add("GetVideoComponents", video=video)
    model = g.add("FrameInterpolationModelLoader", model_name=M.FILM).out
    frames = g.add("FrameInterpolate", interp_model=model, images=comps[0], multiplier=job.multiplier).out
    fps = g.add("ComfyMathExpression", expression="a * b", **{"values.a": comps[2], "values.b": float(job.multiplier)})
    out = g.add("CreateVideo", images=frames, audio=comps[1], fps=fps[0]).out
    g.add("SaveVideo", video=out, filename_prefix=job.prefix, format="mp4",
          **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 12.0})
    return g.to_api()


@dataclass
class MusicJob:
    caption: str                     # 风格/配器/BPM/情绪/结构描述（英文效果最好）
    lyrics: str = "[Instrumental]"
    seconds: float = 90.0
    seed: int = 0
    steps: int = 30
    cfg: float = 1.7
    prefix: str = "aidrama/bgm"


def build_music(job: MusicJob) -> dict:
    g = Graph()
    unet = g.add("UNETLoader", unet_name=M.MUSIC3_DIT, weight_dtype="default").out
    clip = g.add("CLIPLoader", clip_name=M.MUSIC3_TE, type="minimax", device="default").out
    vae = g.add("VAELoader", vae_name=M.MUSIC3_VAE).out
    enc = g.add("MiniMaxMusic3TextEncode", clip=clip, caption=job.caption, lyrics=job.lyrics, seed=job.seed,
                max_duration=job.seconds, cfg_scale=job.cfg, top_k=50)
    neg = g.add("ConditioningZeroOut", conditioning=enc[0]).out
    lat = g.add("EmptyMiniMaxMusic3LatentAudio", seconds=enc[1], batch_size=1).out
    samples = g.add("KSampler", model=unet, seed=job.seed, steps=job.steps, cfg=job.cfg, sampler_name="euler",
                    scheduler="simple", positive=enc[0], negative=neg, latent_image=lat, denoise=1.0).out
    audio = g.add("VAEDecodeAudioTiled", samples=samples, vae=vae, tile_size=1536, overlap=64).out
    g.add("SaveAudioAdvanced", audio=audio, filename_prefix=job.prefix, format="flac")
    return g.to_api()
