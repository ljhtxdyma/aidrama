"""scripts/download_models.py 的下载/续传/防坏文件逻辑（本地 HTTP 服务模拟各种来源）。"""
import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ROOT

spec = importlib.util.spec_from_file_location("dl", ROOT / "scripts" / "download_models.py")
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

BLOB = bytes(range(256)) * 4096          # 1 MiB 的“模型”
EXPECT_GB = len(BLOB) / 1e9


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/error":           # 限流时返回的 JSON
            body = b'{"error": "rate limited"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/pointer":         # git-lfs 指针（二进制类型但很小）
            body = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1048576\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(BLOB):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(BLOB)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(BLOB) - 1}/{len(BLOB)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(BLOB) - start))
        self.end_headers()
        self.wfile.write(BLOB[start:])


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_full_download(server, tmp_path):
    dst = tmp_path / "m.safetensors"
    dl.download(server + "/m.safetensors", dst, EXPECT_GB)
    assert dst.read_bytes() == BLOB


def test_resume_from_partial(server, tmp_path):
    dst = tmp_path / "m.safetensors"
    part = dl.part_path(dst, server + "/m.safetensors")
    part.write_bytes(BLOB[:300_000])
    dl.download(server + "/m.safetensors", dst, EXPECT_GB)
    assert dst.read_bytes() == BLOB and not part.exists()


def test_error_body_is_never_accepted(server, tmp_path):
    dst = tmp_path / "m.safetensors"
    for path in ("/error", "/pointer"):
        for _ in range(2):            # 重试也不能把错误内容续传成“完整文件”
            with pytest.raises(RuntimeError):
                dl.download(server + path, dst, EXPECT_GB)
        assert not dst.exists()
        assert not list(tmp_path.glob("*.part"))


def test_416_with_wrong_total_is_rejected(server, tmp_path):
    dst = tmp_path / "m.safetensors"
    part = dl.part_path(dst, server + "/m.safetensors")
    part.write_bytes(b"x" * (len(BLOB) + 10))     # 比服务器上的文件还大：不能直接收下
    with pytest.raises(RuntimeError):
        dl.download(server + "/m.safetensors", dst, EXPECT_GB)
    assert not dst.exists() and not part.exists()


def test_part_name_is_windows_safe():
    from pathlib import Path

    p = dl.part_path(Path("m.safetensors"), "http://127.0.0.1:8080/x/m.safetensors")
    assert ":" not in p.name and p.name == "m.safetensors.127.0.0.1_8080.part"


def test_unknown_group_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["download_models.py", "--groups", "core fast typo", "--list"])
    assert dl.main() == 2
    assert "typo" in capsys.readouterr().out
