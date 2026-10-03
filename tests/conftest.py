import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / "tests" / "data" / "object_info_comfyui_0.38.json"


@pytest.fixture(scope="session")
def object_info() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture
def demo_project(tmp_path: Path) -> Path:
    from aidrama.cli import main

    d = tmp_path / "demo"
    main(["init-demo", str(d)])
    return d


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
