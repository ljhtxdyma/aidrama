"""MiniMax H3 视频（音画联合）工作流构建器 —— 复刻 ComfyUI 官方模板的节点连接，
并把官方模板里分散的几种用法（I2V / 首尾帧 / 全参考 / 多帧锚定 / 音频锚定）合并成一个参数化函数。

关键事实（来自官方模板与 MiniMax-H3 README）：
  * 原生画布短边 768，竖屏 9:16 = 768x1344；24fps；时长 4~15 秒；帧数必须是 17k+5（124 帧≈5s）
  * 采样器 res_multistep；CFG 蒸馏权重 → BasicGuider（无负向）；基础 20 步，Ref2VA 带音频建议 ≥25 步
  * 参考图多时 scheduler 用 beta/normal 往往优于 simple
  * Turbo LoRA：8 步可用于对白；4 步会损伤音频，只适合纯画面草稿
  * FastH3 蒸馏权重只支持 t2va/fl2va，不支持 ref2va
  * MiniMaxH3AddGuide 可以把图像/视频片段/音频钉在时间轴任意帧（frame_idx, 24fps）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .. import models as M
from ..graph import Graph

FPS = 24


def snap_length(seconds: float) -> int:
    """秒 → H3 合法帧数（向上取到 17k+5，与官方模板的 Math Expression 完全一致）。"""
    n = max(5, round(seconds * FPS))
    return n + (5 - n % 17) % 17


def seconds_of(length: int) -> float:
    return length / FPS


@dataclass
class Guide:
    """时间轴锚点：在第 frame_idx 帧钉住一张图 / 一段视频帧 / 一段音频。"""
    frame_idx: int
    image: Optional[str] = None      # ComfyUI input 中的图片名
    video: Optional[str] = None      # ComfyUI input 中的视频名（取其帧，长度会被裁到 17k+5）
    audio: Optional[str] = None      # ComfyUI input 中的音频名（例如预先合成好的对白）


@dataclass
class H3Job:
    prompt: str
    seconds: float = 5.0
    width: int = 768
    height: int = 1344
    mode: Literal["fl2va", "ref2va"] = "ref2va"
    first_frame: Optional[str] = None
    last_frame: Optional[str] = None          # 仅 fl2va
    ref_images: list[str] = field(default_factory=list)   # 仅 ref2va，≤9
    ref_videos: list[str] = field(default_factory=list)   # 仅 ref2va，≤3
    ref_audios: list[str] = field(default_factory=list)   # 仅 ref2va，≤3（音色参考）
    guides: list[Guide] = field(default_factory=list)
    anchor_first_frame: bool = True           # ref2va 时把 first_frame 同时钉在第 0 帧
    steps: int = 25
    sampler: str = "res_multistep"
    scheduler: str = "beta"
    turbo: Optional[Literal["4step", "8step"]] = None
    fast: bool = False                         # FastH3 8 步蒸馏模型（fl2va only）
    attention: Optional[str] = "comfy kitchen attention"   # INT8 注意力，5090 上约 2 倍提速；None=默认
    ref_image_size: Literal["match", "max"] = "match"
    seed: int = 0
    prefix: str = "aidrama/h3"

    @property
    def length(self) -> int:
        return snap_length(self.seconds)

    def check(self) -> list[str]:
        errs = []
        if self.width % 32 or self.height % 32:
            errs.append("width/height 必须是 32 的倍数")
        if min(self.width, self.height) > 768:
            errs.append("H3 原生短边上限 768（竖屏用 768x1344）")
        if not 4.0 <= self.seconds <= 15.0:
            errs.append("H3 时长范围 4~15 秒")
        if self.mode == "fl2va" and (self.ref_images or self.ref_videos or self.ref_audios):
            errs.append("参考图/视频/音频需要 mode=ref2va")
        if self.mode == "ref2va" and self.last_frame:
            errs.append("ref2va 不支持 last_frame，请改用 Guide(frame_idx=-1, image=...)")
        if self.fast and self.mode != "fl2va":
            errs.append("FastH3 只支持 fl2va")
        if len(self.ref_images) > 9 or len(self.ref_videos) > 3 or len(self.ref_audios) > 3:
            errs.append("参考数量超限：图≤9 视频≤3 音频≤3")
        if self.ref_audios and not (self.ref_images or self.ref_videos):
            errs.append("音频参考不能单独使用，至少还要一张参考图或一段参考视频")
        if self.turbo == "4step" and (self.ref_audios or any(g.audio for g in self.guides)):
            errs.append("4 步 turbo 会损伤音频，对白镜头请用 8step 或不用 turbo")
        for g in self.guides:
            if g.frame_idx >= self.length:
                errs.append(f"guide frame_idx {g.frame_idx} 超出视频长度 {self.length}")
        return errs


def build_h3(job: H3Job) -> dict:
    errs = job.check()
    if errs:
        raise ValueError("H3Job 参数错误: " + "; ".join(errs))
    g = Graph()
    if job.fast:
        unet_name = M.H3_FAST
    else:
        unet_name = M.H3_REF2VA if job.mode == "ref2va" else M.H3_FL2VA
    model = g.add("UNETLoader", "H3 DiT", unet_name=unet_name, weight_dtype="default").out
    clip = g.add("CLIPLoader", "Qwen3-VL-32B TE", clip_name=M.H3_TE, type="minimax", device="default").out
    vae = g.add("VAELoader", "H3 video VAE", vae_name=M.H3_VAE).out
    avae = g.add("VAELoader", "H3 audio VAE (fp32)", vae_name=M.H3_AUDIO_VAE).out

    if job.turbo and not job.fast:
        lora = {
            ("fl2va", "8step"): M.H3_TURBO_FL2V_8,
            ("fl2va", "4step"): M.H3_TURBO_FL2V_4,
            ("ref2va", "4step"): M.H3_TURBO_REF2V_4,
            ("ref2va", "8step"): M.H3_TURBO_REF2V_4,  # 官方暂无 ref2v 8 步 LoRA，用 4 步 LoRA 跑 8 步
        }[(job.mode, job.turbo)]
        model = g.add("LoraLoaderModelOnly", "Turbo LoRA", model=model, lora_name=lora, strength_model=1.0).out
    if job.fast:
        model = g.add("MiniMaxH3SigmaShift", model=model, shift_video=10.0, shift_audio=3.0).out
    if job.attention:
        model = g.add("ModelAttentionBackend", model=model, attention=job.attention).out

    def load_image(name: str):
        return g.add("LoadImage", image=name)[0]

    def load_audio(name: str):
        return g.add("LoadAudio", audio=name).out

    def load_video_frames(name: str):
        v = g.add("LoadVideo", file=name).out
        return g.add("GetVideoComponents", video=v)[0]

    if job.mode == "fl2va":
        ff = load_image(job.first_frame) if job.first_frame else None
        lf = load_image(job.last_frame) if job.last_frame else None
        cond = g.add(
            "MiniMaxH3ImageToVideo", "H3 FL2VA", clip=clip, vae=vae, prompt=job.prompt,
            width=job.width, height=job.height, length=job.length, first_frame=ff, last_frame=lf,
        )
    else:
        kw: dict = {}
        refs = list(job.ref_images)
        # 首帧也作为 <Picture N> 送入，以便提示词里能引用它
        if job.first_frame and job.first_frame not in refs:
            refs.append(job.first_frame)
        ref_nodes = {}
        for i, name in enumerate(refs[:9]):
            ref_nodes[name] = load_image(name)
            kw[f"ref_images.ref_image_{i}"] = ref_nodes[name]
        for i, name in enumerate(job.ref_videos):
            kw[f"ref_videos.ref_video_{i}"] = load_video_frames(name)
        for i, name in enumerate(job.ref_audios):
            kw[f"ref_audios.ref_audio_{i}"] = load_audio(name)
        cond = g.add(
            "MiniMaxH3ReferenceToVideo", "H3 Ref2VA", clip=clip, vae=vae, audio_vae=avae, prompt=job.prompt,
            width=job.width, height=job.height, length=job.length, ref_image_size=job.ref_image_size, **kw,
        )
        if job.first_frame and job.anchor_first_frame:
            job.guides = [Guide(0, image=job.first_frame)] + [x for x in job.guides if not (x.frame_idx == 0 and x.image)]

    positive, latent = cond[0], cond[1]
    for gd in job.guides:
        img = None
        if gd.image:
            img = load_image(gd.image)
        elif gd.video:
            img = load_video_frames(gd.video)
        aud = load_audio(gd.audio) if gd.audio else None
        positive = g.add(
            "MiniMaxH3AddGuide", f"Guide@{gd.frame_idx}", positive=positive, latent=latent, frame_idx=gd.frame_idx,
            vae=vae if img is not None else None, audio_vae=avae if aud is not None else None, image=img, audio=aud,
        )[0]

    steps = 8 if job.fast else job.steps
    noise = g.add("RandomNoise", noise_seed=job.seed).out
    guider = g.add("BasicGuider", model=model, conditioning=positive).out
    sampler = g.add("KSamplerSelect", sampler_name=job.sampler).out
    sigmas = g.add("BasicScheduler", model=model, scheduler="simple" if job.fast else job.scheduler, steps=steps, denoise=1.0).out
    out = g.add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=latent)[0]
    images = g.add("VAEDecode", samples=out, vae=vae).out
    audio = g.add("VAEDecodeAudio", samples=out, vae=avae).out
    video = g.add("CreateVideo", images=images, audio=audio, fps=float(FPS)).out
    g.add("SaveVideo", "Save", video=video, filename_prefix=job.prefix, format="mp4", **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 12.0})
    return g.to_api()
