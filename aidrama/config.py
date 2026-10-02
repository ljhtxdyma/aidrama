"""运行配置：configs/default.yaml + 工程目录下的 aidrama.yaml（覆盖）+ 环境变量。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "configs" / "default.yaml"

# 画质预设 —— 只影响视频生成阶段
PRESETS: dict[str, dict[str, Any]] = {
    # 成片：H3 不加速，FL2VA 20 步 simple / Ref2VA 25 步 beta（参考图多时官方建议 beta），INT8 注意力
    "quality": {"fl2va_steps": 20, "ref2va_steps": 25, "fl2va_turbo": None, "ref2va_turbo": None, "fast": False,
                "fl2va_scheduler": "simple", "ref2va_scheduler": "beta", "width": 768, "height": 1344},
    # 平衡：FL2VA 用官方 Turbo 8 步 LoRA；Ref2VA 官方只有 4 步 LoRA 且会损伤音频，所以不用 LoRA、减到 16 步
    "balanced": {"fl2va_steps": 8, "ref2va_steps": 16, "fl2va_turbo": "8step", "ref2va_turbo": None, "fast": False,
                 "fl2va_scheduler": "simple", "ref2va_scheduler": "beta", "width": 768, "height": 1344},
    # 预演：FL2VA 用 FastH3（8 步 + VSA 稀疏注意力），Ref2VA 用 4 步 LoRA；低分辨率（4:7，与关键帧同比例），只看调度和表演
    "draft": {"fl2va_steps": 8, "ref2va_steps": 4, "fl2va_turbo": None, "ref2va_turbo": "4step", "fast": True,
              "fl2va_scheduler": "simple", "ref2va_scheduler": "simple", "width": 512, "height": 896},
}


def _deep_update(base: dict, upd: dict) -> dict:
    for k, v in (upd or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_config(project_dir: str | Path | None = None, overrides: dict | None = None) -> dict:
    with open(DEFAULT, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if project_dir:
        p = Path(project_dir) / "aidrama.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                _deep_update(cfg, yaml.safe_load(f) or {})
    env_map = {
        "AIDRAMA_COMFY_URL": ("comfy", "url"),
        "AIDRAMA_LLM_BASE_URL": ("llm", "base_url"),
        "AIDRAMA_LLM_MODEL": ("llm", "model"),
        "AIDRAMA_TTS_URL": ("audio", "tts_url"),
        "AIDRAMA_DESIGN_URL": ("audio", "design_url"),
        "AIDRAMA_ASR_URL": ("audio", "asr_url"),
    }
    for env, (a, b) in env_map.items():
        if os.environ.get(env):
            cfg.setdefault(a, {})[b] = os.environ[env]
    if os.environ.get("AIDRAMA_MOCK") == "1":
        cfg["mock"] = True
    _deep_update(cfg, overrides or {})
    preset = cfg.get("video", {}).get("preset", "quality")
    if preset not in PRESETS:
        raise ValueError(f"未知画质预设 {preset}，可选 {list(PRESETS)}")
    cfg["_preset"] = copy.deepcopy(PRESETS[preset])
    return cfg
