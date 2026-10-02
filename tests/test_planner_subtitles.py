from aidrama.config import load_config
from aidrama.planner import build_plan, plan_segments, shot_timing
from aidrama.schema import Line, Project, Shot
from aidrama.subtitles import Cue, split_cue, wrap_zh


def test_demo_segmentation(demo_project):
    p = Project.load(demo_project / "project.yaml")
    cfg = load_config(demo_project)
    ep = p.episodes[0]
    segs = plan_segments(p, ep, cfg)
    shots = [s for seg in segs for s in seg.shots]
    assert shots == [sh.id for sc in ep.scenes for sh in sc.shots]      # 每个镜头恰好出现一次、顺序不变
    for seg in segs:
        assert seg.gen_seconds <= 15.0
        assert len(seg.shots) <= 4
        assert seg.cut_times[0] == 0.0 and seg.cut_times == sorted(seg.cut_times)
        if seg.engine == "h3_fl2va":
            assert len(seg.shots) == 1
    for seg in segs:
        plan = build_plan(p, ep, seg, cfg, dialogue_track="d.wav" if seg.engine == "h3_ref2va" else None, prev_last_frame=None)
        assert sum(1 for r in plan.refs if r.kind == "image") <= 9
        assert len(plan.refs) == len(plan.paths)


def test_shot_timing_extends_for_dialogue():
    sh = Shot(id="x", duration=2.0, dialogue=[Line(speaker="a", text="一二三四五六七八九十", duration=3.0),
                                              Line(speaker="b", text="好", duration=0.8)])
    d = shot_timing(sh, {"lead_in": 0.35, "line_gap": 0.25, "tail": 0.45})
    assert sh.dialogue[0].start == 0.35
    assert sh.dialogue[1].start == 3.6
    assert d == 4.85


def test_wrap_zh():
    assert wrap_zh("三年前寄出的信，为什么今天才到？") == ["三年前寄出的信", "为什么今天才到？"]
    assert wrap_zh("你给我站住！！") == ["你给我站住！！"]
    assert wrap_zh("好。好。好！") == ["好 好 好！"]
    for seg in wrap_zh("这是一个特别特别特别长而且完全没有任何标点符号的句子“好的”吗"):
        assert len(seg) <= 15 and seg[0] not in "，。！？”"


def test_split_cue_timing():
    cues = split_cue(Cue(1.0, 4.0, "三年前寄出的信，为什么今天才到？"))
    assert len(cues) == 2 and cues[0].start == 1.0 and abs(cues[-1].end - 4.0) < 1e-6
    assert cues[0].end <= cues[1].start


def test_pronunciation_markup_only_reaches_tts():
    from aidrama.schema import plain_text

    ln = Line(speaker="a", text="他在银<行|HANG2>里<行|XING2>走")
    assert ln.plain == "他在银行里行走"
    assert plain_text("没有标注") == "没有标注"


def test_cut_detection(tmp_path):
    import shutil
    import subprocess

    import pytest

    from aidrama import qc

    if not shutil.which("ffmpeg"):
        pytest.skip("需要 ffmpeg")
    out = tmp_path / "cuts.mp4"
    # 三个色块镜头：0–2s、2–3.5s、3.5–5s
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=320x568:r=24:d=2",
                    "-f", "lavfi", "-i", "color=c=red:s=320x568:r=24:d=1.5",
                    "-f", "lavfi", "-i", "mandelbrot=s=320x568:r=24",
                    "-filter_complex", "[2:v]trim=duration=1.5,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1[v]",
                    "-map", "[v]", "-pix_fmt", "yuv420p", str(out)], check=True)
    got = qc.detect_cuts(out)
    assert any(abs(g - 2.0) < 0.1 for g in got) and any(abs(g - 3.5) < 0.1 for g in got)
    assert qc.cuts(out, [0.0, 2.0, 3.5]) == []
    issues = qc.cuts(out, [0.0, 1.0, 2.0, 3.5])
    assert any(qc.is_hard(i) and "1.00s" in i for i in issues)
    assert not qc.is_hard("提示：检测到计划外的画面突变 [1.2]")
