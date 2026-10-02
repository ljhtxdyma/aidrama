"""代码审查发现过的问题的回归测试（全部用 mock 模式，不需要 GPU）。"""
import json

import pytest

from aidrama import ffmpeg_utils as ff
from aidrama.llm import extract_json
from aidrama.pipeline import Pipeline
from aidrama.schema import Line

from conftest import needs_ffmpeg


def _pl(demo):
    return Pipeline(demo, {"mock": True}, log=lambda *_: None)


def _ready(demo):
    pl = _pl(demo)
    pl.cast()
    pl.voice_lines("ep01")
    pl.plan("ep01")
    pl.keyframes("ep01")
    return pl


@needs_ffmpeg
def test_two_characters_upload_under_distinct_names(demo_project):
    pl = _ready(demo_project)
    g = json.loads((demo_project / "graphs" / "ep01_s01_02_start.json").read_text(encoding="utf-8"))
    imgs = [n["inputs"]["image"] for n in g.values() if n["class_type"] == "LoadImage"]
    assert len(imgs) == 3 and len(set(imgs)) == 3          # 林晚设定图、顾沉设定图、场景图各不相同
    assert pl.project.character("lin_wan").refs["sheet_default"] != pl.project.character("gu_chen").refs["sheet_default"]


@needs_ffmpeg
def test_interrupted_video_run_recovers(demo_project):
    pl = _ready(demo_project)
    real = pl._gen_video
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟 ComfyUI 中途崩溃")
        return real(*a, **kw)

    pl._gen_video = flaky
    with pytest.raises(RuntimeError):
        pl.video("ep01", takes=1)
    ep = pl.project.episode("ep01")
    done = [s for s in ep.segments if s.takes]
    assert done and all(s.chosen is not None for s in done)   # 已生成的段在异常时也完成了质检和选条
    pl2 = _pl(demo_project)
    pl2.video("ep01", takes=1)
    assert all(s.chosen is not None for s in pl2.project.episode("ep01").segments)


@needs_ffmpeg
def test_overlong_dialogue_shot_is_rejected(demo_project):
    pl = _pl(demo_project)
    pl.cast()
    sh = pl.project.episode("ep01").scenes[1].shots[1]
    sh.dialogue = [Line(speaker="gu_chen", text="很长的一句台词" * 4, duration=6.0) for _ in range(3)]
    with pytest.raises(ValueError, match="15"):
        pl.plan("ep01")


@needs_ffmpeg
def test_inserting_a_line_keeps_other_audio(demo_project):
    pl = _pl(demo_project)
    pl.cast()
    pl.voice_lines("ep01")
    sh = pl.project.episode("ep01").scenes[0].shots[0]
    old = sh.dialogue[0]
    before = (old.audio, (demo_project / old.audio).read_bytes())
    sh.dialogue.insert(0, Line(speaker="lin_wan", text="嗯？"))
    pl.voice_lines("ep01")
    assert sh.dialogue[1].audio == before[0]
    assert (demo_project / before[0]).read_bytes() == before[1]
    assert sh.dialogue[0].audio != before[0]


@needs_ffmpeg
def test_offscreen_character_token_is_substituted(demo_project):
    pl = _pl(demo_project)
    sh = pl.project.episode("ep01").scenes[1].shots[0]          # 空镜，没有角色
    sh.motion_prompt = "Rain streaks down the window of [gu_chen]'s empty office."
    pl.save()
    pl = _ready(demo_project)
    pl.video("ep01", takes=1)
    seg = next(s for s in pl.project.episode("ep01").segments if sh.id in s.shots)
    assert "[gu_chen]" not in seg.prompt and "office" in seg.prompt


@needs_ffmpeg
def test_plan_keeps_takes_when_segment_ids_shift(demo_project):
    pl = _ready(demo_project)
    pl.video("ep01", takes=1)
    ep = pl.project.episode("ep01")
    last = ep.segments[-1]
    kept = (tuple(last.shots), last.takes[0].path)
    ep.scenes[0].shots[0].transition = "fade"     # 前面多切出一个段，后面的段号整体后移
    pl.save()
    pl.plan("ep01")
    seg = next(s for s in pl.project.episode("ep01").segments if tuple(s.shots) == kept[0])
    assert seg.takes and seg.takes[0].path == kept[1]


@needs_ffmpeg
def test_dialogue_track_cleared_when_lines_removed(demo_project):
    pl = _ready(demo_project)
    ep = pl.project.episode("ep01")
    seg = ep.segments[0]
    assert pl._dialogue_track(ep, seg)
    for sid in seg.shots:
        pl.project.shot_map(ep)[sid][1].dialogue = []
    assert pl._dialogue_track(ep, seg) is None and seg.dialogue_track is None


def test_extract_json_variants():
    assert extract_json('思考 {不是json} </think> {"a": 1}') == {"a": 1}
    assert extract_json('[JSON]：{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(ValueError, match="截断"):
        extract_json('{"a": 1')


@needs_ffmpeg
def test_assemble_silent_mix(tmp_path):
    from aidrama.assemble import AssembleOptions, Clip, assemble
    from aidrama.mock import placeholder_video

    v = placeholder_video(tmp_path / "a.mp4", 360, 640, 2.0, "silent")
    res = assemble([Clip(str(v), 2.0)], tmp_path / "out", "ep", AssembleOptions(width=360, height=640, burn_subtitles=False))
    assert ff.duration(res["final"]) == pytest.approx(2.0, abs=0.1)
