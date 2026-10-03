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


def part_path(dst: Path, url: str) -> Path:
    """临时文件：<文件名>.<来源主机>.part。主机名里的端口冒号等在 Windows 文件名里非法（NTFS 会当成数据流），统一换成 _。"""
    host = re.sub(r"[^A-Za-z0-9.-]", "_", url.split("/")[2])
    return dst.with_name(f"{dst.name}.{host}.part")


def _too_small(size: int, expect_gb: float | None) -> bool:
    return bool(expect_gb) and size < expect_gb * 1e9 * 0.85


def download(url: str, dst: Path, expect_gb: float | None) -> None:
    """下载到 <文件>.part，校验通过才改名。续传只在同一个来源内进行（.part 带来源主机名），
    返回网页/JSON/LFS 指针或体积明显不对时删掉 .part，绝不把坏文件当成模型。"""
    part = part_path(dst, url)
    headers = {"User-Agent": "aidrama-downloader/1.1"}
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
        if r.status_code == 416:   # 服务器说已经下完：核对总大小再收下
            total = r.headers.get("Content-Range", "").rpartition("/")[2]
            if (total.isdigit() and int(total) == pos) and not _too_small(pos, expect_gb):
                part.rename(dst)
                return
            part.unlink(missing_ok=True)
            raise RuntimeError("续传状态异常（416），已删除临时文件，将重新下载")
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        ctype = r.headers.get("Content-Type", "")
        if ctype.startswith("text/") or "json" in ctype or "html" in ctype:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"返回的是 {ctype or '网页'}（可能需要登录、地址错误或被限流）")
        if r.status_code == 200 and pos:
            pos = 0  # 服务器不支持续传，重新下载
        total = int(r.headers.get("Content-Length", 0)) + pos
        if total and _too_small(total, expect_gb):
            raise RuntimeError(f"服务器报告的大小只有 {human(total)}，与清单（约 {expect_gb} GB）不符")
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
    if (total and size != total) or _too_small(size, expect_gb):
        if total and size < total:      # 正常断流：保留 .part，下次续传
            raise RuntimeError(f"下载中断（{human(size)}/{human(total)}），重跑会自动续传")
        part.unlink(missing_ok=True)
        raise RuntimeError(f"文件大小不对（{human(size)}，期望约 {expect_gb} GB），已删除，将重新下载")
    part.rename(dst)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # 重定向到文件时（Windows GBK）不要因为 ✓ 字符崩溃
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--comfy", required=False, help="ComfyUI 根目录（里面有 models/）")
    ap.add_argument("--groups", default="core", help="逗号分隔：core,fast,lipsync,animate,qwen21,control 或 all")
    ap.add_argument("--source", default="auto", choices=["auto", "huggingface", "hf-mirror", "modelscope"])
    ap.add_argument("--manifest", default=str(ROOT / "configs" / "models.yaml"))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    man = yaml.safe_load(open(a.manifest, encoding="utf-8"))
    known = {f["group"] for f in man["files"]}
    groups = None if a.groups.strip() == "all" else {g.strip() for g in a.groups.replace(" ", ",").split(",") if g.strip()}
    if groups is not None and (not groups or groups - known):
        print(f"未知的模型组：{sorted(groups - known) or a.groups!r}；可选：{', '.join(sorted(known))} 或 all")
        return 2
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
            if _too_small(dst.stat().st_size, f.get("size_gb")):
                bad = dst.with_name(dst.name + ".bad")
                dst.replace(bad)
                print(f"[{i}/{len(uniq)}] ! {f['dir']}/{f['file']} 只有 {human(bad.stat().st_size)}，明显不完整，已改名为 .bad 并重新下载")
            else:
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
                    if any(k in str(e) for k in ("HTTP 404", "HTTP 401", "HTTP 403", "返回的是", "大小只有")):
                        break       # 这个来源没有/不给这个文件：换下一个来源
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
