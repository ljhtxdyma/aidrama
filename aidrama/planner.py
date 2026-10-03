"""把分镜（镜头）规划成 H3 生成段（segment），并为每段编译 H3 提示词与参考素材清单。

分段规则（来自社区实测与官方模板）：
  * 同一场、同一连续时空的相邻镜头合并为一段，每段 ≤ max_segment_seconds（默认 12s，模型上限 15s）、≤ 4 个镜头
  * 每个镜头的关键帧用 MiniMaxH3AddGuide 钉在切点 frame_idx = round(切点秒 × 24)
  * 有对白 → H3 Ref2VA：参考图 = 各镜关键帧 + 出场角色设定图；参考音频 = 本段对白音轨（partially_copy）
  * 单镜头且无对白 → H3 FL2VA I2V（只用关键帧作首帧；FL2VA 的原始画质高于 Ref2VA）
  * flf2v / t2v / continue / animate → 单独成段
  * 同段参考图总数 ≤ 9；同段出场角色 ≤ 3
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .h3prompt import CutSpec, RefItem, SegmentSpec, Speaker, TimedLine, clip_text
from .schema import Episode, Project, RefRecord, Scene, Segment, Shot

TOKEN = re.compile(r"\[([a-z][a-z0-9_]*)\]")
CHARS_PER_SEC = 3.6     # 中文语速估算（没有配音时用）


def estimate_line_seconds(text: str) -> float:
    n = len(re.sub(r"[\s，。！？、,.!?…“”\"'：:；;]", "", text))
    return max(0.8, n / CHARS_PER_SEC + 0.2)


def substitute(text: str, mapping: dict[str, str]) -> str:
    """把 [角色id] 换成给定称呼；未知 id 保留原样（lint 会报出来）。"""
    return TOKEN.sub(lambda m: mapping.get(m.group(1), m.group(0)), text or "")


def shot_timing(sh: Shot, audio_cfg: dict) -> float:
    """按台词实际长度排好每句的起点，返回镜头时长（≥ 规划时长）。"""
    lead, gap, tail = audio_cfg.get("lead_in", 0.35), audio_cfg.get("line_gap", 0.25), audio_cfg.get("tail", 0.45)
    t = lead
    for ln in sh.dialogue:
        d = ln.duration or estimate_line_seconds(ln.plain)
        ln.start = round(t, 3)
        t += d + gap
    need = (t - gap + tail) if sh.dialogue else 0.0
    return round(max(sh.duration, need), 3)


def plan_segments(project: Project, ep: Episode, cfg: dict) -> list[Segment]:
    vcfg, acfg = cfg.get("video", {}), cfg.get("audio", {})
    max_sec = float(vcfg.get("max_segment_seconds", 12.0))
    max_cuts = int(vcfg.get("max_cuts_per_segment", 4))
    segs: list[Segment] = []

    def new_seg(sc: Scene, engine: str) -> Segment:
        seg = Segment(id=f"{ep.id}_g{len(segs) + 1:02d}", scene=sc.id, shots=[], engine=engine)
        segs.append(seg)
        return seg

    for sc in ep.scenes:
        cur: Segment | None = None
        cur_chars: set[str] = set()
        prev_transition = "cut"
        for sh in sc.shots:
            dur = shot_timing(sh, acfg)
            special = sh.method in ("flf2v", "t2v", "continue", "animate")
            fits = (
                cur is not None and not special and cur.engine == "h3_ref2va"
                and prev_transition == "cut"
                and cur.planned + dur <= max_sec + 1e-6
                and len(cur.shots) < max_cuts
                and len(cur_chars | set(sh.characters)) <= 3
                and not any(project.shot_map(ep)[s][1].method != "auto" for s in cur.shots)
            )
            if not fits:
                engine = "wan_animate" if sh.method == "animate" else "h3_ref2va"
                cur = new_seg(sc, engine)
                cur_chars = set()
            cur.cut_times.append(round(cur.planned, 3))
            cur.shots.append(sh.id)
            cur.planned = round(cur.planned + dur, 3)
            cur_chars |= set(sh.characters)
            sh.segment = cur.id
            prev_transition = sh.transition

    # 引擎最终确定：单镜头无对白（或首尾帧/空镜/续拍）→ FL2VA
    smap = project.shot_map(ep)
    for seg in segs:
        shots = [smap[s][1] for s in seg.shots]
        has_dialogue = any(sh.dialogue for sh in shots)
        if seg.engine != "wan_animate" and len(shots) == 1 and (not has_dialogue or shots[0].method in ("flf2v", "t2v", "continue")):
            seg.engine = "h3_fl2va"
        seg.gen_seconds = round(min(max(seg.planned, 5.0), float(vcfg.get("max_seconds", 15.0)), 15.0), 3)   # H3 训练时长约 124~362 帧（5.1~15 秒）
    return segs


@dataclass
class SegmentPlan:
    spec: SegmentSpec
    refs: list[RefItem]
    paths: list[str]            # 与 refs 一一对应的本地文件
    identity: dict[str, str]
    mode: str                   # fl2va | ref2va
    first_frame: str | None = None
    last_frame: str | None = None
    guides: list[tuple[int, str]] | None = None   # (frame_idx, 本地图片)


def _identity_en(project: Project, cid: str, outfit_key: str | None) -> str:
    c = project.character(cid)
    outfit = c.outfits.get(outfit_key or "default") or next(iter(c.outfits.values()), "")
    return f"{c.identity_en}" + (f", wearing {outfit}" if outfit else "")


def build_plan(project: Project, ep: Episode, seg: Segment, cfg: dict, dialogue_track: str | None,
               prev_last_frame: str | None = None) -> SegmentPlan:
    smap = project.shot_map(ep)
    shots = [smap[s][1] for s in seg.shots]
    scene = smap[seg.shots[0]][0]
    vcfg = cfg.get("video", {})
    native = vcfg.get("dialogue_engine", "ref2va_audio") == "native"
    mode = "ref2va" if seg.engine == "h3_ref2va" else "fl2va"

    chars: list[str] = []
    outfits: dict[str, str] = {}
    for sh in shots:
        for c in sh.characters:
            if c not in chars:
                chars.append(c)
            outfits.setdefault(c, sh.outfit.get(c, "default"))
        for ln in sh.dialogue:
            if ln.voiceover and ln.speaker not in chars:
                chars.append(ln.speaker)

    identity = {c: _identity_en(project, c, outfits.get(c)) for c in chars}
    # 不在本段画面里的角色（例如提示词里写了“[gu_chen] 空着的办公室”）用身份短语替换，不能留下 [id]
    names = {c.id: c.identity_en for c in project.characters}
    if mode == "ref2va":
        names.update({c: f"<Subject {i + 1}>" for i, c in enumerate(chars)})
    else:
        names.update({c: identity[c] if i == 0 else project.character(c).identity_en for i, c in enumerate(chars)})
    speakers = {c: Speaker(c, names[c], project.character(c).voice.voice_en) for c in chars}

    cuts = []
    for k, (sh, t0) in enumerate(zip(shots, seg.cut_times), 1):
        lines = [TimedLine(ln.speaker, ln.plain, t0 + (ln.start or 0.0), t0 + (ln.start or 0.0) + (ln.duration or estimate_line_seconds(ln.plain)),
                           ln.delivery, ln.voiceover) for ln in sh.dialogue]
        speaking = {ln.speaker for ln in sh.dialogue}
        cuts.append(CutSpec(
            start=t0, duration=(seg.cut_times[k] if k < len(shots) else seg.planned) - t0,
            visual=substitute(sh.motion_prompt or sh.keyframe_prompt, names),
            camera=sh.camera_en, shot_size=sh.shot_size, lines=lines,
            listeners=[c for c in sh.characters if c not in speaking], characters=list(sh.characters),
            end_state=substitute(sh.end_state, names), sfx=sh.sfx,
            keyframe_label=None,
        ))
    spec = SegmentSpec(duration=seg.gen_seconds, cuts=cuts, speakers=speakers if mode == "ref2va" else
                       {c: Speaker(c, names[c], speakers[c].voice_en) for c in chars},
                       style=project.series.style_en, lighting=scene.lighting_en, sound=scene.sound_en,
                       music="N/A", language=project.series.language)

    refs: list[RefItem] = []
    paths: list[str] = []
    guides: list[tuple[int, str]] = []
    plan = SegmentPlan(spec, refs, paths, identity, mode)
    if mode == "fl2va":
        sh = shots[0]
        if sh.method == "continue" and prev_last_frame:
            plan.first_frame = prev_last_frame
        elif sh.method != "t2v" and sh.keyframe:
            plan.first_frame = sh.keyframe
        if sh.method == "flf2v" and sh.end_keyframe:
            plan.last_frame = sh.end_keyframe
        return plan

    # Ref2VA：图片 = 各镜关键帧（Picture 1..k）+ 角色身份图；音频 = 对白音轨
    for k, sh in enumerate(shots, 1):
        if sh.keyframe:
            refs.append(RefItem("image", "first_frame" if k == 1 else "keyframe", cut=k,
                                desc=clip_text(substitute(sh.keyframe_prompt, names), 240)))
            paths.append(sh.keyframe)
            if k > 1:   # 官方多帧模板：第 1 张只作参考图（提示词写“从 <Picture 1> 开始”），后面的切点才用 AddGuide 钉帧
                guides.append((round(seg.cut_times[k - 1] * 24), sh.keyframe))
            cuts[k - 1].keyframe_label = f"<Picture {sum(1 for r in refs if r.kind == 'image')}>"
    for c in chars:
        ch = project.character(c)
        for key in (f"sheet_{outfits.get(c, 'default')}", "sheet_default", "front"):
            p = ch.refs.get(key)
            if p and sum(1 for r in refs if r.kind == "image") < 9:
                refs.append(RefItem("image", "identity", char_id=c))
                paths.append(p)
                break
    if native:
        for c in chars:
            v = project.character(c).voice.ref_audio
            if v and any(ln.char_id == c for ln in spec.lines) and sum(1 for r in refs if r.kind == "audio") < 3:
                refs.append(RefItem("audio", "voice", char_id=c))
                paths.append(v)
    elif dialogue_track:
        refs.append(RefItem("audio", "dialogue_track"))
        paths.append(dialogue_track)
    plan.guides = guides
    return plan


def to_records(plan: SegmentPlan) -> list[RefRecord]:
    return [RefRecord(kind=r.kind, path=p, role=r.role, char_id=r.char_id, desc=r.desc) for r, p in zip(plan.refs, plan.paths)]
