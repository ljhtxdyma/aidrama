"""工程数据模型：剧集 → 集 → 场 → 镜头（cut），以及按 H3 一次生成的“段”（segment）。

为什么要有“段”：MiniMax H3 一次生成 4~15 秒，可以在一次生成里按 [Shot k] At MM:SS 切镜，
并用 MiniMaxH3AddGuide 把每个镜头的关键帧钉在切点上。社区实测这样切点准确、生成次数只有逐镜生成的约 1/3，
而且同一段里人物的光线、音色天然连续。所以：分镜按“镜头”写，生成按“段”跑。

所有中间产物都登记在 project.yaml 里，任何一步都可以单独重跑、断点续跑。
英文字段（*_en、keyframe_prompt、motion_prompt …）里用 [角色id] 指代角色，例如
"[lin_wan] grips the strap of her bag"，编排器会按模式替换成 <image1> / <Subject 1> / 通用身份描述。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

ShotMethod = Literal[
    "auto",       # 默认：编排器自动决定（对白/多镜头 → H3 Ref2VA 段；单镜无对白 → H3 FL2VA）
    "flf2v",      # 首尾帧：精确控制落幅（单独成段，H3 FL2VA 两张图）
    "t2v",        # 纯文生：空镜/氛围（单独成段）
    "continue",   # 接上一段的尾帧续拍（长镜头）
    "animate",    # 真人表演/动作视频迁移到 AI 角色（Wan Animate 2，需要 driving_video）
]


class Voice(BaseModel):
    ref_audio: Optional[str] = None      # 10~15 秒干净参考音频；为空则按 description 自动设计
    ref_text: Optional[str] = None
    description: str = ""               # 中文音色描述（Qwen3-TTS VoiceDesign 指令）
    voice_en: str = ""                  # 英文音色描述（写进 H3 提示词）
    design_text: str = ""               # 设计音色时朗读的样本台词（10~15 秒）
    emotion_refs: dict[str, str] = Field(default_factory=dict)  # {"cry": "...wav"} 情绪参考音频（可选）


class Character(BaseModel):
    id: str
    name: str
    gender: str = ""
    age: str = ""
    appearance: str = ""                 # 中文外貌描述（给人看）
    identity_en: str = ""                # 英文通用身份短语："a slender woman in her mid-20s with a low black ponytail"
    appearance_en: str = ""              # 英文详细外貌（定妆照提示词）
    outfits: dict[str, str] = Field(default_factory=lambda: {"default": ""})  # 服装 key → 英文“连续性锁”短语
    personality: str = ""
    voice: Voice = Field(default_factory=Voice)
    refs: dict[str, str] = Field(default_factory=dict)   # front / full / <outfit>_full …


class Location(BaseModel):
    id: str
    name: str
    description: str = ""
    description_en: str = ""
    refs: dict[str, str] = Field(default_factory=dict)   # plate / plate_night …


_PRON = re.compile(r"<([^<>|]+)\|[^<>]*>")


def plain_text(text: str) -> str:
    """去掉 IndexTTS-2.5 读音标注：'银<行|HANG2>' → '银行'（字幕、H3 提示词、ASR 质检都用纯文本）。"""
    return _PRON.sub(r"\1", text or "")


class Line(BaseModel):
    speaker: str                         # 角色 id
    text: str                            # 台词原文（中文）。多音字可写 IndexTTS 读音标注：银<行|HANG2>，只影响配音
    emotion: str = "neutral"             # neutral/happy/sad/angry/fear/surprise/disgust/contempt/whisper/cry …
    delivery: str = ""                   # 英文表演提示，如 "through clenched teeth"
    voiceover: bool = False              # 画外音（画面里嘴不动）
    emo_vector: Optional[list[float]] = None   # IndexTTS 8 维情绪向量（可选，留空按 emotion 推断）
    audio: Optional[str] = None          # 生成的配音
    duration: Optional[float] = None
    start: Optional[float] = None        # 在本镜头内的起始时间（秒）

    @property
    def plain(self) -> str:
        return plain_text(self.text)


class Shot(BaseModel):
    id: str
    duration: float = 3.0                # 规划时长（秒）；有对白时编排器会按配音实际长度自动加长
    shot_size: str = "medium"            # extreme-close-up/close-up/medium-close-up/medium/medium-wide/full/wide/establishing
    angle: str = "eye-level"
    camera_en: str = "The camera holds a static shot."   # H3 运镜：类型 + 幅度 + 速度
    characters: list[str] = Field(default_factory=list)   # 画面内角色
    outfit: dict[str, str] = Field(default_factory=dict) # 角色 id → 服装 key
    action: str = ""                     # 中文：画面内发生了什么（给人看）
    keyframe_prompt: str = ""            # 英文：首帧静帧（构图/站位/表情/光线）
    end_keyframe_prompt: str = ""        # 英文：尾帧（method=flf2v）
    motion_prompt: str = ""              # 英文：起点 → 唯一动作 → 终点
    end_state: str = ""
    dialogue: list[Line] = Field(default_factory=list)
    sfx: str = ""                        # 英文：本镜头的拟音
    method: ShotMethod = "auto"
    driving_video: Optional[str] = None
    transition: Literal["cut", "fade", "dissolve", "flash"] = "cut"   # 进入下一个镜头的方式
    seed: Optional[int] = None
    # 产物
    keyframe: Optional[str] = None
    end_keyframe: Optional[str] = None
    segment: Optional[str] = None        # 所属生成段
    status: str = "todo"
    notes: str = ""


class RefRecord(BaseModel):
    kind: Literal["image", "audio", "video"]
    path: str
    role: str
    char_id: Optional[str] = None
    desc: str = ""


class Take(BaseModel):
    path: str
    seed: int
    preset: str
    seconds: float = 0.0                 # 生成耗时
    qc: dict = Field(default_factory=dict)


class Segment(BaseModel):
    id: str
    scene: str
    shots: list[str]                     # shot id 列表（按顺序）
    engine: Literal["h3_ref2va", "h3_fl2va", "wan_animate"] = "h3_ref2va"
    planned: float = 0.0                 # 规划总时长（成片里用的长度）
    gen_seconds: float = 0.0             # 送给模型的时长（≥4s）
    cut_times: list[float] = Field(default_factory=list)   # 每个镜头在段内的起点
    dialogue_track: Optional[str] = None
    refs: list[RefRecord] = Field(default_factory=list)
    prompt: Optional[str] = None         # 实际使用的 H3 提示词（Regenerate-2K 需原样提交）
    prompt_issues: list[str] = Field(default_factory=list)
    takes: list[Take] = Field(default_factory=list)
    chosen: Optional[int] = None         # 选中的 take 下标
    picked_by_hand: bool = False         # 人工 pick 过（或已被续写镜头接上）：之后补抽不再自动改选
    sig: str = ""                        # 内容签名（镜头文字/台词/关键帧/时长）；变了就说明旧视频已过期
    video_final: Optional[str] = None    # 超分后
    status: str = "todo"


class Scene(BaseModel):
    id: str
    location: str = ""
    time_of_day: str = "day"
    mood: str = ""
    summary: str = ""
    sound_en: str = ""                   # 英文：本场环境声底（room tone、雨声…）
    lighting_en: str = ""                # 英文：本场光线基调
    shots: list[Shot] = Field(default_factory=list)


class Episode(BaseModel):
    id: str
    title: str = ""
    synopsis: str = ""
    script: str = ""
    hook: str = ""
    cliffhanger: str = ""
    scenes: list[Scene] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    bgm_prompt: str = ""                 # 英文配乐描述（MiniMax Music 3 caption）
    bgm: Optional[str] = None
    output: Optional[str] = None


class Series(BaseModel):
    title: str
    logline: str = ""
    genre: str = ""
    style: Literal["realistic", "anime", "3d"] = "realistic"
    style_en: str = "Live-action, cinematic"              # H3 风格开头
    image_style_en: str = "photorealistic live-action film still, natural skin texture, cinematic lighting, shallow depth of field"
    language: str = "zh"
    episode_seconds: int = 90
    ai_label: str = "本内容由AI生成"


class Project(BaseModel):
    series: Series
    characters: list[Character] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    episodes: list[Episode] = Field(default_factory=list)

    # ---------------------------------------------------------------- helpers
    def character(self, cid: str) -> Character:
        for c in self.characters:
            if c.id == cid:
                return c
        raise KeyError(f"unknown character '{cid}'")

    def location(self, lid: str) -> Optional[Location]:
        for loc in self.locations:
            if loc.id == lid:
                return loc
        return None

    def episode(self, eid: str) -> Episode:
        for e in self.episodes:
            if e.id == eid:
                return e
        raise KeyError(f"unknown episode '{eid}'")

    def iter_shots(self, eid: str | None = None):
        for ep in self.episodes:
            if eid and ep.id != eid:
                continue
            for sc in ep.scenes:
                for sh in sc.shots:
                    yield ep, sc, sh

    def shot_map(self, ep: Episode) -> dict[str, tuple[Scene, Shot]]:
        return {sh.id: (sc, sh) for sc in ep.scenes for sh in sc.shots}

    def check(self) -> list[str]:
        problems = []
        cids = {c.id for c in self.characters}
        lids = {loc.id for loc in self.locations}
        for c in self.characters:
            if not c.identity_en:
                problems.append(f"角色 {c.id} 缺少 identity_en")
        seen = set()
        for ep, sc, sh in self.iter_shots():
            key = f"{ep.id}/{sh.id}"
            if key in seen:
                problems.append(f"重复的镜头 id {key}")
            seen.add(key)
            if sc.location and sc.location not in lids:
                problems.append(f"{key}: 未定义的场景 '{sc.location}'")
            for c in sh.characters:
                if c not in cids:
                    problems.append(f"{key}: 未定义的角色 '{c}'")
            for ln in sh.dialogue:
                if ln.speaker not in cids:
                    problems.append(f"{key}: 未定义的说话人 '{ln.speaker}'")
                if not ln.voiceover and ln.speaker not in sh.characters:
                    problems.append(f"{key}: 说话人 {ln.speaker} 不在画面内，请设 voiceover: true 或加入 characters")
            if len(sh.characters) > 3:
                problems.append(f"{key}: 同框超过 3 人，AI 容易串脸")
            if sh.method == "animate" and not sh.driving_video:
                problems.append(f"{key}: method=animate 需要 driving_video")
            if not (1.0 <= sh.duration <= 15.0):
                problems.append(f"{key}: 时长 {sh.duration}s 超出 1~15s")
        return problems

    # ---------------------------------------------------------------- io
    @classmethod
    def load(cls, path: str | Path) -> "Project":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.model_dump(mode="json"), f, allow_unicode=True, sort_keys=False, width=120)
        tmp.replace(path)
