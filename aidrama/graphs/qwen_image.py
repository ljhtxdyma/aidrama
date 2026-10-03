"""Qwen Image 2.1（2026-09，原生节点）：文生图 + 多参考图编辑（最多 16 张参考）。

用途：
  * 角色定妆照 / 三视图 / 表情表        → text-to-image 或以定妆照为参考的 edit
  * 场景设定图                          → text-to-image
  * 每个镜头的首帧/尾帧（关键帧）         → edit：<image1>=角色A定妆照, <image2>=角色B, <image3>=场景图 …

多图编辑时提示词里必须用 <image1>、<image2> … 指代参考图（官方 PE 规范）。
官方模板参数：euler / simple / 25 步 / cfg 1 / QwenImage21Cache(auto)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import models as M
from ..graph import Graph

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


@dataclass
class QwenImageJob:
    prompt: str
    width: int = 896
    height: int = 1568
    refs: list[str] = field(default_factory=list)   # ComfyUI input 名称；空 = 文生图
    negative: str = ""
    steps: int = 25
    cfg: float = 1.0
    seed: int = 0
    batch: int = 1
    ref_resolution: int = 1024                       # 参考图缩放到约 N×N 像素
    enhance: bool = False                            # 使用官方 Qwen3.5-9B 提示词扩写模型（需额外下载 8.8GB）
    prefix: str = "aidrama/img"


def build_qwen_image(job: QwenImageJob) -> dict:
    if len(job.refs) > 16:
        raise ValueError("Qwen Image 2.1 最多 16 张参考图")
    if job.width % 16 or job.height % 16:
        raise ValueError("宽高需为 16 的倍数")
    g = Graph()
    unet = g.add("UNETLoader", "Qwen Image 2.1", unet_name=M.QWEN21_DIT, weight_dtype="default").out
    model = g.add("QwenImage21Cache", model=unet, device="auto", dtype="default").out
    clip = g.add("CLIPLoader", "Qwen3-VL-8B TE", clip_name=M.QWEN21_TE, type="qwen_image", device="default").out
    vae = g.add("VAELoader", vae_name=M.QWEN21_VAE).out

    ref_nodes = [g.add("LoadImage", image=r)[0] for r in job.refs]

    prompt = job.prompt
    if job.enhance:
        sys_file = _PROMPTS / ("qwen_image21_pe_edit.md" if job.refs else "qwen_image21_pe_t2i.md")
        system = sys_file.read_text(encoding="utf-8").split("-->", 1)[-1].strip()
        pe_clip = g.add(
            "CLIPLoader", "Qwen Image 2.1 prompt enhancer",
            clip_name=M.QWEN21_PE_I2I if job.refs else M.QWEN21_PE_T2I,
            type="qwen_image" if job.refs else "stable_diffusion", device="default",
        ).out
        sys_node = g.add("PrimitiveStringMultiline", value=system).out
        gen = g.add(
            "TextGenerate", "Prompt enhancer", clip=pe_clip, prompt=job.prompt, max_length=4096,
            sampling_mode="on", **{
                "sampling_mode.temperature": 1.0, "sampling_mode.top_k": 20, "sampling_mode.top_p": 0.95,
                "sampling_mode.min_p": 0.0, "sampling_mode.repetition_penalty": 1.0,
                "sampling_mode.seed": job.seed, "sampling_mode.presence_penalty": 1.5 if not job.refs else 0.0,
            },
            thinking=True, use_default_template=True, mtp="auto", system_prompt=sys_node,
            image=ref_nodes[0] if ref_nodes else None,
        )
        prompt = gen[0]

    enc_kw = {f"images.image_{i + 1}": n for i, n in enumerate(ref_nodes)}
    enc = g.add(
        "TextEncodeQwenImage21", "Encode", clip=clip, prompt=prompt, negative_prompt=job.negative,
        resolution=job.ref_resolution, vae=vae if ref_nodes else None, **enc_kw,
    )
    latent = g.add("EmptyLatentImage", width=job.width, height=job.height, batch_size=job.batch).out
    samples = g.add(
        "KSampler", model=model, seed=job.seed, steps=job.steps, cfg=job.cfg, sampler_name="euler",
        scheduler="simple", positive=enc[0], negative=enc[1], latent_image=latent, denoise=1.0,
    ).out
    image = g.add("VAEDecode", samples=samples, vae=vae).out
    g.add("SaveImage", "Save", images=image, filename_prefix=job.prefix)
    return g.to_api()
