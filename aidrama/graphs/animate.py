"""Wan Animate 2（2026-08，Apache-2.0，ComfyUI 原生）：真人表演 / 动作视频 → AI 角色。

短剧用法：打戏、走位、需要细腻表演的情绪特写 —— 演员（或你自己）用手机竖屏拍一段表演，
把角色设定图作为 reference_image，动作、表情、口型都从驱动视频迁移过来。
参数复刻官方模板 video_wan_animate2_distilled：蒸馏模型 lcm 10 步 / shift 5 / cfg 1，
超过 81 帧时启用 ContextWindowsManual（21/8，freenoise）。输出没有对白声音的话，后期再配 TTS。
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import models as M
from ..graph import Graph

NEG_WAN = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，"
           "多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")


@dataclass
class AnimateJob:
    reference_image: str         # 角色设定图 / 关键帧（ComfyUI input 名）
    driving_video: str           # 真人表演视频（ComfyUI input 名）
    prompt: str                  # "Character Description: …\nBackground description: …"
    pose_prompt: str = "A reference video of a person performing."
    frames: int = 81             # 驱动视频帧数（会对齐到 4k+1）
    width: int = 720
    height: int = 1280
    steps: int = 10
    seed: int = 0
    distilled: bool = True
    prefix: str = "aidrama/animate"


def build_wan_animate2(job: AnimateJob) -> dict:
    length = max(5, (job.frames - 1) // 4 * 4 + 1)
    g = Graph()
    model = g.add("UNETLoader", unet_name=M.WAN_ANIMATE2_DISTILL if job.distilled else M.WAN_ANIMATE2, weight_dtype="default").out
    if length > 81:
        model = g.add("ContextWindowsManual", model=model, context_length=21, context_overlap=8, context_schedule="standard_static",
                      context_stride=1, closed_loop=False, fuse_method="pyramid", dim=2, freenoise=True,
                      cond_retain_index_list="0", split_conds_to_windows=False, latent_retain_index_list="", causal_window_fix=True).out
    model = g.add("WanAnimate2Cache", model=model, device="gpu", dtype="int8").out
    sampling_model = g.add("ModelSamplingSD3", model=model, shift=5.0).out
    clip = g.add("CLIPLoader", clip_name=M.WAN_UMT5, type="wan", device="default").out
    vae = g.add("VAELoader", vae_name=M.WAN_VAE).out
    cv = g.add("CLIPVisionLoader", clip_name=M.CLIP_VISION_H).out
    pos = g.add("CLIPTextEncode", text=job.prompt, clip=clip).out
    neg = g.add("CLIPTextEncode", text=NEG_WAN, clip=clip).out
    pos_pose = g.add("CLIPTextEncode", text=job.pose_prompt, clip=clip).out
    ref = g.add("LoadImage", image=job.reference_image)[0]
    vid = g.add("LoadVideo", file=job.driving_video).out
    comps = g.add("GetVideoComponents", video=vid)
    pose = g.add("ResizeImageMaskNode", input=comps[0], resize_type="scale dimensions", scale_method="area",
                 **{"resize_type.width": job.width, "resize_type.height": job.height, "resize_type.crop": "center"}).out
    ref_r = g.add("ResizeImageMaskNode", input=ref, resize_type="scale dimensions", scale_method="area",
                  **{"resize_type.width": job.width, "resize_type.height": job.height, "resize_type.crop": "center"}).out
    cv_ref = g.add("CLIPVisionEncode", clip_vision=cv, image=ref_r, crop="none").out
    first = g.add("ImageFromBatch", image=pose, batch_index=0, length=1).out
    cv_pose = g.add("CLIPVisionEncode", clip_vision=cv, image=first, crop="none").out
    anim = g.add("WanAnimate2ToVideo", positive=pos, negative=neg, vae=vae, width=job.width, height=job.height, length=length,
                 batch_size=1, video_frame_offset=0, pose_strength=1.0, pose_start_percent=0.0, pose_end_percent=1.0,
                 reference_image_strength=1.0, reference_image=ref_r, pose_video=pose, clip_vision_output=cv_ref,
                 positive_pose=pos_pose, clip_vision_output_pose=cv_pose)
    sampler = g.add("KSamplerSelect", sampler_name="lcm").out
    sigmas = g.add("BasicScheduler", model=model, scheduler="simple", steps=job.steps, denoise=1.0).out
    lat = g.add("SamplerCustom", model=sampling_model, add_noise=True, noise_seed=job.seed, cfg=1.0, positive=anim[0],
                negative=anim[1], sampler=sampler, sigmas=sigmas, latent_image=anim[2])[0]
    lat = g.add("TrimVideoLatent", samples=lat, trim_amount=anim[3]).out
    img = g.add("VAEDecode", samples=lat, vae=vae).out
    out = g.add("CreateVideo", images=img, audio=comps[1], fps=comps[2]).out
    g.add("SaveVideo", video=out, filename_prefix=job.prefix, format="mp4",
          **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 12.0})
    return g.to_api()
