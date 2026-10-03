from aidrama.h3prompt import (CutSpec, RefItem, SegmentSpec, Speaker, TimedLine, clip_text, compile_fl2va,
                              compile_ref2va, lint)
from aidrama.llm import LLM, LLMConfig
from aidrama.h3prompt import refine


def _spec():
    l1 = TimedLine("lin_wan", "这封信，写的是我的名字。", 0.35, 3.0, "in a low, stunned whisper")
    l2 = TimedLine("gu_chen", "林小姐，这封信你不该打开。", 6.9, 9.6, "quietly")
    cuts = [
        CutSpec(0.0, 3.6, "<Subject 1> reads the envelope; her eyes widen.", "The camera pushes in slowly.", "close", [l1]),
        CutSpec(3.6, 2.9, "<Subject 2> steps into the doorway.", "Static shot.", "medium_wide", [], listeners=["lin_wan", "gu_chen"],
                keyframe_label="<Picture 2>"),
        CutSpec(6.5, 4.2, "<Subject 2> raises his eyes to her face.", "", "medium_close", [l2], keyframe_label="<Picture 3>"),
    ]
    speakers = {"lin_wan": Speaker("lin_wan", "<Subject 1>", "a clear, cool young female voice"),
                "gu_chen": Speaker("gu_chen", "<Subject 2>", "a low, calm male voice")}
    return SegmentSpec(duration=10.7, cuts=cuts, speakers=speakers, lighting="low-key lamp light", sound="Quiet archive room tone.")


def _refs():
    return [RefItem("image", "first_frame", cut=1, desc="Close-up of [lin_wan]"),
            RefItem("image", "keyframe", cut=2, desc="Medium-wide shot"),
            RefItem("image", "keyframe", cut=3, desc="Medium close-up"),
            RefItem("image", "identity", char_id="lin_wan"),
            RefItem("image", "identity", char_id="gu_chen"),
            RefItem("audio", "dialogue_track")]


IDENTITY = {"lin_wan": "a slender woman in her mid-20s", "gu_chen": "a tall man in his early 30s"}


def test_ref2va_compiles_and_lints_clean():
    spec = _spec()
    p = compile_ref2va(spec, [r if r.desc != "Close-up of [lin_wan]" else RefItem("image", "first_frame", cut=1, desc="Close-up of her")
                              for r in _refs()], IDENTITY)
    assert lint(p, spec, "ref2va", n_images=5, n_audios=1) == []
    assert "<d>[Chinese] 这封信，写的是我的名字。</d>" in p
    assert "[Shot 2] At 00:03.600" in p
    assert "lips remain completely closed" in p
    assert p.rstrip().endswith("N/A")


def test_lint_catches_errors():
    spec = _spec()
    good = compile_ref2va(spec, [RefItem(r.kind, r.role, r.char_id, r.cut, "") for r in _refs()], IDENTITY)
    assert lint(good, spec, "ref2va", 5, 1) == []
    bad = good.replace("这封信，写的是我的名字。", "这封信写的是我的名字")
    assert any("逐字" in e for e in lint(bad, spec, "ref2va", 5, 1))
    assert any("Picture" in e for e in lint(good, spec, "ref2va", 2, 1))
    assert any("中文" in e for e in lint(good.replace("Static shot.", "镜头不动。"), spec, "ref2va", 5, 1))
    assert any("占位符" in e for e in lint(good.replace("Static shot.", "[gu_chen] waits."), spec, "ref2va", 5, 1))
    assert any("字段" in e for e in lint(good.replace("overall_soundscape:", "sound:"), spec, "ref2va", 5, 1))


def test_fl2va_i2v():
    cut = CutSpec(0.0, 4.0, "City lights twinkle beyond the glass.", "The camera pushes in slowly.", "wide")
    spec = SegmentSpec(duration=4.0, cuts=[cut], speakers={}, lighting="warm desk lamp")
    p = compile_fl2va(spec, first_frame=True)
    assert p.startswith("For the target video, at 0.00 seconds into the target video, <Picture 1>")
    assert "city lights twinkle" in p          # 句中拼接首字母小写
    assert lint(p, spec, "fl2va", n_images=1) == []


def test_clip_text():
    s = "Close-up at eye level of a woman seated at a wooden worktable, holding a yellowed envelope, her face lit warmly, shelves softly blurred"
    out = clip_text(s, 80)
    assert len(out) <= 80 and not out.endswith(",") and out in s
    assert clip_text("short", 80) == "short"


def test_refine_falls_back_to_draft_on_bad_llm_output():
    spec = _spec()
    draft = compile_ref2va(spec, [RefItem(r.kind, r.role, r.char_id, r.cut, "") for r in _refs()], IDENTITY)

    llm = LLM(LLMConfig.from_dict({"base_url": "http://x", "model": "m"}), mock=lambda m: "subject_definitions: 这是中文，没有台词")
    out, notes = refine(llm, draft, spec, "ref2va", {}, 5, 1)
    assert out == draft and notes
