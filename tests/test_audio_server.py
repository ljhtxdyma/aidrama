"""services/audio_server.py 的 HTTP 接口联调（--mock：不加载模型）。"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from aidrama.audio import AudioClient, build_track, trim_silence
from aidrama import ffmpeg_utils as ff

from conftest import ROOT, needs_ffmpeg


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "services" / "audio_server.py"), "--mock", "--port", str(port),
                             "--engines", "indextts,voicedesign,asr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            requests.get(url + "/health", timeout=0.5)
            break
        except requests.RequestException:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(10)


@needs_ffmpeg
def test_audio_client_roundtrip(server, tmp_path: Path):
    cl = AudioClient({"tts_url": server, "design_url": server, "asr_url": server}, mock=False)
    assert all(v.get("ok") for v in cl.health().values())
    ref = cl.design_voice("你好，我是林晚。", "二十五岁女性，声音清亮", tmp_path / "ref.wav")
    assert ref.exists()
    wav, dur = cl.tts("这封信，写的是我的名字。", ref, tmp_path / "l1.wav", emotion="surprise")
    assert wav.exists() and dur > 1.0
    clean = trim_silence(wav, tmp_path / "l1_clean.wav")
    assert ff.lufs(clean) == pytest.approx(-18.0, abs=1.5)
    assert cl.asr(clean, expect="这封信，写的是我的名字。") == "这封信，写的是我的名字。"
    items = cl.align(clean, "这封信")
    assert [i["text"] for i in items] == ["这", "封", "信"]
    track = build_track([(clean, 0.35), (clean, 4.0)], 8.0, tmp_path / "track.wav")
    assert ff.duration(track) == pytest.approx(8.0, abs=0.05)
