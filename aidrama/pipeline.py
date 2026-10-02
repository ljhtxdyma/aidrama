"""AI 短剧流水线编排。每个阶段都可单独重跑；产物登记在 <工程>/project.yaml。

阶段：
  bible       一句话创意 → 剧集圣经（角色/场景/分集）                 LLM
  storyboard  分集 → 分场分镜剧本（镜头、台词、英文提示词）             LLM
  cast        角色定妆照 + 角色设定图 + 场景设定图 + 角色音色样本      Qwen-Image-2512/Edit-2511 + Qwen3-TTS VoiceDesign
  voice       逐句配音（情绪控制）                                     IndexTTS-2.5
  plan        镜头 → H3 生成段（按配音实际时长排时间轴）
  keyframes   每个镜头的首帧（多参考：角色设定图 + 场景图）            Qwen-Image-Edit-2511
  video       每段抽 N 条（H3 Ref2VA / FL2VA）+ 自动质检 + 自动选优    MiniMax H3
  upscale     768x1344 → 1080x1920                                     SeedVR2
  music       本集配乐                                                 MiniMax Music 3
  assemble    拼接、转场、BGM 闪避、字幕、AI 标识、响度                ffmpeg
  review      生成审片页 review.html
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from . import ffmpeg_utils as ff
from . import qc as QC
from .assemble import AssembleOptions, Clip, assemble
from .audio import AudioClient, build_track, trim_silence
from .comfy_client import ComfyClient, ComfyError, upload_name
from .config import load_config
from .graph import validate_api_graph
from .graphs.h3 import Guide, H3Job, build_h3, snap_length
from .graphs.post import MusicJob, UpscaleJob, build_music, build_seedvr2
from .graphs.qwen_edit import QwenEditJob, QwenT2IJob, build_qwen2511_edit, build_qwen2512_t2i
from .graphs.qwen_image import QwenImageJob, build_qwen_image
from .h3prompt import compile_fl2va, compile_ref2va, lint, refine
from .llm import LLM, LLMConfig
from .mock import mock_llm, placeholder_image, placeholder_video, tone_wav
from .planner import build_plan, plan_segments, substitute, to_records
from .schema import Episode, Project, Segment, Take
from .story import make_bible, make_storyboard
from .subtitles import Cue

SIZE_EN = {
    "extreme-close-up": "extreme close-up", "close-up": "close-up", "medium-close-up": "medium close-up",
    "medium": "medium shot", "medium-wide": "medium-wide shot", "full": "full shot", "wide": "wide shot",
    "establishing": "wide establishing shot", "over-the-shoulder": "over-the-shoulder shot",
}


def _seed(*parts) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12], 16) % (2**31)


class Pipeline:
    def __init__(self, project_dir: str | Path, overrides: dict | None = None, log: Callable[[str], None] = print):
        self.dir = Path(project_dir).resolve()
        self.cfg = load_config(self.dir, overrides)
        self.mock = bool(self.cfg.get("mock"))
        self.log = log
        self.pfile = self.dir / "project.yaml"
        self.project: Project | None = Project.load(self.pfile) if self.pfile.exists() else None
        self.llm = LLM(LLMConfig.from_dict(self.cfg["llm"]), mock=mock_llm if self.mock else None)
        self.audio = AudioClient(self.cfg["audio"], mock=self.mock)
        self.comfy = None if self.mock else ComfyClient(self.cfg["comfy"]["url"])
        self._last_family = None
        self._gpu_owner: str | None = None
        self.graph_dir = self.dir / "graphs"
        self._object_info = None

    # ------------------------------------------------------------------ utils
    def save(self) -> None:
        assert self.project is not None
        self.project.save(self.pfile)

    def rel(self, p: str | Path) -> str:
        p = Path(p).resolve()
        try:
            return p.relative_to(self.dir).as_posix()
        except ValueError:
            return str(p)

    def abs(self, p: str | None) -> Path | None:
        if not p:
            return None
        q = Path(p)
        return q if q.is_absolute() else self.dir / q

    def _gpu(self, tenant: str) -> None:
        """一块 32GB 显卡由三方轮流使用：comfy（画面/视频/超分/配乐）、audio（配音/识别）、llm（本地大模型）。
        换人之前让其他两方释放显存，避免 H3 生成时显存被 TTS 或 LLM 占着导致 OOM 或大量换页变慢。"""
        if tenant == self._gpu_owner:
            return
        self._gpu_owner = tenant
        if self.mock or not self.cfg.get("gpu", {}).get("handoff", True):
            return
        if tenant != "comfy" and self.comfy:
            try:
                self.comfy.free()
            except Exception:  # noqa: BLE001
                pass
            self._last_family = None
        if tenant != "audio":
            self.audio.unload()
        if tenant != "llm":
            self.llm.unload()

    def _family(self, fam: str) -> None:
        """切换模型家族时释放显存（图像 → 视频 → 超分），避免 32GB 显存里残留上一个大模型。"""
        self._gpu("comfy")
        if self.comfy and self._last_family and fam != self._last_family and self.cfg["comfy"].get("free_between_stages", True):
            try:
                self.comfy.free()
            except Exception:  # noqa: BLE001
                pass
        self._last_family = fam

    def _upload(self, path: str | Path) -> str:
        if self.mock:
            return f"aidrama/{upload_name(path)}"
        return self.comfy.upload(path, subfolder=self.cfg["comfy"].get("input_subfolder", "aidrama"))

    def _check_graph(self, api: dict, name: str) -> None:
        """保存每个实际提交的工作流（可直接拖进 ComfyUI 复现）；mock 模式下对照 0.38 节点快照做静态校验。"""
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        (self.graph_dir / f"{name}.json").write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
        if self.mock:
            snap = Path(__file__).resolve().parent.parent / "tests" / "data" / "object_info_comfyui_0.38.json"
            if snap.exists():
                if self._object_info is None:
                    self._object_info = json.loads(snap.read_text(encoding="utf-8"))
                errs = [e for e in validate_api_graph(api, self._object_info, allow_missing_files=True) if "dangling" not in e]
                if errs:
                    raise ComfyError(f"[{name}] 工作流静态校验失败:\n  " + "\n  ".join(errs))

    def _run(self, api: dict, out_dir: Path, stem: str, name: str) -> list[Path]:
        self._check_graph(api, name)
        t0 = time.time()
        paths = self.comfy.run(api, out_dir, stem, on_progress=None)
        self.log(f"    ✓ {name}  {time.time() - t0:.0f}s")
        return paths

    # ------------------------------------------------------------------ 1. bible
    @classmethod
    def create(cls, project_dir: str | Path, idea: str, episodes: int = 3, seconds: int = 90,
               overrides: dict | None = None, log=print) -> "Pipeline":
        d = Path(project_dir)
        d.mkdir(parents=True, exist_ok=True)
        pl = cls(d, overrides, log)
        pl._gpu("llm")
        log(f"[bible] 用 LLM 生成剧集圣经：{idea}")
        pl.project = make_bible(pl.llm, idea, episodes, seconds)
        pl.save()
        log(f"[bible] {pl.project.series.title}：{len(pl.project.characters)} 个角色，{len(pl.project.locations)} 个场景，{len(pl.project.episodes)} 集")
        return pl

    # ------------------------------------------------------------------ 2. storyboard
    def storyboard(self, ep_id: str, notes: str = "") -> Episode:
        self._gpu("llm")
        old_bgm_prompt = self.project.episode(ep_id).bgm_prompt
        ep = make_storyboard(self.llm, self.project, ep_id, notes)
        # 重写分镜后：成片作废；配乐描述变了就重做配乐；旧视频由 plan 按内容签名判断能否沿用
        ep.output = None
        if ep.bgm_prompt != old_bgm_prompt:
            ep.bgm = None
        self.save()
        n = sum(len(s.shots) for s in ep.scenes)
        total = sum(sh.duration for s in ep.scenes for sh in s.shots)
        self.log(f"[storyboard] {ep_id}《{ep.title}》{len(ep.scenes)} 场 {n} 个镜头，约 {total:.0f}s")
        return ep

    # ------------------------------------------------------------------ 3. cast
    def cast(self, force: bool = False) -> None:
        p = self.project
        icfg = self.cfg["image"]
        eng = icfg.get("engine", "qwen2511")
        style = p.series.image_style_en
        out = self.dir / "assets"
        self._family("image")
        for c in p.characters:
            cdir = out / "characters" / c.id
            # 定妆照（脸部锚点）
            if force or not c.refs.get("front"):
                prompt = (f"Vertical portrait photograph, head and shoulders, front view, looking straight into the camera, neutral expression, "
                          f"{c.appearance_en}. Wearing {c.outfits.get('default', '')}. Plain light-grey studio backdrop, soft even "
                          f"studio lighting, {style}. A unique, distinctive, natural face; not a celebrity.")
                self.log(f"[cast] {c.name} 定妆照")
                c.refs["front"] = self.rel(self._image_t2i(prompt, cdir / "front.png", icfg["sheet_width"], icfg["sheet_height"],
                                                           _seed(c.id, "front"), f"{c.id}_front", eng))
                self.save()
            # 每套服装一张角色设定图（正面全身 + 3/4 侧 + 背面 + 面部特写），作为后续所有参考的身份锚点
            for key, outfit in c.outfits.items():
                rk = f"sheet_{key}"
                if force or not c.refs.get(rk):
                    prompt = ("Character reference sheet of the person in Picture 1 on a plain light-grey background, three views standing side by side: "
                              f"full-body front view, full-body three-quarter view and full-body back view, wearing {outfit}. "
                              "Keep the face, hairstyle, skin tone and body shape of Picture 1 exactly. Even soft studio lighting, "
                              f"{style}. No text, no labels.")
                    self.log(f"[cast] {c.name} 设定图（{key}）")
                    c.refs[rk] = self.rel(self._image_edit(prompt, [self.abs(c.refs["front"])], cdir / f"{rk}.png",
                                                            1344, 896, _seed(c.id, rk), f"{c.id}_{rk}", eng))
                    self.save()
        for loc in p.locations:
            if force or not loc.refs.get("plate"):
                prompt = (f"Vertical film still of an empty location, no people: {loc.description_en}. Eye-level wide view, "
                          f"{style}. No text.")
                self.log(f"[cast] 场景 {loc.name}")
                loc.refs["plate"] = self.rel(self._image_t2i(prompt, out / "locations" / loc.id / "plate.png",
                                                             icfg["width"], icfg["height"], _seed(loc.id, "plate"), f"{loc.id}_plate", eng))
                self.save()
        self.voices(force)

    def voices(self, force: bool = False) -> None:
        for c in self.project.characters:
            designed = f"assets/characters/{c.id}/voice.wav"
            if c.voice.ref_audio and (not force or c.voice.ref_audio != designed):
                if force:
                    self.log(f"[voice] {c.name} 用的是你提供的录音 {c.voice.ref_audio}，--force 也不会覆盖（要重新设计请先清空 ref_audio）")
                continue
            if not c.voice.description:
                continue
            self._gpu("audio")
            text = c.voice.design_text or "这件事我想了很久，今天必须当面问清楚。你到底还有什么瞒着我？"
            self.log(f"[voice] 设计 {c.name} 的音色：{c.voice.description}")
            raw = self.dir / "assets" / "characters" / c.id / "voice_raw.wav"
            self.audio.design_voice(text, c.voice.description, raw)
            c.voice.ref_audio = self.rel(trim_silence(raw, raw.with_name("voice.wav")))
            c.voice.ref_text = text
            self.save()

    def _image_t2i(self, prompt: str, out: Path, w: int, h: int, seed: int, name: str, engine: str) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        if engine == "qwen21":
            api = build_qwen_image(QwenImageJob(prompt=prompt, width=w, height=h, seed=seed, prefix=f"aidrama/{name}",
                                                steps=self.cfg["image"].get("steps", 25), enhance=self.cfg["image"].get("enhance", False)))
        else:
            api = build_qwen2512_t2i(QwenT2IJob(prompt=prompt, width=w, height=h, seed=seed, prefix=f"aidrama/{name}"))
        if self.mock:
            self._check_graph(api, name)
            return placeholder_image(out, w // 4, h // 4, name)
        return self._run(api, out.parent, out.stem, name)[0]

    def _image_edit(self, prompt: str, refs: list[Path], out: Path, w: int, h: int, seed: int, name: str, engine: str) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        names = [self._upload(r) for r in refs]
        if engine == "qwen21":
            p21 = prompt
            for i in range(len(refs), 0, -1):
                p21 = p21.replace(f"Picture {i}", f"<image{i}>")
            api = build_qwen_image(QwenImageJob(prompt=p21, refs=names, width=w, height=h, seed=seed, prefix=f"aidrama/{name}",
                                                steps=self.cfg["image"].get("steps", 25), enhance=self.cfg["image"].get("enhance", False)))
        else:
            api = build_qwen2511_edit(QwenEditJob(prompt=prompt, refs=names[:3], width=w, height=h, seed=seed, prefix=f"aidrama/{name}"))
        if self.mock:
            self._check_graph(api, name)
            return placeholder_image(out, w // 4, h // 4, name, "0x553344")
        return self._run(api, out.parent, out.stem, name)[0]

    # ------------------------------------------------------------------ 4. voice (lines)
    def voice_lines(self, ep_id: str, force: bool = False) -> None:
        ep = self.project.episode(ep_id)
        lang = self.project.series.language
        adir = self.dir / "episodes" / ep_id / "audio"
        for sc in ep.scenes:
            for sh in sc.shots:
                for ln in sh.dialogue:
                    ch = self.project.character(ln.speaker)
                    emo_ref = ch.voice.emotion_refs.get(ln.emotion)
                    # 文件名带上“台词+情绪+音色”的哈希：插入/删除/改写台词不会覆盖别的句子，改过的句子自动重配
                    key = hashlib.sha1(json.dumps([ln.speaker, ln.text, ln.emotion, ln.emo_vector, ch.voice.ref_audio, emo_ref],
                                                  ensure_ascii=False).encode()).hexdigest()[:8]
                    target = adir / f"{sh.id}_{key}.wav"
                    auto = bool(ln.audio) and re.fullmatch(rf"{re.escape(sh.id)}_[0-9a-f]{{8}}\.wav", Path(ln.audio).name) is not None
                    if ln.audio and self.abs(ln.audio).exists() and not force and (not auto or self.abs(ln.audio) == target):
                        if ln.duration is None:          # 你自己放进来的录音：只补时长
                            ln.duration = round(ff.duration(self.abs(ln.audio)), 3)
                        continue
                    if not ch.voice.ref_audio:
                        raise RuntimeError(f"角色 {ch.name} 还没有参考音色，请先运行 cast/voices")
                    self._gpu("audio")
                    raw = adir / f"{sh.id}_{key}_raw.wav"
                    self.audio.tts(ln.text, self.abs(ch.voice.ref_audio), raw, ln.emotion, ln.emo_vector,
                                   str(self.abs(emo_ref)) if emo_ref else None, lang)
                    clean = trim_silence(raw, target)
                    ln.audio = self.rel(clean)
                    ln.duration = round(ff.duration(clean), 3)
                    self.log(f"[voice] {sh.id} {ch.name}：{ln.plain}（{ln.duration:.2f}s）")
                self.save()

    # ------------------------------------------------------------------ 5. plan
    def plan(self, ep_id: str, quiet: bool = False) -> list[Segment]:
        ep = self.project.episode(ep_id)
        segs = plan_segments(self.project, ep, self.cfg)
        limit = min(float(self.cfg["video"].get("max_seconds", 15.0)), 15.0)
        too_long = [s for s in segs if s.planned > limit + 1e-6]
        if too_long:
            smap = self.project.shot_map(ep)
            msg = "; ".join(f"{s.id}（{', '.join(s.shots)}）需要 {s.planned:.1f}s" for s in too_long)
            hint = [sid for s in too_long for sid in s.shots if smap[sid][1].dialogue]
            raise ValueError(f"以下生成段超过 H3 单次上限 {limit:.0f} 秒：{msg}。"
                             + (f"请把镜头 {', '.join(hint)} 的台词拆到两个镜头里，" if hint else "")
                             + "或缩短镜头时长（对白镜头的时长由配音实际长度决定）")
        old = {o.sig: o for o in ep.segments if o.sig}
        pdir = self.dir / "episodes" / ep_id / "prompts"
        for s in segs:
            s.sig = self._seg_sig(ep, s)
            o = old.get(s.sig)
            if o:      # 内容没变就保留已有的生成结果（即使段号因为前面的改动而变了）
                s.takes, s.chosen, s.video_final, s.status, s.prompt = o.takes, o.chosen, o.video_final, o.status, o.prompt
                s.picked_by_hand, s.dialogue_track, s.refs = o.picked_by_hand, o.dialogue_track, o.refs
                if o.id != s.id and (pdir / f"{o.id}.manual.txt").exists() and not (pdir / f"{s.id}.manual.txt").exists():
                    (pdir / f"{o.id}.manual.txt").rename(pdir / f"{s.id}.manual.txt")
                    self.log(f"[plan] 人工提示词 {o.id}.manual.txt → {s.id}.manual.txt（段号变了，镜头没变）")
        old_by_id = {o.id: o for o in ep.segments}
        for s in segs:   # 段号相同但镜头已经变了：旧的人工提示词不能套到新镜头上
            o = old_by_id.get(s.id)
            m = pdir / f"{s.id}.manual.txt"
            if m.exists() and o is not None and o.shots != s.shots:
                m.rename(pdir / f"{s.id}.manual.{time.strftime('%Y%m%d%H%M%S')}.orphan.txt")
                self.log(f"[plan] ! {s.id} 的镜头变了，原 {m.name} 已改名为 orphan，请检查后重新放置")
        ep.segments = segs
        self.save()
        if not quiet:
            for s in segs:
                self.log(f"[plan] {s.id} {s.engine:10s} {len(s.shots)} 镜 {s.planned:5.2f}s（生成 {s.gen_seconds:.2f}s）: {', '.join(s.shots)}")
        return segs

    def _seg_sig(self, ep: Episode, seg: Segment) -> str:
        """生成段的内容签名：影响画面/声音的字段（含关键帧文件内容、配音文件）变了，旧视频就不再沿用。"""
        smap = self.project.shot_map(ep)
        h = hashlib.sha1(f"{seg.engine}|{seg.planned:.3f}|{seg.cut_times}".encode())
        for sid in seg.shots:
            sc, sh = smap[sid]
            h.update(json.dumps([sid, sc.location, sc.lighting_en, sc.sound_en, sh.keyframe_prompt, sh.end_keyframe_prompt,
                                 sh.motion_prompt, sh.camera_en, sh.characters, sh.outfit, sh.method, sh.end_state, sh.sfx,
                                 sh.transition, sh.driving_video,
                                 [(ln.speaker, ln.text, ln.emotion, ln.delivery, ln.voiceover, ln.audio) for ln in sh.dialogue]],
                                ensure_ascii=False, sort_keys=True).encode())
            for kf in (sh.keyframe, sh.end_keyframe):
                f = self.abs(kf)
                if f and f.exists():
                    h.update(hashlib.sha1(f.read_bytes()).digest())
        return h.hexdigest()[:10]

    # ------------------------------------------------------------------ 6. keyframes
    def keyframes(self, ep_id: str, force: bool = False, only: list[str] | None = None) -> None:
        p = self.project
        ep = p.episode(ep_id)
        icfg = self.cfg["image"]
        eng = icfg.get("engine", "qwen2511")
        kdir = self.dir / "episodes" / ep_id / "keyframes"
        self._family("image")
        for sc in ep.scenes:
            loc = p.location(sc.location)
            for sh in sc.shots:
                if only and sh.id not in only:
                    continue
                if sh.method in ("t2v", "continue"):
                    continue
                for which in ("start", "end") if sh.method == "flf2v" else ("start",):
                    attr = "keyframe" if which == "start" else "end_keyframe"
                    if getattr(sh, attr) and not force:
                        continue
                    text = sh.keyframe_prompt if which == "start" else sh.end_keyframe_prompt
                    refs = []
                    names = {c.id: c.identity_en for c in p.characters}   # 画面外的角色也要替换掉 [id]
                    max_refs = 3 if eng == "qwen2511" else 9
                    for c in sh.characters[: max_refs]:
                        ch = p.character(c)
                        ref = ch.refs.get(f"sheet_{sh.outfit.get(c, 'default')}") or ch.refs.get("front")
                        if ref:
                            refs.append(self.abs(ref))
                            names[c] = f"the {_noun(ch.gender)} from Picture {len(refs)}"
                    plate = loc.refs.get("plate") if loc else None
                    if plate and len(refs) < max_refs:
                        refs.append(self.abs(plate))
                        loc_line = f"The setting is the location shown in Picture {len(refs)}; keep its layout, props and lighting."
                    else:
                        loc_line = f"Setting: {loc.description_en}." if loc else ""
                    keep = " ".join(f"Keep the face, hairstyle and costume of the person in Picture {i + 1} exactly." for i in range(len(names)))
                    prompt = (f"Vertical 9:16 live-action film still, {SIZE_EN.get(sh.shot_size, sh.shot_size)}, {sh.angle} angle. "
                              f"{substitute(text, names)} {loc_line} {sc.lighting_en}. {keep} "
                              f"{p.series.image_style_en}. No text, no subtitles, no watermark.").replace("  ", " ")
                    self.log(f"[keyframe] {sh.id}{'（尾帧）' if which == 'end' else ''}")
                    out = kdir / f"{sh.id}{'_end' if which == 'end' else ''}.png"
                    seed = sh.seed if sh.seed is not None else _seed(ep_id, sh.id, which)
                    if refs:
                        path = self._image_edit(prompt, refs, out, icfg["width"], icfg["height"], seed, f"{ep_id}_{sh.id}_{which}", eng)
                    else:
                        path = self._image_t2i(prompt, out, icfg["width"], icfg["height"], seed, f"{ep_id}_{sh.id}_{which}", eng)
                    setattr(sh, attr, self.rel(path))
                    self.save()

    # ------------------------------------------------------------------ 7. video
    def video(self, ep_id: str, takes: int | None = None, only: list[str] | None = None, force: bool = False) -> None:
        """三遍走完，保证同一时刻只有一个模型占显存：
        A. 编译提示词（本地 LLM 扩写）→ B. ComfyUI 抽卡 + 技术质检 → C. 对白回读质检（ASR）+ 自动选优。"""
        p = self.project
        ep = p.episode(ep_id)
        self.plan(ep_id, quiet=bool(ep.segments))    # 配音/分镜改过也能拿到最新时间轴；内容没变的段保留原有结果
        vcfg = self.cfg["video"]
        pre = self.cfg["_preset"]
        takes = takes or int(vcfg.get("takes", 2))
        vdir = self.dir / "episodes" / ep_id / "segments"
        vdir.mkdir(parents=True, exist_ok=True)
        smap = p.shot_map(ep)

        todo: list[Segment] = []
        pending: list[tuple[Segment, Take]] = []   # 上次中断留下的：已生成但还没做对白质检/没选条
        for seg in ep.segments:
            if only and seg.id not in only and not any(s in only for s in seg.shots):
                continue
            if seg.engine == "wan_animate":
                dt = self._dialogue_track(ep, seg)
                self.log(f"[video] {seg.id} 为 Wan Animate 2 动作迁移段，请按 docs/03 第 11 节用 workflows/12_wan_animate2 手动生成，"
                         f"再用 add-take 登记" + (f"；对白音轨：{dt}" if dt else ""))
                self.save()
                continue
            chosen_take = seg.takes[seg.chosen] if seg.chosen is not None and seg.chosen < len(seg.takes) else None
            if force:   # 重抽：丢掉流水线生成的条，保留 add-take 登记的外部视频
                seg.takes = [t for t in seg.takes if t.preset == "external"]
                if chosen_take is None or chosen_take.preset != "external":
                    chosen_take, seg.picked_by_hand = None, False
                seg.video_final = None
            done = [t for t in seg.takes if self.abs(t.path) and self.abs(t.path).exists()]
            if len(done) != len(seg.takes) or (chosen_take is not None and chosen_take not in done):
                seg.takes = done
                if chosen_take not in done:     # 选中的那条文件没了
                    chosen_take, seg.picked_by_hand, seg.video_final = None, False, None
            seg.chosen = done.index(chosen_take) if chosen_take is not None else None
            pending += [(seg, t) for t in seg.takes if t.qc.get("cer") is None and t.preset != "external"]
            generated = [t for t in seg.takes if t.preset != "external"]
            if len(generated) < takes:
                todo.append(seg)
        if not todo and not pending:
            self.save()
            return

        # A. 提示词（补抽时沿用已有条目的提示词，保证同一段的各条可比；有 .manual.txt 时重新读取）
        pdir = self.dir / "episodes" / ep_id / "prompts"
        need = {seg.id for seg in todo
                if not (seg.prompt and any(t.preset != "external" for t in seg.takes)) or (pdir / f"{seg.id}.manual.txt").exists()}
        if need and vcfg.get("refine_prompt_with_llm", True):
            self._gpu("llm")
        for seg in todo:
            dtrack = self._dialogue_track(ep, seg)
            if seg.id not in need:
                continue
            cont = smap[seg.shots[0]][1].method == "continue"
            plan = build_plan(p, ep, seg, self.cfg, dtrack, "<上一段尾帧>" if cont else None)
            seg.prompt = self._compile_prompt(ep, seg, plan)
            self.save()

        new: list[tuple[Segment, Take]] = list(pending)
        try:
            # B. 生成（ComfyUI）
            if todo:
                self._family("video")
            for seg in todo:
                idx = ep.segments.index(seg)
                prev_last = None
                if smap[seg.shots[0]][1].method == "continue":
                    ps = ep.segments[idx - 1] if idx > 0 else None
                    if ps is None or not ps.takes:
                        self.log(f"[video] ! {seg.id} 是续写镜头，但上一段还没有视频，跳过")
                        continue
                    self._choose(ps, tech_only=ps.chosen is None)
                    if not ps.picked_by_hand:
                        ps.picked_by_hand = True    # 续写接在这条上了：以后不再自动改选（要换请 pick 后重抽本段）
                        self.log(f"[video] {ps.id} 锁定 take {ps.chosen + 1}（{seg.id} 从它的结尾续写）")
                    src = self.abs(ps.takes[ps.chosen].path)
                    # 成片里上一段只用到 planned 秒（生成的文件更长），所以取 planned 处的那一帧，而不是文件最后一帧
                    prev_last = str(ff.frame_at(src, max(0.0, min(ps.planned, ff.duration(src)) - 1 / 24),
                                                vdir / f"{seg.id}_prev_last.png"))
                plan = build_plan(p, ep, seg, self.cfg, str(self.abs(seg.dialogue_track)) if seg.dialogue_track else None, prev_last)
                seg.refs = to_records(plan)
                for r in seg.refs:
                    r.path = self.rel(r.path) if Path(r.path).is_absolute() else r.path
                while sum(1 for t in seg.takes if t.preset != "external") < takes:
                    n = self._next_take_no(seg, vdir)
                    seed = _seed(ep_id, seg.sig or seg.id, "take", n)
                    out = vdir / f"{seg.id}_{seg.sig[:6]}_t{n}.mp4" if seg.sig else vdir / f"{seg.id}_t{n}.mp4"
                    self.log(f"[video] {seg.id} take {n}（{plan.mode}, {seg.gen_seconds:.1f}s, seed {seed}）")
                    t0 = time.time()
                    path = self._gen_video(plan, seg.prompt, seg, seed, out, pre, vcfg)
                    take = Take(path=self.rel(path), seed=seed, preset=vcfg.get("preset", "quality"), seconds=round(time.time() - t0, 1))
                    issues = QC.technical(path, seg.planned)
                    if not self.mock and len(seg.shots) > 1:
                        issues += QC.cuts(path, seg.cut_times)
                    take.qc = {"pass": not any(QC.is_hard(i) for i in issues), "issues": issues, "cer": None}
                    seg.takes.append(take)
                    new.append((seg, take))
                    self.save()
        finally:
            # C. 对白质检（ASR）+ 选优 —— 即使 B 中途出错也把已生成的条处理完，下次不会卡在“有视频但没选条”
            if any(self._seg_lines(ep, seg) for seg, _ in new):
                self._gpu("audio")
            for seg, take in new:
                try:
                    self._qc_dialogue(ep, seg, take)
                except Exception as e:  # noqa: BLE001
                    self.log(f"[video] ! {seg.id} 对白质检失败：{e}")
                self.save()
            touched = {seg.id: seg for seg in [s for s, _ in new] + todo}
            for seg in touched.values():
                if not seg.takes:
                    continue
                self._choose(seg)
                seg.status = "video"
                bad = [i + 1 for i, t in enumerate(seg.takes) if not t.qc.get("pass")]
                if bad:
                    self.log(f"[video] {seg.id} 未过质检的条：{bad}（详见 review.html）")
            self.save()

    @staticmethod
    def _next_take_no(seg: Segment, vdir: Path) -> int:
        """新条目的编号：比现有任何条目（含已删除文件的）都大，避免覆盖文件或复用种子。"""
        import re as _re
        nums = [int(m.group(1)) for t in seg.takes for m in [_re.search(r"_[tx](\d+)\.\w+$", t.path)] if m]
        nums += [int(m.group(1)) for f in vdir.glob(f"{seg.id}_*") for m in [_re.search(r"_[tx](\d+)\.\w+$", f.name)] if m]
        return max(nums, default=0) + 1

    def _dialogue_track(self, ep: Episode, seg: Segment) -> str | None:
        smap = self.project.shot_map(ep)
        items = []
        for sid, t0 in zip(seg.shots, seg.cut_times):
            for ln in smap[sid][1].dialogue:
                if ln.audio:
                    items.append((self.abs(ln.audio), t0 + (ln.start or 0.0)))
        if not items:
            seg.dialogue_track = None     # 台词删光了：不能再把旧音轨送给 H3
            return None
        out = self.dir / "episodes" / ep.id / "audio" / f"{seg.id}_dialogue.wav"
        build_track(items, max(seg.gen_seconds, 2.0), out)
        seg.dialogue_track = self.rel(out)
        return str(out)

    def _compile_prompt(self, ep: Episode, seg: Segment, plan) -> str:
        spec = plan.spec
        n_img = sum(1 for r in plan.refs if r.kind == "image")
        n_aud = sum(1 for r in plan.refs if r.kind == "audio")
        if plan.mode == "ref2va":
            draft = compile_ref2va(spec, plan.refs, plan.identity)
        else:
            draft = compile_fl2va(spec, first_frame=bool(plan.first_frame), last_frame=bool(plan.last_frame))
            n_img = int(bool(plan.first_frame)) + int(bool(plan.last_frame))
        issues = lint(draft, spec, plan.mode, n_img, n_aud)
        if issues:
            raise RuntimeError(f"{seg.id} 确定性提示词未通过校验（请检查分镜）：{issues}")
        pdir = self.dir / "episodes" / ep.id / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{seg.id}.draft.txt").write_text(draft, encoding="utf-8")
        manual = pdir / f"{seg.id}.manual.txt"
        if manual.exists():     # 人工改过的提示词优先（只提示问题，不拦截）
            prompt = manual.read_text(encoding="utf-8").strip()
            seg.prompt_issues = lint(prompt, spec, plan.mode, n_img, n_aud)
            self.log(f"    使用人工提示词 {manual.name}" + (f"（注意：{seg.prompt_issues}）" if seg.prompt_issues else ""))
            return prompt
        prompt, notes = draft, []
        if self.cfg["video"].get("refine_prompt_with_llm", True):
            smap = self.project.shot_map(ep)
            ctx = {"shots": [{"id": s, "action_zh": smap[s][1].action, "shot_size": smap[s][1].shot_size,
                              "keyframe": smap[s][1].keyframe_prompt} for s in seg.shots],
                   "characters": {c: plan.identity[c] for c in plan.identity}}
            prompt, notes = refine(self.llm, draft, spec, plan.mode, ctx, n_img, n_aud)
        seg.prompt_issues = notes
        (pdir / f"{seg.id}.txt").write_text(prompt, encoding="utf-8")
        for n in notes:
            self.log(f"    ! {n}")
        return prompt

    def _gen_video(self, plan, prompt: str, seg: Segment, seed: int, out: Path, pre: dict, vcfg: dict) -> Path:
        mode = plan.mode
        job = H3Job(
            prompt=prompt, seconds=seg.gen_seconds, width=pre["width"], height=pre["height"], mode=mode,
            steps=pre["ref2va_steps"] if mode == "ref2va" else pre["fl2va_steps"],
            scheduler=pre["ref2va_scheduler"] if mode == "ref2va" else pre["fl2va_scheduler"],
            turbo=pre["ref2va_turbo"] if mode == "ref2va" else (None if pre["fast"] else pre["fl2va_turbo"]),
            fast=bool(pre["fast"] and mode == "fl2va"),
            attention=vcfg.get("attention") or None, ref_image_size=vcfg.get("ref_image_size", "match"),
            seed=seed, prefix=f"aidrama/{seg.id}",
            allow_audio_damage=vcfg.get("preset") == "draft",   # 预演允许 4 步 LoRA 损伤对白音质
        )
        if mode == "fl2va":
            job.first_frame = self._upload(self.abs(plan.first_frame)) if plan.first_frame else None
            job.last_frame = self._upload(self.abs(plan.last_frame)) if plan.last_frame else None
        else:
            uploaded = {}
            for r, pth in zip(plan.refs, plan.paths):
                key = str(self.abs(pth))
                uploaded.setdefault(key, self._upload(self.abs(pth)))
                if r.kind == "image":
                    job.ref_images.append(uploaded[key])
                elif r.kind == "audio":
                    job.ref_audios.append(uploaded[key])
            job.anchor_first_frame = False
            for fidx, img in plan.guides or []:
                job.guides.append(Guide(frame_idx=fidx, image=uploaded.get(str(self.abs(img))) or self._upload(self.abs(img))))
            if vcfg.get("anchor_dialogue_audio") and seg.dialogue_track:
                job.guides.append(Guide(frame_idx=0, audio=uploaded.get(str(self.abs(seg.dialogue_track))) or self._upload(self.abs(seg.dialogue_track))))
        api = build_h3(job)
        name = out.stem
        if self.mock:
            self._check_graph(api, name)
            first = plan.first_frame or (plan.paths[0] if plan.paths else None)
            return placeholder_video(out, 384, 672, snap_length(seg.gen_seconds) / 24, name,
                                     audio=self.abs(seg.dialogue_track) if seg.dialogue_track else None,
                                     first_frame=self.abs(first) if first else None)
        try:
            return self._run(api, out.parent, out.stem, name)[0]
        except ComfyError as e:
            msg = str(e).lower()
            if job.attention and not job.fast and ("kitchen" in msg or "must be aligned" in msg) \
                    and "out of memory" not in msg:
                self.log("    ! INT8 注意力报错，改用 pytorch attention 重试")
                job.attention = None
                return self._run(build_h3(job), out.parent, out.stem, name)[0]
            raise

    def _seg_lines(self, ep: Episode, seg: Segment) -> list[tuple[str, float, float]]:
        smap = self.project.shot_map(ep)
        lines = []
        for sid, t0 in zip(seg.shots, seg.cut_times):
            for ln in smap[sid][1].dialogue:
                st = t0 + (ln.start or 0.0)
                lines.append((ln.plain, st, st + (ln.duration or 1.0)))
        return lines

    def _qc_dialogue(self, ep: Episode, seg: Segment, take: Take) -> None:
        lines = self._seg_lines(ep, seg)
        if not lines:
            return
        dia, c = QC.dialogue(self.audio, self.abs(take.path), lines, self.project.series.language,
                             self.cfg["audio"].get("max_cer", 0.15), work=self.dir / "episodes" / ep.id / "qc")
        issues = list(take.qc.get("issues", [])) + dia
        take.qc = {"pass": not any(QC.is_hard(i) for i in issues), "issues": issues, "cer": c}

    @staticmethod
    def _choose(seg: Segment, tech_only: bool = False) -> None:
        """自动选优：先看是否过质检，再看对白字错率；人工 pick 过的不动。改选后清掉旧的超分结果。"""
        if seg.picked_by_hand and seg.chosen is not None and seg.chosen < len(seg.takes):
            return
        if not seg.takes:
            return
        passing = [i for i, t in enumerate(seg.takes) if t.qc.get("pass")]
        if passing:
            best = passing[0] if tech_only else min(passing, key=lambda i: (seg.takes[i].qc.get("cer") or 0.0, i))
        else:
            best = seg.chosen if seg.chosen is not None and seg.chosen < len(seg.takes) else 0
        if best != seg.chosen:
            seg.chosen, seg.video_final = best, None

    def pick(self, ep_id: str, seg_id: str, take: int) -> None:
        seg = next(s for s in self.project.episode(ep_id).segments if s.id == seg_id)
        if not 1 <= take <= len(seg.takes):
            raise ValueError(f"{seg_id} 只有 {len(seg.takes)} 条")
        seg.chosen = take - 1
        seg.picked_by_hand = True
        seg.video_final = None
        self.save()

    def add_take(self, ep_id: str, seg_id: str, video: str | Path, pick: bool = True) -> Take:
        """登记一条在流水线外做的视频（InfiniteTalk / Wan Animate 2 / 付费 API 重做的关键镜头 / 实拍），
        同样做质检，默认直接选中；之后 upscale / assemble 照常处理（分辨率和帧率在合成时统一）。"""
        ep = self.project.episode(ep_id)
        seg = next((s for s in ep.segments if s.id == seg_id), None)
        if seg is None:
            raise ValueError(f"{ep_id} 没有生成段 {seg_id}（先运行 plan，段 id 见 project.yaml 或 review.html）")
        src = Path(video)
        if not src.exists():
            raise FileNotFoundError(src)
        vdir = self.dir / "episodes" / ep_id / "segments"
        vdir.mkdir(parents=True, exist_ok=True)
        dst = vdir / f"{seg.id}_{seg.sig[:6] or 'ext'}_x{self._next_take_no(seg, vdir)}.mp4"
        if src.suffix.lower() == ".mp4":
            shutil.copy(src, dst)
        else:       # mkv/mov/avi 等统一转成 mp4（ComfyUI 的 LoadVideo 和后续超分只认常见格式）
            try:
                ff.run(["-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)])
            except ff.FFmpegError:
                ff.run(["-i", str(src), "-c:v", "libx264", "-crf", "12", "-c:a", "aac", "-b:a", "320k", str(dst)])
        take = Take(path=self.rel(dst), seed=-1, preset="external")
        issues = QC.technical(dst, seg.planned, expect_audio=False)
        take.qc = {"pass": not any(QC.is_hard(i) for i in issues), "issues": issues, "cer": None}
        if self._seg_lines(ep, seg) and ff.has_audio(dst):
            self._gpu("audio")
            self._qc_dialogue(ep, seg, take)
        seg.takes.append(take)
        if pick:
            seg.chosen, seg.picked_by_hand, seg.video_final = len(seg.takes) - 1, True, None
        seg.status = "video"
        self.save()
        self.log(f"[add-take] {seg.id} take {len(seg.takes)} ← {src.name}" + ("（已选中）" if pick else "")
                 + (f"；质检：{take.qc['issues']}" if take.qc["issues"] else ""))
        return take

    # ------------------------------------------------------------------ 8. upscale
    def upscale(self, ep_id: str, force: bool = False) -> None:
        ucfg = self.cfg["upscale"]
        ep = self.project.episode(ep_id)
        udir = self.dir / "episodes" / ep_id / "final"
        self._family("upscale")
        for seg in ep.segments:
            if seg.chosen is None:
                continue
            if seg.video_final and not force and self.abs(seg.video_final).exists():
                continue
            src = self.abs(seg.takes[seg.chosen].path)
            out = udir / f"{seg.id}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            if not ucfg.get("enabled", True):
                shutil.copy(src, out)
            else:
                w, h = ff.video_size(src)
                scale = round(max(1920 / h, 1080 / w), 5)
                api = build_seedvr2(UpscaleJob(video=self._upload(src), scale=scale, model=ucfg.get("model", "7b"),
                                               color_correction=ucfg.get("color_correction", "lab"), prefix=f"aidrama/{seg.id}_up"))
                self.log(f"[upscale] {seg.id} ×{scale:.3f}（SeedVR2 {ucfg.get('model', '7b')}）")
                if self.mock:
                    self._check_graph(api, f"{seg.id}_upscale")
                    ff.run(["-i", str(src), "-vf", f"scale=-2:1920:flags=lanczos", "-c:v", "libx264", "-crf", "16", "-c:a", "copy", str(out)])
                else:
                    out = self._run(api, udir, seg.id, f"{seg.id}_upscale")[0]
            seg.video_final = self.rel(out)
            seg.status = "final"
            self.save()

    # ------------------------------------------------------------------ 9. music
    def music(self, ep_id: str, force: bool = False) -> None:
        ep = self.project.episode(ep_id)
        if self.cfg["music"].get("engine", "minimax_music3") == "none":
            return
        if ep.bgm and not force and self.abs(ep.bgm).exists():
            return
        total = sum(s.planned for s in ep.segments) or self.project.series.episode_seconds
        caption = ep.bgm_prompt or "Minimal cinematic drama underscore, soft piano and strings, slow tempo, no vocals."
        seconds = min(float(self.cfg["music"].get("seconds", 120)), total + 8)
        out = self.dir / "episodes" / ep_id / "audio" / "bgm.flac"
        api = build_music(MusicJob(caption=caption + " Instrumental only, no vocals.", seconds=seconds, seed=_seed(ep_id, "bgm"),
                                   prefix=f"aidrama/{ep_id}_bgm"))
        self.log(f"[music] {ep_id} 配乐 {seconds:.0f}s：{caption[:60]}")
        self._family("music")
        if self.mock:
            self._check_graph(api, f"{ep_id}_bgm")
            out = tone_wav(out.with_suffix(".wav"), seconds, 110.0)
        else:
            out = self._run(api, out.parent, "bgm", f"{ep_id}_bgm")[0]
        ep.bgm = self.rel(out)
        self.save()

    # ------------------------------------------------------------------ 10. assemble
    def assemble(self, ep_id: str) -> dict:
        p = self.project
        ep = p.episode(ep_id)
        pc = self.cfg["post"]
        smap = p.shot_map(ep)
        clips = []
        for seg in ep.segments:
            if seg.chosen is None:
                raise RuntimeError(f"{seg.id} 还没有可用的视频，请先运行 video")
            src = self.abs(seg.video_final) if seg.video_final else self.abs(seg.takes[seg.chosen].path)
            last = smap[seg.shots[-1]][1]
            cues = []
            for sid, t0 in zip(seg.shots, seg.cut_times):
                for ln in smap[sid][1].dialogue:
                    st = t0 + (ln.start or 0.0)
                    cues.append(Cue(st, st + (ln.duration or 1.5), ln.plain, ln.speaker))
            clips.append(Clip(str(src), seg.planned, last.transition, 0.5, cues, seg.id))
        opts = AssembleOptions(
            width=pc["width"], height=pc["height"], fps=pc["fps"], bgm=str(self.abs(ep.bgm)) if ep.bgm else None,
            bgm_db=pc.get("bgm_db", -16.0), target_lufs=pc["target_lufs"], burn_subtitles=pc["burn_subtitles"],
            subtitle_font=pc["subtitle_font"], subtitle_size=pc["subtitle_size"], subtitle_margin_v=pc["subtitle_margin_v"],
            fonts_dir=pc.get("fonts_dir") or _bundled_fonts(), ai_label=p.series.ai_label or pc.get("ai_label"),
            aigc_producer=pc.get("aigc_producer", "aidrama-local"),
        )
        self.log(f"[assemble] {ep_id}：{len(clips)} 段")
        res = assemble(clips, self.dir / "episodes" / ep_id / "out", ep_id, opts)
        ep.output = self.rel(res["final"])
        self.save()
        self.log(f"[assemble] 成片 {res['final']}（{res['duration']:.1f}s，{res['lufs']} LUFS）")
        return res

    # ------------------------------------------------------------------ all
    def run_episode(self, ep_id: str, takes: int | None = None) -> dict:
        ep = self.project.episode(ep_id)
        if not ep.scenes:
            self.storyboard(ep_id)
        self.cast()
        self.voice_lines(ep_id)
        self.plan(ep_id)
        self.keyframes(ep_id)
        self.video(ep_id, takes)
        self.upscale(ep_id)
        self.music(ep_id)
        res = self.assemble(ep_id)
        from .review import write_review
        write_review(self, ep_id)
        return res


def _bundled_fonts() -> str | None:
    """安装脚本会把思源黑体放到 <仓库>/fonts/；有就用它烧录字幕（保证不同电脑字幕字体一致）。"""
    d = Path(__file__).resolve().parent.parent / "fonts"
    return str(d) if any(d.glob("*.[ot]t[fc]")) else None


def _noun(gender: str) -> str:
    g = (gender or "").lower()
    if g in ("女", "f", "female", "woman", "girl"):
        return "woman"
    if g in ("男", "m", "male", "man", "boy"):
        return "man"
    return "person"
