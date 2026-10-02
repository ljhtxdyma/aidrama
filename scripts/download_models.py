#!/usr/bin/env python
"""按 configs/models.yaml 下载模型到 ComfyUI/models/<目录>/，支持断点续传与国内镜像自动回退。

  python scripts/download_models.py --comfy D:/ComfyUI --groups core
  python scripts/download_models.py --comfy ~/ComfyUI --groups core,fast,lipsync --source modelscope
  python scripts/download_models.py --comfy ~/ComfyUI --list            # 只看清单和体积

--source 顺序：auto（默认）= huggingface → hf-mirror → modelscope 逐个尝试；
               hf-mirror / modelscope = 先试该镜像，失败再回退。
需要登录的仓库可设置环境变量 HF_TOKEN / MODELSCOPE_TOKEN。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
HF_RE = re.compile(r"https://huggingface\.co/([^/]+)/([^/]+)/resolve/([^/]+)/(.+)")


def candidates(url: str, source: str) -> list[str]:
    m = HF_RE.match(url)
    if not m:
        return [url]
    org, repo, rev, path = m.groups()
    hf = url
    mirror = f"https://hf-mirror.com/{org}/{repo}/resolve/{rev}/{path}"
    ms = f"https://modelscope.cn/models/{org}/{repo}/resolve/master/{path}"
    order = {"auto": [hf, mirror, ms], "huggingface": [hf, mirror, ms], "hf-mirror": [mirror, ms, hf], "modelscope": [ms, mirror, hf]}
    return order.get(source, [hf, mirror, ms])


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url: str, dst: Path, expect_gb: float | None) -> None:
    part = dst.with_suffix(dst.suffix + ".part")
    headers = {"User-Agent": "aidrama-downloader/1.0"}
    if "huggingface.co" in url or "hf-mirror.com" in url:
        tok = os.environ.get("HF_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    if "modelscope.cn" in url and os.environ.get("MODELSCOPE_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['MODELSCOPE_TOKEN']}"
    pos = part.stat().st_size if part.exists() else 0
    if pos:
        headers["Range"] = f"bytes={pos}-"
    with requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
        if r.status_code == 416:  # 已经下完
            part.rename(dst)
            return
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        if r.status_code == 200 and pos:
            pos = 0  # 服务器不支持续传，重新下载
        total = int(r.headers.get("Content-Length", 0)) + pos
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError("返回的是网页（可能需要登录或地址错误）")
        mode = "ab" if pos else "wb"
        t0, done, last = time.time(), pos, 0.0
        with open(part, mode) as f:
            for chunk in r.iter_content(chunk_size=8 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 2:
                    last = time.time()
                    speed = (done - pos) / max(1e-6, last - t0)
                    pct = f"{done / total:.0%}" if total else ""
                    print(f"\r    {human(done)}/{human(total) if total else '?'} {pct} {human(speed)}/s   ", end="", flush=True)
    print()
    size = part.stat().st_size
    if expect_gb and size < expect_gb * 1e9 * 0.85:
        raise RuntimeError(f"文件偏小（{human(size)}，期望约 {expect_gb} GB），保留 .part 以便续传")
    part.rename(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comfy", required=False, help="ComfyUI 根目录（里面有 models/）")
    ap.add_argument("--groups", default="core", help="逗号分隔：core,fast,lipsync,animate,qwen21,control 或 all")
    ap.add_argument("--source", default="auto", choices=["auto", "huggingface", "hf-mirror", "modelscope"])
    ap.add_argument("--manifest", default=str(ROOT / "configs" / "models.yaml"))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    man = yaml.safe_load(open(a.manifest, encoding="utf-8"))
    groups = None if a.groups == "all" else set(a.groups.split(","))
    files = [f for f in man["files"] if groups is None or f["group"] in groups]
    seen, uniq = set(), []
    for f in files:
        key = (f["dir"], f["file"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    total = sum(f.get("size_gb", 0) for f in uniq)
    print(f"共 {len(uniq)} 个文件，约 {total:.0f} GB（分组：{a.groups}）")
    if a.list or not a.comfy:
        for f in uniq:
            print(f"  [{f['group']:8s}] {f['dir']:22s} {f['file']:70s} {f.get('size_gb', 0):6.2f} GB  {f.get('license', '')}")
        if not a.comfy:
            print("\n加 --comfy <ComfyUI 根目录> 开始下载")
        return 0

    models = Path(a.comfy).expanduser() / "models"
    if not models.exists():
        print(f"找不到 {models}，请确认 --comfy 指向 ComfyUI 根目录")
        return 1
    failed = []
    for i, f in enumerate(uniq, 1):
        dst = models / f["dir"] / f["file"]
        if dst.exists() and dst.stat().st_size > 0:
            print(f"[{i}/{len(uniq)}] ✓ 已存在 {f['dir']}/{f['file']}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{i}/{len(uniq)}] ↓ {f['dir']}/{f['file']}（{f.get('size_gb', '?')} GB）")
        for url in candidates(f["url"], a.source):
            host = url.split("/")[2]
            for attempt in range(3):
                try:
                    print(f"    来源 {host}（第 {attempt + 1} 次）")
                    download(url, dst, f.get("size_gb"))
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"\n    失败：{e}")
                    time.sleep(3 * (attempt + 1))
                    if "HTTP 404" in str(e) or "HTTP 401" in str(e) or "网页" in str(e):
                        break
            if dst.exists():
                break
        if not dst.exists():
            failed.append(f"{f['dir']}/{f['file']}")
    if failed:
        print("\n以下文件下载失败，可稍后重跑本脚本（会自动续传），或手动下载后放到对应目录：")
        for x in failed:
            print("  " + x)
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
