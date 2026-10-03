"""Qwen-Image-2512（文生图）+ Qwen-Image-Edit-2511（多参考编辑）—— Apache-2.0，可商用。

这是默认的图像引擎。Qwen Image 2.1 画质更强、支持 16 张参考，但它是 Qwen Research License（仅限非商用），
只在 configs 里 image.engine: qwen21 时使用。

参数取自 ComfyUI 官方模板 image_qwen_Image_2512 / image_qwen_image_edit_2511_int8：
  2512 T2I : ModelSamplingAuraFlow(3.1) → KSampler euler/simple，50 步，cfg 4，官方中文负向词
  2511 Edit: ModelSamplingAuraFlow(3.1) → CFGNorm(1.0) → KSampler euler/simple，40 步，cfg 4（模板里 KSampler 显示 3，但实际由 CFG 开关链接的 4.0 覆盖）；
             TextEncodeQwenImageEditPlus 最多 3 张参考（image1~3），FluxKontextMultiReferenceLatentMethod(index_timestep_zero)
             可选 Lightning 4 步 LoRA（cfg 1）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import models as M
from ..graph import Graph

NEG_ZH = "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲"

QWEN2512_DIT = "qwen_image_2512_fp8_e4m3fn.safetensors"
QWEN2512_LIGHTNING = "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors"


@dataclass
class QwenT2IJob:
    prompt: str
    width: int = 1024
    height: int = 1536
    negative: str = NEG_ZH
    steps: int = 50
    cfg: float = 4.0
    lightning: bool = False          # 4 步 LoRA（cfg 1），用于快速试稿
    seed: int = 0
    batch: int = 1
    prefix: str = "aidrama/img"


def build_qwen2512_t2i(job: QwenT2IJob) -> dict:
    g = Graph()
    model = g.add("UNETLoader", "Qwen-Image-2512", unet_name=QWEN2512_DIT, weight_dtype="default").out
    if job.lightning:
        model = g.add("LoraLoaderModelOnly", model=model, lora_name=QWEN2512_LIGHTNING, strength_model=1.0).out
    model = g.add("ModelSamplingAuraFlow", model=model, shift=3.1).out
    clip = g.add("CLIPLoader", clip_name=M.QWEN_25_VL_TE, type="qwen_image", device="default").out
    vae = g.add("VAELoader", vae_name=M.QWEN_IMAGE_VAE).out
    pos = g.add("CLIPTextEncode", text=job.prompt, clip=clip).out
    neg = g.add("CLIPTextEncode", text=job.negative, clip=clip).out
    latent = g.add("EmptySD3LatentImage", width=job.width, height=job.height, batch_size=job.batch).out
    steps, cfg = (4, 1.0) if job.lightning else (job.steps, job.cfg)
    samples = g.add("KSampler", model=model, seed=job.seed, steps=steps, cfg=cfg, sampler_name="euler",
                    scheduler="simple", positive=pos, negative=neg, latent_image=latent, denoise=1.0).out
    img = g.add("VAEDecode", samples=samples, vae=vae).out
    g.add("SaveImage", "Save", images=img, filename_prefix=job.prefix)
    return g.to_api()


@dataclass
class QwenEditJob:
    prompt: str                      # 用 Picture 1 / Picture 2 / Picture 3（或 图1/图2/图3）指代参考图
    refs: list[str] = field(default_factory=list)   # ≤3 张，ComfyUI input 名；第 1 张放身份锚点
    width: int = 896                 # 输出尺寸（0 = 跟随第 1 张参考图）；官方按 ~1MP 训练，4:7 取 896x1568（1.4MP）
    height: int = 1568
    steps: int = 40
    cfg: float = 4.0
    lightning: bool = False
    seed: int = 0
    prefix: str = "aidrama/img"


def build_qwen2511_edit(job: QwenEditJob) -> dict:
    if not 1 <= len(job.refs) <= 3:
        raise ValueError("Qwen-Image-Edit-2511 需要 1~3 张参考图")
    g = Graph()
    model = g.add("UNETLoader", "Qwen-Image-Edit-2511", unet_name=M.QWEN_EDIT_2511, weight_dtype="default").out
    model = g.add("ModelSamplingAuraFlow", model=model, shift=3.1).out
    model = g.add("CFGNorm", model=model, strength=1.0).out
    if job.lightning:
        model = g.add("LoraLoaderModelOnly", model=model, lora_name=M.QWEN_EDIT_2511_LIGHTNING, strength_model=1.0).out
    clip = g.add("CLIPLoader", clip_name=M.QWEN_25_VL_TE, type="qwen_image", device="default").out
    vae = g.add("VAELoader", vae_name=M.QWEN_IMAGE_VAE).out
    imgs = []
    for i, r in enumerate(job.refs):
        im = g.add("LoadImage", image=r)[0]
        if i == 0:
            im = g.add("FluxKontextImageScale", image=im).out
        imgs.append(im)
    kw = {f"image{i + 1}": im for i, im in enumerate(imgs)}
    pos = g.add("TextEncodeQwenImageEditPlus", "Encode +", clip=clip, prompt=job.prompt, vae=vae, **kw).out
    neg = g.add("TextEncodeQwenImageEditPlus", "Encode -", clip=clip, prompt="", vae=vae, **kw).out
    pos = g.add("FluxKontextMultiReferenceLatentMethod", conditioning=pos, reference_latents_method="index_timestep_zero").out
    neg = g.add("FluxKontextMultiReferenceLatentMethod", conditioning=neg, reference_latents_method="index_timestep_zero").out
    if job.width and job.height:
        latent = g.add("EmptySD3LatentImage", width=job.width, height=job.height, batch_size=1).out
    else:
        latent = g.add("VAEEncode", pixels=imgs[0], vae=vae).out
    steps, cfg = (4, 1.0) if job.lightning else (job.steps, job.cfg)
    samples = g.add("KSampler", model=model, seed=job.seed, steps=steps, cfg=cfg, sampler_name="euler",
                    scheduler="simple", positive=pos, negative=neg, latent_image=latent, denoise=1.0).out
    img = g.add("VAEDecode", samples=samples, vae=vae).out
    g.add("SaveImage", "Save", images=img, filename_prefix=job.prefix)
    return g.to_api()
