"""完整流水线 mock 跑通：不连 ComfyUI / LLM / TTS，但每个提交给 ComfyUI 的工作流都会对照节点快照做静态校验。"""
import json

from aidrama import ffmpeg_utils as ff
from aidrama.pipeline import Pipeline

from conftest import needs_ffmpeg


@needs_ffmpeg
def test_mock_episode_end_to_end(demo_project):
    pl = Pipeline(demo_project, {"mock": True, "video": {"takes": 1}}, log=lambda *_: None)
    res = pl.run_episode("ep01", takes=1)
    final = res["final"]
    assert ff.video_size(final) == (1080, 1920)
    assert ff.has_audio(final)
    assert abs(res["lufs"] - (-14.0)) < 1.0
    assert 20 < res["duration"] < 45
    tags = ff.probe(final)["format"]["tags"]
    aigc = json.loads(tags["AIGC"])["AIGC"]                     # GB 45438-2025 附录 E 隐式标识
    assert aigc["Label"] == "1" and aigc["ContentProducer"] and aigc["PropagateID"] == aigc["ProduceID"]
    assert sum("aigc" in k.lower() for k in tags) == 1            # 只保留一份
    srt = (demo_project / "episodes" / "ep01" / "out" / "ep01.srt").read_text(encoding="utf-8")
    assert "这封信，写的是我的名字" in srt
    graphs = list((demo_project / "graphs").glob("*.json"))
    kinds = {n["class_type"] for g in graphs for n in json.loads(g.read_text(encoding="utf-8")).values()}
    assert {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo", "MiniMaxH3AddGuide", "TextEncodeQwenImageEditPlus",
            "SeedVR2Conditioning", "MiniMaxMusic3TextEncode"} <= kinds
    prompts = list((demo_project / "episodes" / "ep01" / "prompts").glob("*.txt"))
    assert prompts and (demo_project / "episodes" / "ep01" / "review.html").exists()


def test_gpu_handoff_order(demo_project):
    calls = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def free(self):
            calls.append(f"{self.name}.free")

        def unload(self):
            calls.append(f"{self.name}.unload")

    pl = Pipeline(demo_project, {"mock": True}, log=lambda *_: None)
    pl.mock = False
    pl.comfy, pl.audio, pl.llm = Fake("comfy"), Fake("audio"), Fake("llm")
    pl._gpu("llm")
    assert calls == ["comfy.free", "audio.unload"]
    calls.clear()
    pl._gpu("llm")                      # 同一占用者：什么都不做
    assert calls == []
    pl._gpu("comfy")
    assert calls == ["audio.unload", "llm.unload"]
    calls.clear()
    pl._gpu("audio")
    assert calls == ["comfy.free", "llm.unload"]


@needs_ffmpeg
def test_continue_shot_uses_previous_tail_frame(demo_project):
    from aidrama.schema import Project

    p = Project.load(demo_project / "project.yaml")
    sh = p.episodes[0].scenes[1].shots[0]
    sh.method = "continue"
    p.save(demo_project / "project.yaml")
    pl = Pipeline(demo_project, {"mock": True}, log=lambda *_: None)
    pl.cast()
    pl.voice_lines("ep01")
    segs = pl.plan("ep01")
    pl.keyframes("ep01")
    cont = next(s for s in segs if sh.id in s.shots)
    assert cont.engine == "h3_fl2va" and cont.shots == [sh.id]
    pl.video("ep01", takes=1)
    ep = pl.project.episode("ep01")
    cont = next(s for s in ep.segments if sh.id in s.shots)
    assert cont.takes and cont.prompt.startswith("For the target video, at 0.00 seconds")
    assert (demo_project / "episodes" / "ep01" / "segments" / f"{cont.id}_prev_last.png").exists()
    g = json.loads((demo_project / "graphs" / f"{cont.id}_t1.json").read_text(encoding="utf-8"))
    images = [n["inputs"]["image"] for n in g.values() if n["class_type"] == "LoadImage"]
    assert images == [f"aidrama/{cont.id}_prev_last.png"]      # 首帧 = 上一段所选条的最后一帧


def test_choose_respects_manual_pick():
    from aidrama.schema import Segment, Take

    seg = Segment(id="g", scene="s", shots=["a"])
    seg.takes = [Take(path="1.mp4", seed=1, preset="quality", qc={"pass": False}), Take(path="2.mp4", seed=2, preset="quality", qc={"pass": True, "cer": 0.1})]
    Pipeline._choose(seg)
    assert seg.chosen == 1
    seg.takes.append(Take(path="3.mp4", seed=3, preset="quality", qc={"pass": True, "cer": 0.0}))
    seg.video_final = "final.mp4"
    Pipeline._choose(seg)                       # 补抽后自动改选更好的条，旧超分作废
    assert seg.chosen == 2 and seg.video_final is None
    seg.chosen, seg.picked_by_hand = 0, True    # 人工选了第 1 条
    seg.takes.append(Take(path="4.mp4", seed=4, preset="quality", qc={"pass": True, "cer": 0.0}))
    Pipeline._choose(seg)
    assert seg.chosen == 0


@needs_ffmpeg
def test_add_external_take(demo_project, tmp_path):
    from aidrama.mock import placeholder_video

    pl = Pipeline(demo_project, {"mock": True}, log=lambda *_: None)
    pl.cast()
    pl.voice_lines("ep01")
    seg = pl.plan("ep01")[2]
    ext = placeholder_video(tmp_path / "talk.mp4", 480, 848, seg.planned, "external", audio=None)
    take = pl.add_take("ep01", seg.id, ext)
    seg = next(s for s in pl.project.episode("ep01").segments if s.id == seg.id)
    assert seg.chosen == len(seg.takes) - 1 and seg.picked_by_hand and take.preset == "external"
    assert (demo_project / take.path).exists()
