"""LLM 客户端：任何 OpenAI 兼容接口都能用。

  本地：Ollama (http://127.0.0.1:11434/v1)、LM Studio (http://127.0.0.1:1234/v1)、vLLM、llama.cpp server
  云端：DeepSeek、通义千问 DashScope 兼容模式、Kimi、智谱、OpenRouter、Anthropic 兼容网关 ……

只依赖 requests。json_call() 会自动从回复中抽取 JSON，解析失败时把错误反馈给模型重试。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from . import net


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "aidrama-llm"
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 8192
    timeout: float = 600.0
    extra: dict | None = None        # 透传给接口的额外参数，例如 {"reasoning_effort": "low"}
    unload: str = "auto"             # auto | ollama | none：ComfyUI 阶段前是否让 LLM 释放显存

    @classmethod
    def from_dict(cls, d: dict) -> "LLMConfig":
        d = dict(d or {})
        key = d.get("api_key", "")
        if isinstance(key, str) and key.startswith("env:"):
            d["api_key"] = os.environ.get(key[4:], "")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class LLM:
    def __init__(self, cfg: LLMConfig, mock: Callable[[list[dict]], str] | None = None):
        self.cfg = cfg
        self.mock = mock

    def unload(self) -> None:
        """Ollama：用 keep_alive=0 立即卸载模型，把显存还给 ComfyUI。其它后端（llama.cpp/vLLM/云端）不处理。"""
        if self.mock is not None or not self.cfg.model:
            return
        mode = self.cfg.unload
        if mode == "auto":
            mode = "ollama" if ":11434" in self.cfg.base_url else "none"
        if mode != "ollama":
            return
        root = re.sub(r"/v1/?$", "", self.cfg.base_url.rstrip("/"))
        try:
            net.post(root + "/api/generate", json={"model": self.cfg.model, "keep_alive": 0}, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def chat(self, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None) -> str:
        if self.mock is not None:
            return self.mock(messages)
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.extra:
            body.update(self.cfg.extra)
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        last = None
        for attempt in range(3):
            try:
                r = net.post(url, json=body, headers=headers, timeout=self.cfg.timeout)
                if r.status_code >= 400:
                    raise LLMError(f"{r.status_code}: {r.text[:1000]}")
                msg = r.json()["choices"][0]["message"]
                text = msg.get("content") or ""
                return _strip_think(text)
            except (requests.RequestException, LLMError, KeyError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise LLMError(f"LLM 调用失败（{url}，模型 {self.cfg.model}）：{last}")

    def text(self, system: str, user: str, **kw) -> str:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw).strip()

    def json_call(self, system: str, user: str, validate: Callable[[Any], None] | None = None, retries: int = 3, **kw) -> Any:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        err = None
        for _ in range(retries):
            raw = self.chat(messages, **kw)
            try:
                data = extract_json(raw)
                if validate:
                    validate(data)
                return data
            except Exception as e:  # noqa: BLE001
                err = e
                messages += [
                    {"role": "assistant", "content": raw[:12000]},
                    {"role": "user", "content": f"上面的输出无法通过校验：{e}\n请只输出修正后的完整 JSON，不要任何解释。"},
                ]
        raise LLMError(f"LLM 输出 {retries} 次都不是合法 JSON：{err}")


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def extract_json(text: str) -> Any:
    text = _strip_think(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找到第一个 { 或 [ 与其匹配的结尾
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("回复中没有 JSON")
    s = min(starts)
    opener = text[s]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(s, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[s:i + 1])
    raise ValueError("JSON 不完整（可能被截断，请调大 max_tokens）")
