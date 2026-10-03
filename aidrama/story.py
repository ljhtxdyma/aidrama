"""LLM 写剧集圣经与分镜剧本。输出经过 pydantic 校验 + 项目一致性检查，不合格就把错误回喂给 LLM 修复。"""
from __future__ import annotations

import json
from pathlib import Path

from .llm import LLM
from .schema import Character, Episode, Location, Project, Scene, Series, Shot

PROMPTS = Path(__file__).resolve().parent / "prompts"


def _system(name: str, **kw) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def make_bible(llm: LLM, idea: str, episodes: int = 3, episode_seconds: int = 90, extra: str = "") -> Project:
    system = _system("bible_system.md", episode_seconds=episode_seconds)
    user = f"创意：{idea}\n集数：{episodes}\n单集时长：约 {episode_seconds} 秒\n{extra}".strip()

    def validate(d):
        p = _bible_to_project(d, episode_seconds)
        ids = [c.id for c in p.characters]
        if len(ids) != len(set(ids)):
            raise ValueError("角色 id 重复")
        if len(p.episodes) < 1:
            raise ValueError("没有分集")
        for c in p.characters:
            if not c.identity_en or not c.appearance_en or not c.voice.description:
                raise ValueError(f"角色 {c.id} 缺少 identity_en / appearance_en / voice.description")

    data = llm.json_call(system, user, validate=validate)
    return _bible_to_project(data, episode_seconds)


def _bible_to_project(d: dict, episode_seconds: int) -> Project:
    s = d.get("series", {})
    series = Series(title=s.get("title", "未命名"), logline=s.get("logline", ""), genre=s.get("genre", ""),
                    style_en=s.get("style_en") or "Live-action, cinematic",
                    image_style_en=s.get("image_style_en") or Series.model_fields["image_style_en"].default,
                    episode_seconds=episode_seconds)
    chars = [Character.model_validate(c) for c in d.get("characters", [])]
    locs = [Location.model_validate(x) for x in d.get("locations", [])]
    eps = [Episode.model_validate(e) for e in d.get("episodes", [])]
    return Project(series=series, characters=chars, locations=locs, episodes=eps)


def bible_digest(p: Project) -> str:
    """喂给分镜 LLM 的圣经摘要。"""
    return json.dumps({
        "series": {"title": p.series.title, "logline": p.series.logline, "genre": p.series.genre},
        "characters": [{"id": c.id, "name": c.name, "gender": c.gender, "age": c.age, "personality": c.personality,
                        "identity_en": c.identity_en, "outfits": list(c.outfits)} for c in p.characters],
        "locations": [{"id": x.id, "name": x.name, "description_en": x.description_en} for x in p.locations],
        "episodes": [{"id": e.id, "title": e.title, "synopsis": e.synopsis, "hook": e.hook, "cliffhanger": e.cliffhanger}
                     for e in p.episodes],
    }, ensure_ascii=False, indent=1)


def make_storyboard(llm: LLM, project: Project, ep_id: str, notes: str = "") -> Episode:
    ep = project.episode(ep_id)
    system = _system("storyboard_system.md", episode_seconds=project.series.episode_seconds)
    prev = [e for e in project.episodes if e.id < ep_id and e.scenes]
    prev_txt = ""
    if prev:
        last = prev[-1]
        prev_txt = f"\n上一集（{last.id}）结尾：{last.cliffhanger}"
    user = (f"剧集圣经：\n{bible_digest(project)}\n\n本集：{ep.id}《{ep.title}》\n梗概：{ep.synopsis}\n"
            f"开场钩子：{ep.hook}\n结尾卡点：{ep.cliffhanger}{prev_txt}\n{notes}").strip()

    def validate(d):
        scenes = [Scene.model_validate(s) for s in d.get("scenes", [])]
        if not scenes:
            raise ValueError("scenes 为空")
        tmp = project.model_copy(deep=True)
        e = tmp.episode(ep_id)
        e.scenes = scenes
        probs = [x for x in tmp.check() if x.startswith(f"{ep_id}/") or "镜头 id" in x]
        if probs:
            raise ValueError("；".join(probs[:12]))
        total = sum(sh.duration for sc in scenes for sh in sc.shots)
        target = project.series.episode_seconds
        if not (0.6 * target <= total <= 1.5 * target):
            raise ValueError(f"总时长 {total:.0f}s 偏离目标 {target}s 太多")

    data = llm.json_call(system, user, validate=validate)
    ep.scenes = [Scene.model_validate(s) for s in data["scenes"]]
    ep.hook = data.get("hook") or ep.hook
    ep.cliffhanger = data.get("cliffhanger") or ep.cliffhanger
    ep.bgm_prompt = data.get("bgm_prompt", "")
    if data.get("title"):
        ep.title = data["title"]
    return ep


def shot_count(ep: Episode) -> int:
    return sum(len(sc.shots) for sc in ep.scenes)


def all_shots(ep: Episode) -> list[tuple[Scene, Shot]]:
    return [(sc, sh) for sc in ep.scenes for sh in sc.shots]
