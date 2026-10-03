"""命令行入口：python -m aidrama <命令> …   （python -m aidrama -h 查看全部命令）"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pl(args, need_project=True):
    from .pipeline import Pipeline

    ov: dict = {}
    if getattr(args, "mock", False):
        ov["mock"] = True
    if getattr(args, "preset", None):
        ov.setdefault("video", {})["preset"] = args.preset
    pl = Pipeline(args.project, ov)
    if need_project and pl.project is None:
        sys.exit(f"{args.project} 下没有 project.yaml，请先运行 new 或 init-demo")
    return pl


def cmd_new(a):
    from .pipeline import Pipeline

    ov = {"mock": True} if a.mock else {}
    Pipeline.create(a.project, a.idea, a.episodes, a.seconds, ov)


def cmd_init_demo(a):
    """不调用 LLM，直接用内置示例《第七封信》建一个工程（剧集圣经 + 第 1 集分镜）。"""
    from .mock import DEMO_BIBLE, DEMO_STORYBOARD
    from .schema import Scene
    from .story import _bible_to_project

    d = Path(a.project)
    d.mkdir(parents=True, exist_ok=True)
    p = _bible_to_project(DEMO_BIBLE, 30)
    ep = p.episodes[0]
    ep.scenes = [Scene.model_validate(s) for s in DEMO_STORYBOARD["scenes"]]
    ep.bgm_prompt = DEMO_STORYBOARD["bgm_prompt"]
    p.save(d / "project.yaml")
    from .pipeline import write_project_gitignore

    write_project_gitignore(d)
    print(f"示例工程已创建：{d / 'project.yaml'}")
    probs = p.check()
    print("一致性检查：" + ("通过" if not probs else "\n  " + "\n  ".join(probs)))


def cmd_check(a):
    pl = _pl(a)
    probs = pl.project.check()
    print("通过" if not probs else "\n".join(probs))


def cmd_storyboard(a):
    _pl(a).storyboard(a.episode, a.notes or "")


def cmd_cast(a):
    _pl(a).cast(force=a.force)


def cmd_voice(a):
    pl = _pl(a)
    pl.voices()
    pl.voice_lines(a.episode, force=a.force)


def cmd_plan(a):
    _pl(a).plan(a.episode)


def cmd_keyframes(a):
    _pl(a).keyframes(a.episode, force=a.force, only=a.only.split(",") if a.only else None)


def cmd_video(a):
    _pl(a).video(a.episode, takes=a.takes, only=a.only.split(",") if a.only else None, force=a.force)


def cmd_pick(a):
    _pl(a).pick(a.episode, a.segment, a.take)
    print(f"{a.segment} 已选 take {a.take}（重新运行 upscale/assemble 生效）")


def cmd_add_take(a):
    _pl(a).add_take(a.episode, a.segment, a.video, pick=not a.no_pick)


def cmd_upscale(a):
    _pl(a).upscale(a.episode, force=a.force)


def cmd_music(a):
    _pl(a).music(a.episode, force=a.force)


def cmd_assemble(a):
    _pl(a).assemble(a.episode)


def cmd_review(a):
    from .review import write_review

    write_review(_pl(a), a.episode)


def cmd_run(a):
    pl = _pl(a)
    res = pl.run_episode(a.episode, takes=a.takes)
    print(json.dumps({k: v for k, v in res.items() if k != "timeline"}, ensure_ascii=False, indent=1))


def cmd_fetch_guides(a):
    """下载 MiniMax 官方 H3 提示词指南（供本地 Context-IR 使用）。"""
    import requests

    from .h3prompt import GUIDE_CACHE

    GUIDE_CACHE.mkdir(parents=True, exist_ok=True)
    base = "https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/references/"
    for f in ("base-en.txt", "ref-en.txt"):
        for url in (base + f, base.replace("raw.githubusercontent.com", "ghproxy.net/https://raw.githubusercontent.com") + f):
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 200 and "Shot" in r.text:
                    (GUIDE_CACHE / f).write_text(r.text, encoding="utf-8")
                    print(f"✓ {f} → {GUIDE_CACHE / f}")
                    break
            except requests.RequestException:
                continue
        else:
            print(f"✗ {f} 下载失败；可手动从 https://github.com/MiniMax-AI/MiniMax-H3 复制到 {GUIDE_CACHE}")


def cmd_export(a):
    from .export import export_workflows

    for p in export_workflows(Path(a.out)):
        print(p)


def cmd_smoke(a):
    from .smoke import smoke

    sys.exit(0 if smoke(a.only.split(",") if a.only else None) else 1)


def cmd_doctor(a):
    from .doctor import doctor

    ok = doctor(a.project, a.comfy)
    sys.exit(0 if ok else 1)


def _safe_console() -> None:
    """Windows 控制台输出被重定向时默认用 GBK/cp1252，遇到 ✓ ✗ 等字符会直接崩溃：改成替换而不是报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _safe_console()
    ap = argparse.ArgumentParser(prog="aidrama", description="本地 AI 短剧流水线（RTX 5090 / ComfyUI 0.38 / MiniMax H3）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, ep=True, project=True):
        s = sub.add_parser(name, help=help_)
        if project:
            s.add_argument("project", help="工程目录")
        if ep:
            s.add_argument("episode", help="集 id，如 ep01")
        s.add_argument("--mock", action="store_true", help="不连接 ComfyUI/LLM/TTS，用占位素材跑通流程")
        s.set_defaults(fn=fn)
        return s

    s = add("new", cmd_new, "一句话创意 → 剧集圣经（LLM）", ep=False)
    s.add_argument("--idea", required=True)
    s.add_argument("--episodes", type=int, default=3)
    s.add_argument("--seconds", type=int, default=90)
    add("init-demo", cmd_init_demo, "用内置示例《第七封信》创建工程（不需要 LLM）", ep=False)
    add("check", cmd_check, "检查工程一致性", ep=False)
    s = add("storyboard", cmd_storyboard, "分集 → 分镜剧本（LLM）")
    s.add_argument("--notes", default="")
    s = add("cast", cmd_cast, "角色定妆照/设定图、场景图、角色音色", ep=False)
    s.add_argument("--force", action="store_true")
    s = add("voice", cmd_voice, "逐句配音（IndexTTS-2.5）")
    s.add_argument("--force", action="store_true")
    add("plan", cmd_plan, "镜头 → H3 生成段")
    s = add("keyframes", cmd_keyframes, "生成每个镜头的首帧")
    s.add_argument("--only", default="")
    s.add_argument("--force", action="store_true")
    s = add("video", cmd_video, "生成视频（H3）+ 自动质检")
    s.add_argument("--takes", type=int, default=None)
    s.add_argument("--only", default="", help="只生成这些段或镜头，逗号分隔")
    s.add_argument("--preset", choices=["quality", "balanced", "draft"])
    s.add_argument("--force", action="store_true")
    s = add("pick", cmd_pick, "人工选条：pick <工程> ep01 ep01_g03 2")
    s.add_argument("segment")
    s.add_argument("take", type=int)
    s = add("add-take", cmd_add_take, "登记流水线外做的视频（InfiniteTalk/Wan Animate/API 重做）：add-take <工程> ep01 ep01_g03 x.mp4")
    s.add_argument("segment")
    s.add_argument("video")
    s.add_argument("--no-pick", action="store_true", help="只登记不选中")
    s = add("upscale", cmd_upscale, "SeedVR2 超分到 1080x1920")
    s.add_argument("--force", action="store_true")
    s = add("music", cmd_music, "生成本集配乐")
    s.add_argument("--force", action="store_true")
    add("assemble", cmd_assemble, "合成成片")
    add("review", cmd_review, "生成审片页 review.html")
    s = add("run", cmd_run, "一键跑完整集")
    s.add_argument("--takes", type=int, default=None)
    s.add_argument("--preset", choices=["quality", "balanced", "draft"])
    add("fetch-guides", cmd_fetch_guides, "下载 MiniMax 官方 H3 提示词指南", ep=False, project=False)
    s = add("export-workflows", cmd_export, "导出各阶段 ComfyUI API 工作流示例", ep=False, project=False)
    s.add_argument("out")
    s = add("smoke", cmd_smoke, "真机冒烟测试：每个模型用最小参数推理一次（装好后先跑，约 10~15 分钟）", ep=False, project=False)
    s.add_argument("--only", default="", help="逗号分隔：image,h3,ref2va,upscale,music,audio")
    s = add("doctor", cmd_doctor, "体检：ComfyUI、节点、模型文件、音频服务、LLM、ffmpeg", ep=False, project=False)
    s.add_argument("--project", default=None)
    s.add_argument("--comfy", default=None)

    a = ap.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        print("警告：没有找到 ffmpeg（成片合成需要）", file=sys.stderr)
    a.fn(a)


if __name__ == "__main__":
    main()
