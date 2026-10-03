"""HTTP 小工具：访问本机服务（ComfyUI / 音频服务 / 本地 LLM）时绕过系统代理。

国内环境常开 Clash 等代理并设置 HTTP(S)_PROXY，requests 默认会把 127.0.0.1 的请求也发给代理，
导致 502 / 超时。这里对本机地址关闭环境代理，远程地址（云端 LLM API 等）照常走代理。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import requests

_LOCAL_NAMES = {"localhost", "host.docker.internal"}
_NO_PROXY = {"http": None, "https": None}


def is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_NAMES or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _kw(url: str, kw: dict) -> dict:
    if is_local(url) and "proxies" not in kw:
        kw["proxies"] = _NO_PROXY
    return kw


def get(url: str, **kw) -> requests.Response:
    return requests.get(url, **_kw(url, kw))


def post(url: str, **kw) -> requests.Response:
    return requests.post(url, **_kw(url, kw))
