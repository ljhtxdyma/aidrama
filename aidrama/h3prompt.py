"""H3 提示词编译器（本地版 “Context-IR”）。

MiniMax 官方 README：H3-Context-IR（把多模态输入改写成结构化提示词）对最终质量“至关重要”，但没有开源。
这里分两层替代：

1. compile_fl2va() / compile_ref2va() —— 确定性编译：时间戳、<Picture N>/<Audio N> 编号、(Sx)、<d> 台词、
   非说话者闭嘴声明、N/A 等全部由代码生成，保证结构永远合法。
2. refine() —— 让 LLM 以官方指南为系统提示，只扩写画面细节；输出必须通过 lint()，否则回退到确定性版本。

格式要点（官方 h3-prompt-writing 指南）：
  * 除对白和画面内文字外一律英文；台词写 <d>[Chinese] 原文</d>，逐字保留
  * 说话人 (S1)(S2)…；画外音 "says in an off-screen voiceover" + 嘴唇闭合
  * [Shot 1] 不带时间戳；后续 "[Shot N] At MM:SS.mmm, the camera cuts to …"，严格递增
  * 运镜 = 类型 + 幅度 + 速度
  * FL2VA：integrated_multimodal_description / overall_soundscape / non_diegetic_music
  * Ref2VA：subject_definitions / summary / retention_analysis / detailed_description /
            overall_soundscape / non_diegetic_music；图片、视频、音频各自按连接顺序编号
社区实测补充：画面里有不说话的正脸时要明写 "X does not speak; lips remain completely closed"，否则口型会跑到最显眼的脸上；
没有配乐要明写 N/A，否则模型会自己加音乐。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .llm import LLM

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
GUIDE_CACHE = Path.home() / ".cache" / "aidrama" / "h3-guides"
LANG_TAG = {"zh": "Chinese", "yue": "Cantonese", "en": "English", "ja": "Japanese", "ko": "Korean"}

SIZE_PHRASE = {
    "extreme-close-up": "an extreme close-up", "close-up": "a close-up", "medium-close-up": "a medium close-up",
    "medium": "a medium shot", "medium-wide": "a medium-wide shot", "full": "a full shot", "wide": "a wide shot",
    "establishing": "a wide establishing shot", "over-the-shoulder": "an over-the-shoulder shot",
}


@dataclass
class Speaker:
    char_id: str
    ref: str                     # 在正文里怎么称呼：Ref2VA 用 "<Subject 1>"，FL2VA 用通用身份短语
    voice_en: str = ""           # "a clear, mid-pitched young female voice"


@dataclass
class TimedLine:
    char_id: str
    text: str
    start: float                 # 相对段起点（秒）
    end: float
    delivery: str = ""
    voiceover: bool = False


@dataclass
class CutSpec:
    start: float
    duration: float
    visual: str                  # 英文，角色已替换为 <Subject N> 或通用身份
    camera: str = ""
    shot_size: str = ""
    lines: list[TimedLine] = field(default_factory=list)
    listeners: list[str] = field(default_factory=list)     # 画面内但本镜头不说话的角色 id
    end_state: str = ""
    sfx: str = ""
    keyframe_label: Optional[str] = None                   # "<Picture 2>"（Ref2VA 多镜头锚点）


@dataclass
class SegmentSpec:
    duration: float              # 送给模型的时长
    cuts: list[CutSpec]
    speakers: dict[str, Speaker]
    style: str = "Live-action, cinematic"
    lighting: str = ""
    sound: str = ""
    music: str = "N/A"
    language: str = "zh"

    @property
    def lines(self) -> list[TimedLine]:
        return [ln for c in self.cuts for ln in c.lines]


@dataclass
class RefItem:
    kind: str                    # image | audio | video
    role: str                    # first_frame | keyframe | identity | scene | voice | dialogue_track | motion
    char_id: Optional[str] = None
    cut: Optional[int] = None    # keyframe 属于第几个镜头（从 1 开始）
    desc: str = ""


def clip_text(text: str, limit: int = 240) -> str:
    """截断到 limit 字符以内，优先在句号/分号/逗号处断开，不切断单词。"""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i >= limit * 0.5:
            return cut[:i].rstrip(" ,;")
    return cut.rsplit(" ", 1)[0].rstrip(" ,;")


def _lc_first(s: str) -> str:
    """句中拼接时把普通单词的首字母改小写（不动 <Subject 1> 这类标签和缩写）。"""
    s = s.strip()
    if len(s) > 1 and s[0].isupper() and s[1].islower():
        return s[0].lower() + s[1:]
    return s


def ts(t: float) -> str:
    m = int(t // 60)
    return f"{m:02d}:{t - 60 * m:06.3f}"


def speaker_ids(spec: SegmentSpec) -> dict[str, str]:
    ids: dict[str, str] = {}
    for ln in sorted(spec.lines, key=lambda x: x.start):
        if ln.char_id not in ids:
            ids[ln.char_id] = f"S{len(ids) + 1}"
    return ids


def _cut_dialogue(cut: CutSpec, spec: SegmentSpec, ids: dict[str, str], introduced: set[str], dialogue_label: str | None) -> str:
    tag = LANG_TAG.get(spec.language, "Chinese")
    out = []
    for ln in sorted(cut.lines, key=lambda x: x.start):
        sp = spec.speakers.get(ln.char_id) or Speaker(ln.char_id, "the speaker")
        who = sp.ref
        if ln.char_id not in introduced and sp.voice_en:
            who = f"{sp.ref}, with {sp.voice_en},"
        introduced.add(ln.char_id)
        delivery = f" {ln.delivery.strip()}" if ln.delivery else ""
        timing = f"At about {ln.start:.1f} seconds, " if len(spec.lines) > 1 else ""
        if ln.voiceover:
            out.append(f"{timing}{who} ({ids[ln.char_id]}) says in an off-screen voiceover{delivery}: "
                       f"<d>[{tag}] {ln.text}</d> while the lips of everyone on screen remain completely closed.")
        else:
            out.append(f"{timing}{who} ({ids[ln.char_id]}) says{delivery}: <d>[{tag}] {ln.text}</d>")
    speaking = {ln.char_id for ln in cut.lines if not ln.voiceover}
    for cid in cut.listeners:
        if cid not in speaking:
            ref = spec.speakers[cid].ref if cid in spec.speakers else "the other person"
            out.append(f"{ref} does not speak; the lips remain completely closed.")
    if speaking:
        out.append("As soon as each line ends, the speaker's lips close and the jaw stops moving.")
        if dialogue_label:
            out.append(f"Every syllable and mouth movement follows {dialogue_label} exactly.")
    return " ".join(out)


def _cut_body(k: int, cut: CutSpec, spec: SegmentSpec, ids, introduced, dialogue_label=None, first_prefix="") -> str:
    if k == 1:
        head = f"[Shot 1] {first_prefix}"
    else:
        size = SIZE_PHRASE.get(cut.shot_size, "a new angle")
        anchor = f", whose keyframe corresponds to {cut.keyframe_label}" if cut.keyframe_label else ""
        head = f"[Shot {k}] At {ts(cut.start)}, the camera cuts to {size}{anchor}. "
    parts = [head + (_lc_first(cut.visual) if k == 1 and first_prefix else cut.visual.strip())]
    if cut.camera:
        parts.append(cut.camera.strip())
    dia = _cut_dialogue(cut, spec, ids, introduced, dialogue_label)
    if dia:
        parts.append(dia)
    if cut.end_state:
        parts.append(cut.end_state.strip())
    return " ".join(p for p in parts if p)


def _default_sound(spec: SegmentSpec) -> str:
    sfx = " ".join(c.sfx.strip() for c in spec.cuts if c.sfx.strip())
    base = spec.sound.strip() or "Quiet room tone continues throughout, with subtle fabric rustle and soft breathing."
    return (base + (" " + sfx if sfx else "")).strip()


def compile_fl2va(spec: SegmentSpec, first_frame: bool = True, last_frame: bool = False) -> str:
    """FL2VA / I2VA / T2VA 三段式（单镜头）。"""
    if len(spec.cuts) != 1:
        raise ValueError("FL2VA 编译只用于单镜头段，多镜头请用 Ref2VA")
    cut = spec.cuts[0]
    head = ""
    if first_frame and last_frame:
        head = ("How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the "
                f"0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the {spec.duration:.2f}-second "
                "mark of the target video.\n\n")
    elif first_frame:
        head = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    elif last_frame:
        head = ("How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with the "
                f"{spec.duration:.2f}-second mark of the target video.\n\n")
    prefix = f"{spec.style}, "
    if first_frame and not last_frame:
        prefix += "starting exactly from <Picture 1> and keeping its characters, costumes, set and lighting consistent, "
    elif first_frame and last_frame:
        prefix += "starting from the state established by Picture 1, "
    if spec.lighting:
        prefix += spec.lighting.strip().rstrip(".") + "; "
    ids = speaker_ids(spec)
    body = _cut_body(1, cut, spec, ids, set(), first_prefix=prefix)
    if last_frame:
        body += " At the end of the shot the composition settles into the pose, spacing and framing established by Picture 2."
    body += " No subtitles, captions or on-screen text appear."
    return (f"{head}integrated_multimodal_description: {body}\n\n"
            f"overall_soundscape: {_default_sound(spec)}\n\n"
            f"non_diegetic_music: {spec.music or 'N/A'}")


def compile_ref2va(spec: SegmentSpec, refs: list[RefItem], identity: dict[str, str]) -> str:
    """Ref2VA 六段式。refs 按节点连接顺序排列（图片/视频/音频各自编号）；
    identity: char_id → 英文外貌 + 服装锁定短语。"""
    labels: list[tuple[RefItem, str]] = []
    n = {"image": 0, "video": 0, "audio": 0}
    name = {"image": "Picture", "video": "Video", "audio": "Audio"}
    for r in refs:
        n[r.kind] += 1
        labels.append((r, f"<{name[r.kind]} {n[r.kind]}>"))
    ids = speaker_ids(spec)
    subj = {cid: sp.ref for cid, sp in spec.speakers.items()}   # char_id -> "<Subject N>"

    defs, ret = [], []
    for cid, sref in subj.items():
        pics = [lab for r, lab in labels if r.role == "identity" and r.char_id == cid]
        desc = identity.get(cid, "").strip().rstrip(".")
        src = f" in {' and '.join(pics)}" if pics else ""
        sid = f" ({ids[cid]})" if cid in ids else ""
        defs.append(f"{sref}{sid} is the person{src}" + (f", {desc}." if desc else "."))
        ret.append(f"{sref} (appears in the shots where visible): fully_preserved - facial identity, hairstyle, skin tone, "
                   "body shape and costume are retained exactly; pose and expression follow each shot.")
    dialogue_label = None
    for r, lab in labels:
        if r.role in ("first_frame", "keyframe"):
            what = "the first frame of [Shot 1]" if (r.role == "first_frame" or r.cut == 1) else f"the keyframe of [Shot {r.cut}]"
            defs.append(f"{lab} is {what}" + (f", showing {_lc_first(r.desc).rstrip('.')}." if r.desc else "."))
            ret.append(f"{lab} ([Shot {r.cut or 1}] {'first frame' if (r.cut or 1) == 1 else 'keyframe'}): fully_preserved - "
                       "framing, subject positions, costumes and lighting are matched exactly at that moment.")
        elif r.role == "scene":
            defs.append(f"{lab} is the location reference" + (f", {r.desc.strip().rstrip('.')}." if r.desc else "."))
            ret.append(f"{lab} (location): partially_preserved - layout, palette and lighting mood are kept.")
        elif r.role == "voice" and r.char_id:
            sref = subj.get(r.char_id, "the speaker")
            sid = f" ({ids[r.char_id]})" if r.char_id in ids else ""
            defs.append(f"{lab} is the voice-timbre reference for {sref}{sid}.")
            ret.append(f"{lab}: reference - the voice timbre and delivery of {lab} are followed without copying its words.")
        elif r.role == "dialogue_track":
            dialogue_label = lab
            who = ", ".join(f"{subj.get(c, 'the speaker')} ({s})" for c, s in ids.items())
            defs.append(f"{lab} is the pre-recorded dialogue track of the target video, containing the spoken lines of {who}.")
            ret.append(f"{lab}: partially_copy - every spoken word of {lab} is reused verbatim and in sync as the dialogue, "
                       "while room tone and physical sounds are added around it.")
        elif r.role == "motion":
            defs.append(f"{lab} is the motion reference" + (f": {r.desc.strip().rstrip('.')}." if r.desc else "."))
            ret.append(f"{lab} (body motion): weak_reference - only the motion and timing are followed.")

    tasks = []
    if any(r.role in ("first_frame", "keyframe") for r in refs):
        tasks.append("keyframe completion")
    if subj:
        tasks.append("reference generation")
    if dialogue_label:
        tasks.append("audio reuse")
    if any(r.role == "voice" for r in refs):
        tasks.append("audio reference")
    first_kf = next((lab for r, lab in labels if r.role in ("first_frame", "keyframe") and (r.cut or 1) == 1), None)
    who = " and ".join(subj.values()) or "the characters"
    summary = f"[{' + '.join(tasks) or 'reference generation'}] The target video is a {len(spec.cuts)}-shot scene with {who}."
    if first_kf:
        summary += f" It begins exactly from {first_kf}"
        kfs = [lab for r, lab in labels if r.role == "keyframe" and (r.cut or 1) > 1]
        summary += (f" and cuts to {', '.join(kfs)} at the planned times." if kfs else ".")
    if dialogue_label:
        summary += f" {dialogue_label} supplies the exact dialogue, and the speakers' lips are synchronized to it."

    style = spec.style.strip().rstrip(".")
    opening = f"The target video is in a {style.lower()} style"
    opening += (f", {spec.lighting.strip().rstrip('.')}." if spec.lighting else ".")
    introduced: set[str] = set()
    bodies = []
    for k, cut in enumerate(spec.cuts, 1):
        prefix = f"The shot begins from {first_kf}. " if (k == 1 and first_kf) else ""
        bodies.append(_cut_body(k, cut, spec, ids, introduced, dialogue_label, first_prefix=prefix))
    detailed = opening + "\n" + "\n".join(bodies) + "\nNo subtitles, captions or on-screen text appear at any time."
    return ("subject_definitions:\n" + "\n".join(defs) + "\n\n"
            "summary:\n" + summary + "\n\n"
            "retention_analysis:\n" + "\n".join(ret) + "\n\n"
            "detailed_description:\n" + detailed + "\n\n"
            "overall_soundscape:\n" + _default_sound(spec) + "\n\n"
            "non_diegetic_music:\n" + (spec.music or "N/A"))


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

FL2VA_SECTIONS = ["integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"]
REF_SECTIONS = ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:",
                "overall_soundscape:", "non_diegetic_music:"]


def lint(prompt: str, spec: SegmentSpec, mode: str, n_images: int = 0, n_audios: int = 0, n_videos: int = 0) -> list[str]:
    errs = []
    secs = REF_SECTIONS if mode == "ref2va" else FL2VA_SECTIONS
    pos = [prompt.find(s) for s in secs]
    if any(p < 0 for p in pos):
        errs.append("缺少必需字段: " + ", ".join(s for s, p in zip(secs, pos) if p < 0))
    elif pos != sorted(pos):
        errs.append("字段顺序不对")
    if "[Shot 1]" not in prompt:
        errs.append("缺少 [Shot 1]")
    if re.search(r"\[Shot 1\]\s*At\s+\d", prompt):
        errs.append("[Shot 1] 不应带时间戳")
    shots = re.findall(r"\[Shot (\d+)\]", prompt)
    uniq = list(dict.fromkeys(int(s) for s in shots))
    if uniq != list(range(1, len(uniq) + 1)):
        errs.append(f"镜头编号不连续: {uniq}")
    if len(uniq) != len(spec.cuts):
        errs.append(f"镜头数 {len(uniq)} 与计划 {len(spec.cuts)} 不一致")
    times = [float(m.group(1)) * 60 + float(m.group(2)) for m in re.finditer(r"\[Shot \d+\]\s*At\s+(\d+):(\d+(?:\.\d+)?)", prompt)]
    if times != sorted(times) or any(t >= spec.duration for t in times):
        errs.append(f"切镜时间必须递增且小于时长 {spec.duration:.2f}s: {times}")
    tag = LANG_TAG.get(spec.language, "Chinese")
    found = re.findall(r"<d>\[" + tag + r"\] (.*?)</d>", prompt, flags=re.S)
    want = [ln.text for ln in sorted(spec.lines, key=lambda x: x.start)]
    if found != want:
        errs.append(f"台词必须逐字、按顺序写在 <d>[{tag}] …</d> 中：期望 {want}，实际 {found}")
    for kind, nmax in (("Picture", n_images), ("Audio", n_audios), ("Video", n_videos)):
        for m in re.finditer(rf"<{kind} (\d+)>", prompt):
            if int(m.group(1)) > nmax:
                errs.append(f"引用了不存在的 <{kind} {m.group(1)}>（只连接了 {nmax} 个）")
    if mode != "ref2va" and re.search(r"<(Subject|Audio|Video) \d+>", prompt):
        errs.append("FL2VA 模式不能使用 <Subject>/<Audio>/<Video> 标签")
    outside = re.sub(r"<d>.*?</d>", "", prompt, flags=re.S)
    outside = re.sub(r'"[^"]*"', "", outside)
    if re.search(r"[一-鿿]{2,}", outside):
        errs.append("对白和画面文字以外出现了中文（描述部分必须写英文）")
    if re.search(r"\[[a-z][a-z0-9_]*\]", outside.replace("[Shot", "").replace("[Chinese]", "")):
        errs.append("还有未替换的 [角色id] 占位符")
    m = re.search(r"non_diegetic_music:\s*(.+)", prompt)
    if not m or not m.group(1).strip():
        errs.append("non_diegetic_music 为空（没有配乐要写 N/A）")
    return errs


# ---------------------------------------------------------------------------
# LLM refine（本地 Context-IR）
# ---------------------------------------------------------------------------

def guide_text(mode: str) -> str:
    parts = [(PROMPTS_DIR / "h3_rewriter_system.md").read_text(encoding="utf-8")]
    files = ["base-en.txt"] + (["ref-en.txt"] if mode == "ref2va" else [])
    for f in files:
        p = GUIDE_CACHE / f
        if p.exists():
            parts.append(f"\n\n# Official MiniMax H3 prompt writing guide: {f}\n\n" + p.read_text(encoding="utf-8"))
    return "".join(parts)


def refine(llm: LLM, draft: str, spec: SegmentSpec, mode: str, context: dict,
           n_images=0, n_audios=0, n_videos=0) -> tuple[str, list[str]]:
    """LLM 只扩写画面/表演/声音细节；结构与台词必须保持。返回 (最终提示词, 问题列表)。"""
    system = guide_text(mode)
    base_user = (
        f"Mode: {'Ref2VA (six sections)' if mode == 'ref2va' else 'FL2VA / I2VA (three sections)'}\n"
        f"Duration: {spec.duration:.2f} s with {len(spec.cuts)} shot(s); keep every [Shot N] header and timestamp exactly.\n"
        f"Connected references: {n_images} picture(s), {n_audios} audio clip(s), {n_videos} video clip(s); "
        "keep every reference label exactly as in the draft and do not invent new ones.\n"
        "Keep every <d>[…] …</d> line character-for-character, in the same order, and keep every 'does not speak' sentence.\n"
        f"Shot context (JSON, for detail only):\n{json.dumps(context, ensure_ascii=False, indent=1)}\n\n"
        f"Structurally valid draft:\n<<<\n{draft}\n>>>\n\n"
        "Return only the final prompt text."
    )
    errs: list[str] = []
    for _ in range(2):
        user = base_user if not errs else base_user + "\n\nYour previous answer violated these rules: " + "; ".join(errs)
        try:
            out = llm.text(system, user).strip().strip("`").strip()
        except Exception as e:  # noqa: BLE001
            return draft, [f"LLM 改写失败，使用确定性版本: {e}"]
        errs = lint(out, spec, mode, n_images, n_audios, n_videos)
        if not errs:
            return out, []
    return draft, ["LLM 改写未通过校验，使用确定性版本: " + "; ".join(errs)]
