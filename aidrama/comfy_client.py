"""ComfyUI HTTP/WebSocket client used by the pipeline.

只依赖 requests（websocket-client 可选，用于实时进度）。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from . import net

from .graph import validate_api_graph


class ComfyError(RuntimeError):
    pass


@dataclass
class ComfyResult:
    prompt_id: str
    outputs: dict[str, Any]
    files: list[dict] = field(default_factory=list)  # [{filename, subfolder, type, kind}]
    seconds: float = 0.0


def upload_name(path: str | os.PathLike) -> str:
    """上传到 ComfyUI 时的文件名：内容哈希 + 原名。不同角色的 sheet_default.png 不会互相覆盖，内容相同则复用。"""
    p = Path(path)
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"{h.hexdigest()[:12]}_{p.name}"


def _close(ws) -> None:
    try:
        if ws is not None:
            ws.close()
    except Exception:
        pass


class ComfyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex
        self._object_info: dict | None = None

    # ------------------------------------------------------------------ basics
    def _get(self, path: str, **kw) -> requests.Response:
        r = net.get(self.base + path, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r

    def _post(self, path: str, **kw) -> requests.Response:
        r = net.post(self.base + path, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ComfyError(f"POST {path} -> {r.status_code}: {r.text[:4000]}")
        return r

    def alive(self) -> bool:
        try:
            self._get("/system_stats")
            return True
        except Exception:
            return False

    def system_stats(self) -> dict:
        return self._get("/system_stats").json()

    def object_info(self, refresh: bool = False) -> dict:
        if self._object_info is None or refresh:
            self._object_info = self._get("/object_info").json()
        return self._object_info

    def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        """在不同阶段之间释放显存（例如从图像模型切到视频模型）。"""
        self._post("/free", json={"unload_models": unload_models, "free_memory": free_memory})

    # ------------------------------------------------------------------ files
    def upload(self, path: str | os.PathLike, subfolder: str = "aidrama", overwrite: bool = True) -> str:
        """上传图片/音频/视频到 ComfyUI input 目录，返回可在 LoadImage/LoadAudio/LoadVideo 中使用的名字。"""
        p = Path(path)
        name = upload_name(p)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with open(p, "rb") as f:
            r = self._post(
                "/upload/image",
                files={"image": (name, f, mime)},
                data={"subfolder": subfolder, "type": "input", "overwrite": "true" if overwrite else "false"},
            )
        info = r.json()
        name = info["name"]
        sub = info.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    def download(self, file: dict, dest: str | os.PathLike) -> Path:
        params = {"filename": file["filename"], "subfolder": file.get("subfolder", ""), "type": file.get("type", "output")}
        r = net.get(self.base + "/view", params=params, timeout=600, stream=True)
        r.raise_for_status()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        return dest

    # ------------------------------------------------------------------ run
    def validate(self, api: dict) -> list[str]:
        return validate_api_graph(api, self.object_info(), allow_missing_files=False)

    def queue(self, api: dict) -> str:
        r = self._post("/prompt", json={"prompt": api, "client_id": self.client_id})
        data = r.json()
        if data.get("node_errors"):
            raise ComfyError("node_errors: " + json.dumps(data["node_errors"], ensure_ascii=False)[:4000])
        return data["prompt_id"]

    def _ws_connect(self):
        try:
            import websocket  # type: ignore

            return websocket.create_connection(self.base.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}", timeout=30)
        except Exception:  # noqa: BLE001
            return None

    def wait(self, prompt_id: str, timeout: float = 4 * 3600, on_progress: Callable[[str], None] | None = None,
             ws=None) -> ComfyResult:
        """等待任务完成。

        有 websocket 时只靠推送消息判断进度，采样期间不去轮询 /history ——
        社区在 5090 上实测，采样中频繁请求 ComfyUI HTTP 接口会让单步卡顿 18~43 秒。
        """
        t0 = time.time()
        connect = self._ws_connect
        if ws is None:
            ws = connect()
        reconnects = 0
        last_poll = 0.0
        done_signal = False
        check_now = False
        errors = 0
        missing = 0
        while time.time() - t0 < timeout:
            if ws is not None and not done_signal and not missing:   # 收到完成信号或任务疑似丢失：不再阻塞等消息，改为查历史
                try:
                    msg = ws.recv()
                    if isinstance(msg, str):
                        m = json.loads(msg)
                        d = m.get("data", {})
                        if d.get("prompt_id") not in (None, prompt_id):
                            continue
                        t = m.get("type")
                        if t == "progress" and on_progress:
                            on_progress(f"step {d.get('value')}/{d.get('max')} (node {d.get('node')})")
                        elif t == "executing":
                            if d.get("node") is None and d.get("prompt_id") == prompt_id:
                                done_signal = True
                            elif on_progress and d.get("node"):
                                on_progress(f"node {d.get('node')}")
                        elif t in ("execution_success", "execution_error", "execution_interrupted"):
                            done_signal = True
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ != "WebSocketTimeoutException":
                        # 连接断了：重连一次；再不行就改成低频轮询 /history（不能空转，也不能高频打扰采样）
                        _close(ws)
                        ws = connect() if reconnects < 3 else None
                        reconnects += 1
                        check_now = True      # 断线期间可能已完成，立刻查一次历史
            # 收到完成信号：每秒查一次直到历史里出现；无 websocket：每 30 秒；有 websocket：120 秒兜底一次
            interval = 0 if check_now else 0.5 if done_signal else 10 if missing else (30 if ws is None else 120)
            if time.time() - last_poll >= interval:
                last_poll = time.time()
                check_now = False
                try:
                    hist = self._get(f"/history/{prompt_id}").json()
                    errors = 0
                except requests.RequestException as e:
                    errors += 1  # 偶发网络错误不要丢掉一条要跑好几分钟的镜头
                    if errors >= 10:
                        _close(ws)
                        raise ComfyError(f"连续 10 次无法连接 ComfyUI: {e}")
                    time.sleep(3)
                    continue
                if prompt_id not in hist:
                    # 既不在历史也不在队列里（ComfyUI 重启过）：连续 3 次就判定任务丢失，不要干等 4 小时
                    try:
                        q = self._get("/queue").json()
                        queued = any(item[1] == prompt_id for k in ("queue_running", "queue_pending") for item in q.get(k, []))
                    except Exception:  # noqa: BLE001
                        queued = True
                    missing = 0 if queued else missing + 1
                    if missing >= 3:
                        _close(ws)
                        raise ComfyError(f"任务 {prompt_id} 不在 ComfyUI 的队列和历史里（ComfyUI 可能重启过），请重跑")
                if prompt_id in hist:
                    h = hist[prompt_id]
                    status = h.get("status", {})
                    if status.get("status_str") == "error":
                        msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                        detail = msgs[-1][1] if msgs else status
                        _close(ws)
                        raise ComfyError(f"execution error: {json.dumps(detail, ensure_ascii=False)[:4000]}")
                    if status.get("completed") or h.get("outputs"):
                        _close(ws)
                        return ComfyResult(prompt_id, h.get("outputs", {}), self._collect(h.get("outputs", {})), time.time() - t0)
            if done_signal:
                time.sleep(0.25)
            elif missing:
                time.sleep(1)
            elif ws is None:
                time.sleep(2)
        _close(ws)
        raise ComfyError(f"timeout waiting for {prompt_id}")

    @staticmethod
    def _collect(outputs: dict) -> list[dict]:
        files = []
        for node_out in outputs.values():
            for kind in ("images", "gifs", "videos", "audio", "files"):
                for f in node_out.get(kind, []) or []:
                    if isinstance(f, dict) and "filename" in f:
                        files.append({**f, "kind": kind})
        return files

    def run(self, api: dict, out_dir: str | os.PathLike, stem: str, on_progress=None, validate: bool = True) -> list[Path]:
        """校验 → 排队 → 等待 → 下载全部输出，返回本地文件路径列表。"""
        if validate:
            errs = self.validate(api)
            if errs:
                raise ComfyError("graph validation failed:\n  " + "\n  ".join(errs))
        ws = self._ws_connect()     # 先连 websocket 再排队：否则很快的任务可能在连上之前就跑完了，收不到完成通知
        pid = self.queue(api)
        res = self.wait(pid, on_progress=on_progress, ws=ws)
        paths = []
        for i, f in enumerate(res.files):
            if f.get("type") == "temp":
                continue
            ext = Path(f["filename"]).suffix
            suffix = "" if i == 0 else f"_{i}"
            paths.append(self.download(f, Path(out_dir) / f"{stem}{suffix}{ext}"))
        if not paths:
            raise ComfyError(f"prompt {pid} finished but produced no files: {json.dumps(res.outputs)[:1000]}")
        return paths
