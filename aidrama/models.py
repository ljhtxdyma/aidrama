"""模型文件名的唯一来源（与 configs/models.yaml 的下载清单保持一致）。

文件名全部取自 ComfyUI 官方工作流模板（comfyui-workflow-templates 0.11.74，2026-10），
在 ComfyUI 0.38 中均由原生节点加载，不需要第三方插件。
"""

# --- MiniMax H3（主力视频模型，音画联合生成）
H3_FL2VA = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"        # 文生/首帧/首尾帧
H3_REF2VA = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"      # 全参考：≤9图 + ≤3视频 + ≤3音频
H3_FAST = "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors"  # FastH3 8 步蒸馏（仅 t2va/fl2va）
H3_TE = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
H3_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"               # 必须 fp32，否则音画不同步
H3_TURBO_FL2V_8 = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
H3_TURBO_FL2V_4 = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
H3_TURBO_REF2V_4 = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
H3_CONTROLNET = "minimax_h3_fun_controlnet_union_pruned_int8_convrot.safetensors"

# --- Qwen Image 2.1（定妆照 / 角色设定 / 分镜关键帧，支持最多 16 张参考图）
QWEN21_DIT = "qwen_image_2.1_int8_convrot.safetensors"
QWEN21_TE = "qwen3vl_8b_int8_convrot.safetensors"
QWEN21_VAE = "qwen_image_2.1_vae_bf16.safetensors"
QWEN21_PE_T2I = "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"   # 官方提示词扩写模型（可选）
QWEN21_PE_I2I = "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"

# --- Qwen-Image-Edit-2511（备选编辑模型）
QWEN_EDIT_2511 = "qwen_image_edit_2511_int8_convrot.safetensors"
QWEN_EDIT_2511_LIGHTNING = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
QWEN_25_VL_TE = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_IMAGE_VAE = "qwen_image_vae.safetensors"

# --- SeedVR2（视频超分 768x1344 -> 1080x1920）
SEEDVR2_7B = "seedvr2_7b_int8_convrot.safetensors"
SEEDVR2_3B = "seedvr2_3b_int8_convrot.safetensors"
SEEDVR2_VAE = "seedvr2_ema_vae_fp16.safetensors"

# --- 补帧
FILM = "film_net_fp16.safetensors"

# --- 音乐
MUSIC3_DIT = "minimax_music3_dit_fp16.safetensors"
MUSIC3_TE = "minimax_music3_text_encoder_pruned_int8_convrot.safetensors"
MUSIC3_VAE = "minimax_music3_dav.safetensors"

# --- Wan 2.1 系（InfiniteTalk 口型兜底 / Wan Animate 2 表演迁移）
WAN21_I2V_480 = "Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors"
WAN_UMT5 = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "Wan2_1_VAE_bf16.safetensors"
WAN_LIGHTX2V_480 = "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
INFINITETALK_PATCH = "wan2.1_infiniteTalk_multi_fp16.safetensors"
WAV2VEC_ZH = "wav2vec2-chinese-base_fp16.safetensors"
CLIP_VISION_H = "clip_vision_h.safetensors"
WAN_ANIMATE2 = "wan_animate_2_int8_convrot.safetensors"
WAN_ANIMATE2_DISTILL = "wan_animate_2_distill_int8_convrot.safetensors"
