#!/usr/bin/env python
"""用 ComfyUI 自己的 validate_prompt() 离线校验 API 格式工作流（不执行、不需要 GPU）。

用法（必须用 ComfyUI 所在的 Python 环境运行）:
    python scripts/comfy_validate.py --comfy D:/ComfyUI workflows/api/*.json
    python scripts/comfy_validate.py --comfy ~/ComfyUI --stub-models workflows/api/*.json

--stub-models : 假装所有引用到的模型文件 / 输入素材都存在（云端 CI 用，那里没有模型权重）。
                不加该参数时，缺失的模型文件会被报告出来 —— 可以当作“模型是否下载齐全”的体检。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys

# Combo options that only exist when a CUDA/ROCm GPU is present.
GPU_ONLY_OPTIONS = {"comfy kitchen attention", "sageattention", "sage attention", "flash attention"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comfy", required=True, help="ComfyUI 根目录")
    ap.add_argument("--stub-models", action="store_true")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()

    # 先把工作流路径展开成绝对路径，再切到 ComfyUI 目录（相对路径否则会找不到）
    paths = []
    for pat in a.files:
        paths.extend(sorted(glob.glob(os.path.expanduser(pat))) or [pat])
    paths = [os.path.abspath(p) for p in paths]

    comfy = os.path.abspath(os.path.expanduser(a.comfy))
    sys.path.insert(0, comfy)
    os.chdir(comfy)
    sys.argv = [sys.argv[0]]

    import torch  # noqa: F401
    from comfy.cli_args import args as cargs

    if not torch.cuda.is_available():
        cargs.cpu = True
    import logging

    logging.disable(logging.ERROR)
    import execution
    import folder_paths
    import nodes

    graphs = {p: json.load(open(p, encoding="utf-8")) for p in paths}

    if a.stub_models:
        wanted = set()
        for g in graphs.values():
            for n in g.values():
                for v in n.get("inputs", {}).values():
                    if isinstance(v, str) and len(v) < 300:
                        wanted.add(v)
        orig_list = folder_paths.get_filename_list
        orig_full = folder_paths.get_full_path
        orig_exists = folder_paths.exists_annotated_filepath

        def get_filename_list(folder_name):
            base = list(orig_list(folder_name))
            return base + [w for w in wanted if "." in w and w not in base]

        def get_full_path(folder_name, filename):
            return orig_full(folder_name, filename) or os.path.join(comfy, "models", folder_name, filename)

        folder_paths.get_filename_list = get_filename_list
        folder_paths.get_full_path = get_full_path
        folder_paths.exists_annotated_filepath = lambda name: True

    async def run():
        await nodes.init_extra_nodes(init_custom_nodes=True, init_api_nodes=False)
        ok_all = True
        for p, g in graphs.items():
            valid, err, outs, node_errors = await execution.validate_prompt("validate", g, None)
            problems = []
            if not valid:
                if err and err.get("type") != "prompt_outputs_failed_validation":
                    problems.append(f"{err.get('type')}: {err.get('message')} {err.get('details', '')}")
                for nid, ne in (node_errors or {}).items():
                    for r in ne.get("errors", []):
                        detail = str(r.get("details", ""))
                        if r.get("type") == "value_not_in_list" and any(o in detail for o in GPU_ONLY_OPTIONS):
                            continue  # GPU-only option on a CPU box: fine
                        problems.append(f"  node {nid} ({ne.get('class_type')}): {r.get('type')}: {r.get('message')} | {detail[:300]}")
            status = "OK " if not problems else "ERR"
            print(f"[{status}] {p}  ({len(g)} nodes)")
            for pr in problems:
                print("      " + pr)
            ok_all &= not problems
        return ok_all

    return 0 if asyncio.run(run()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
