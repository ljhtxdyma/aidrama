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
    # 成片：H3 不加速，Ref2VA 25 步 / FL2VA 20 步，INT8 注意力（实测身份与口型无损，约 2 倍提速）
    "quality": {"fl2va_steps": 20, "ref2va_steps": 25, "turbo": None, "fast": False,
                "fl2va_scheduler": "simple", "ref2va_scheduler": "beta", "width": 768, "height": 1344},
    # 平衡：官方 Turbo LoRA 8 步（对白镜头不要用 4 步）
    "balanced": {"fl2va_steps": 8, "ref2va_steps": 8, "turbo": "8step", "fast": False,
                 "fl2va_scheduler": "simple", "ref2va_scheduler": "simple", "width": 768, "height": 1344},
    # 预演：FastH3 8 步 / ref2va 4 步 LoRA，低分辨率，只用来看调度和表演
    "draft": {"fl2va_steps": 8, "ref2va_steps": 4, "turbo": "4step", "fast": True,
              "fl2va_scheduler": "simple", "ref2va_scheduler": "simple", "width": 480, "height": 864},
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
