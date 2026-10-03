"""冒烟测试脚本本身的测试：用假的 ComfyClient 截获每个工作流并做静态校验，不需要 GPU。"""
from pathlib import Path

import aidrama.smoke as S
from aidrama.audio import AudioClient
from aidrama.graph import validate_api_graph
from aidrama.mock import placeholder_image, placeholder_video, tone_wav

from conftest import needs_ffmpeg


@needs_ffmpeg
def test_smoke_builds_valid_graphs(tmp_path, object_info, monkeypatch):
    seen = []

    class FakeComfy:
        def __init__(self, url):
            pass

        def alive(self):
            return True

        def free(self):
            pass

        def upload(self, path, subfolder="aidrama"):
            return f"aidrama/{Path(path).name}"

        def run(self, api, out_dir, stem, on_progress=None, validate=True):
            errs = [e for e in validate_api_graph(api, object_info) if "dangling" not in e]
            assert errs == [], (stem, errs)
            seen.append(stem)
            out = Path(out_dir)
            if stem in ("t2i", "keyframe"):
                return [placeholder_image(out / f"{stem}.png", 144, 252, stem)]
            if stem == "bgm":
                return [tone_wav(out / "bgm.wav", 10)]
            return [placeholder_video(out / f"{stem}.mp4", 480, 864, 4.1, stem, audio=tone_wav(out / f"{stem}.wav", 4.1))]

    monkeypatch.setattr(S, "ComfyClient", FakeComfy)
    monkeypatch.setattr(S, "AudioClient", lambda cfg: AudioClient(cfg, mock=True))
    assert S.smoke(out_dir=tmp_path)
    assert seen == ["t2i", "keyframe", "h3_fl2va", "h3_ref2va", "upscaled", "bgm"]
