"""所有 ComfyUI 工作流构建器的静态校验（对照 ComfyUI 0.38 节点快照）。

更严格的校验（ComfyUI 自己的 validate_prompt）见 scripts/comfy_validate.py，需要装好 ComfyUI 的 Python 环境。
"""
import json

import pytest

from aidrama.export import export_workflows
from aidrama.graph import Graph, validate_api_graph
from aidrama.graphs.h3 import Guide, H3Job, build_h3, snap_length


def _errors(api, object_info):
    return [e for e in validate_api_graph(api, object_info, allow_missing_files=True) if "dangling" not in e]


def test_exported_workflows_validate(tmp_path, object_info):
    paths = export_workflows(tmp_path)
    assert len(paths) >= 13
    for p in paths:
        api = json.loads(p.read_text(encoding="utf-8"))
        assert _errors(api, object_info) == [], p.name


@pytest.mark.parametrize("seconds,frames", [(5, 124), (4, 107), (10.7, 260), (15, 362), (0.1, 5)])
def test_snap_length(seconds, frames):
    n = snap_length(seconds)
    assert n == frames
    assert (n - 5) % 17 == 0


def test_h3_ref2va_with_guides(object_info):
    job = H3Job(prompt="x", seconds=8, mode="ref2va", ref_images=["a.png", "b.png"], ref_audios=["d.wav"],
                guides=[Guide(0, image="a.png"), Guide(96, image="b.png"), Guide(0, audio="d.wav")], seed=1)
    api = build_h3(job)
    assert _errors(api, object_info) == []
    guides = [n for n in api.values() if n["class_type"] == "MiniMaxH3AddGuide"]
    assert sorted(n["inputs"]["frame_idx"] for n in guides) == [0, 0, 96]
    ref = next(n for n in api.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert ref["inputs"]["length"] == snap_length(8)
    assert "ref_images.ref_image_1" in ref["inputs"] and "ref_audios.ref_audio_0" in ref["inputs"]


def test_h3_limits():
    assert H3Job(prompt="x", mode="ref2va", ref_images=[f"{i}.png" for i in range(10)]).check()
    assert H3Job(prompt="x", mode="fl2va", seconds=16).check()
    assert H3Job(prompt="x", mode="ref2va", ref_images=["a.png"], ref_audios=["d.wav"], turbo="4step").check()
    assert H3Job(prompt="x", mode="ref2va", ref_images=["a.png"], ref_audios=["d.wav"], turbo="8step").check() == []
    with pytest.raises(ValueError):
        build_h3(H3Job(prompt="x", mode="fl2va", seconds=16))


def test_h3_fast_and_turbo(object_info):
    for kw in ({"fast": True, "mode": "fl2va", "first_frame": "a.png"}, {"turbo": "8step", "mode": "fl2va"}):
        api = build_h3(H3Job(prompt="x", seconds=5, **kw))
        assert _errors(api, object_info) == []


def test_validator_catches_mistakes(object_info):
    g = Graph()
    g.add("NoSuchNode")
    g.add("KSamplerSelect", sampler_name="definitely_not_a_sampler")
    errs = validate_api_graph(g.to_api(), object_info)
    assert any("unknown node class" in e for e in errs)
    assert any("definitely_not_a_sampler" in e for e in errs)
