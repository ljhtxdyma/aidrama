"""环境体检：ffmpeg、ComfyUI（版本/显卡/节点/模型文件）、音频服务、LLM、H3 官方提示词指南。"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from . import net

from .config import ROOT, load_config
from .h3prompt import GUIDE_CACHE

REQUIRED_NODES = [
    "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AddGuide", "ModelAttentionBackend",
    "TextEncodeQwenImageEditPlus", "FluxKontextMultiReferenceLatentMethod", "CFGNorm", "EmptySD3LatentImage",
    "SeedVR2Conditioning", "SeedVR2TemporalChunk", "MiniMaxMusic3TextEncode", "FrameInterpolate",
    "WanInfiniteTalkToVideo", "WanAnimate2ToVideo", "SaveVideo", "CreateVideo", "LoadAudio", "LoadVideo",
]


def _ok(flag: bool, msg: str) -> bool:
    print(("  ✓ " if flag else "  ✗ ") + msg)
    return flag


def doctor(project: str | None = None, comfy_url: str | None = None) -> bool:
    cfg = load_config(project)
    url = (comfy_url or cfg["comfy"]["url"]).rstrip("/")
    allok = True
    print("[ffmpeg]")
    allok &= _ok(bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")), "ffmpeg / ffprobe 在 PATH 中")

    print(f"[ComfyUI] {url}")
    try:
        st = net.get(url + "/system_stats", timeout=5).json()
        sysi = st.get("system", {})
        _ok(True, f"ComfyUI {sysi.get('comfyui_version', '?')}，Python {sysi.get('python_version', '?').split()[0]}，PyTorch {sysi.get('pytorch_version', '?')}")
        for d in st.get("devices", []):
            vram = d.get("vram_total", 0) / 1024**3
            _ok(vram > 0, f"{d.get('name')}  显存 {vram:.1f} GB")
            if "5090" not in d.get("name", "") and vram < 30:
                print("    提示：本工作流按 RTX 5090 32GB 调优，显存更小的卡请用 balanced/draft 预设或更低精度权重")
        ver = sysi.get("comfyui_version", "0")
        try:
            major = tuple(int(x) for x in ver.split(".")[:2])
            allok &= _ok(major >= (0, 38), "ComfyUI ≥ 0.38（低版本缺 H3 AddGuide / Qwen 2511 / SeedVR2 原生节点）")
        except ValueError:
            pass
        info = net.get(url + "/object_info", timeout=30).json()
        missing = [n for n in REQUIRED_NODES if n not in info]
        allok &= _ok(not missing, "原生节点齐全" if not missing else f"缺少节点：{missing}（请更新 ComfyUI）")
        man = yaml.safe_load(open(ROOT / "configs" / "models.yaml", encoding="utf-8"))
        by_group: dict[str, list[str]] = {}
        cache: dict[str, set[str]] = {}
        for f in man["files"]:
            folder = f["dir"]
            if folder not in cache:
                try:
                    cache[folder] = set(net.get(f"{url}/models/{folder}", timeout=10).json())
                except Exception:  # noqa: BLE001
                    cache[folder] = set()
            if f["file"] not in cache[folder]:
                by_group.setdefault(f["group"], []).append(f"{folder}/{f['file']}")
        for g in ("core", "fast", "lipsync", "animate", "qwen21", "control"):
            miss = by_group.get(g, [])
            flag = _ok(not miss, f"模型组 {g:8s} " + ("齐全" if not miss else f"缺 {len(miss)} 个：{', '.join(miss[:3])}{' …' if len(miss) > 3 else ''}"))
            if g == "core":
                allok &= flag
        if by_group:
            print("    补齐：python scripts/download_models.py --comfy <ComfyUI 目录> --groups core[,fast,…] [--source modelscope]")
    except Exception as e:  # noqa: BLE001
        allok &= _ok(False, f"无法连接 ComfyUI：{e}（先启动 ComfyUI）")

    print("[音频服务]")
    for key, what in (("tts_url", "IndexTTS 配音"), ("design_url", "音色设计"), ("asr_url", "识别/对齐")):
        try:
            h = net.get(cfg["audio"][key].rstrip("/") + "/health", timeout=5).json()
            _ok(True, f"{what} {cfg['audio'][key]}  engines={h.get('engines')}")
            for m, present in (h.get("models") or {}).items():
                if present is False:
                    allok &= _ok(False, f"    {m} 的权重文件不在（下载中断过？重跑安装脚本会补全）")
        except Exception:  # noqa: BLE001
            allok &= _ok(False, f"{what} {cfg['audio'][key]} 未启动（见 docs/02-安装部署.md 第 5 步）")

    print("[LLM]")
    try:
        from .llm import LLMConfig

        key = LLMConfig.from_dict(cfg["llm"]).api_key        # 会解析 env:变量名
        if str(cfg["llm"].get("api_key", "")).startswith("env:") and not key:
            allok &= _ok(False, f"环境变量 {cfg['llm']['api_key'][4:]} 没有设置（LLM 的 API key）")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = net.get(cfg["llm"]["base_url"].rstrip("/") + "/models", timeout=10, headers=headers)
        allok &= _ok(r.status_code < 400, f"{cfg['llm']['base_url']} " + ("可访问" if r.status_code < 400 else
                     f"返回 HTTP {r.status_code}（401/403 多半是 API key 不对，404 多半是 base_url 少了 /v1）"))
        try:
            ids = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            ids = []
        if ids:
            want = cfg["llm"]["model"]
            hit = any(i == want or i.split(":")[0] == want or i == want + ":latest" for i in ids)
            allok &= _ok(hit, f"模型 {want}" + ("" if hit else f" 不存在；已有：{', '.join(ids[:6])}。Ollama 可用 ollama cp <模型> {want} 建别名，或在 aidrama.yaml 里改 llm.model"))
    except Exception as e:  # noqa: BLE001
        allok &= _ok(False, f"LLM 接口不可用：{e}")

    print("[H3 官方提示词指南]")
    have = all((GUIDE_CACHE / f).exists() for f in ("base-en.txt", "ref-en.txt"))
    _ok(have, f"{GUIDE_CACHE}" + ("" if have else "  缺失 → 运行 python -m aidrama fetch-guides（不影响运行，但会降低提示词质量）"))
    print("\n结论：" + ("可以开工 ✅" if allok else "有必需项未就绪 ❌"))
    return allok
