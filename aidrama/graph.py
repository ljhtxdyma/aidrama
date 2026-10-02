"""Minimal builder for ComfyUI API-format graphs ("prompt" JSON).

ComfyUI 的 /prompt 接口接收的是 API 格式：{node_id: {"class_type": ..., "inputs": {...}}}，
连线写成 [源节点id, 输出槽位]。这里用一个很小的构建器生成这种 JSON，
并可对照 /object_info 校验节点名、输入名、必填项和枚举值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Out:
    """A reference to one output slot of a node."""

    node_id: str
    slot: int = 0

    def to_json(self) -> list:
        return [self.node_id, self.slot]


class Node:
    def __init__(self, graph: "Graph", node_id: str, class_type: str):
        self.graph = graph
        self.id = node_id
        self.class_type = class_type

    def __getitem__(self, slot: int) -> Out:
        return Out(self.id, slot)

    @property
    def out(self) -> Out:
        return Out(self.id, 0)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self._next = 1

    def add(self, class_type: str, _title: str | None = None, **inputs: Any) -> Node:
        node_id = str(self._next)
        self._next += 1
        enc: dict[str, Any] = {}
        for k, v in inputs.items():
            if v is None:
                continue
            if isinstance(v, Node):
                v = v.out
            enc[k] = v.to_json() if isinstance(v, Out) else v
        entry: dict[str, Any] = {"class_type": class_type, "inputs": enc}
        if _title:
            entry["_meta"] = {"title": _title}
        self.nodes[node_id] = entry
        return Node(self, node_id, class_type)

    def to_api(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.nodes))

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.nodes, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Validation against /object_info
# ---------------------------------------------------------------------------

def _input_spec(info: dict, name: str):
    for section in ("required", "optional"):
        sec = (info.get("input") or {}).get(section) or {}
        if name in sec:
            return section, sec[name]
    return None, None


def _combo_options(spec) -> list | None:
    """Return the allowed values for a combo input, or None if not a combo."""
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    t = spec[0]
    if isinstance(t, list):  # legacy combo: [[opt1, opt2], {...}]
        return t
    if t == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options")
    return None


MODEL_EXTS = (".safetensors", ".gguf", ".pt", ".pth", ".ckpt", ".bin", ".onnx", ".sft")


def _typed(t) -> bool:
    """False for wildcard / V3 MatchType sockets (resolved at runtime, accept anything compatible)."""
    return isinstance(t, str) and t not in ("*", "COMBO") and not t.startswith("COMFY_MATCHTYPE")


def validate_api_graph(api: dict, object_info: dict, *, allow_missing_files: bool = True) -> list[str]:
    """Static check of an API graph against ComfyUI's /object_info.

    Returns a list of human-readable problems (empty list == valid).
    Model/file combos are allowed to be absent when ``allow_missing_files`` is set,
    because the cloud CI box has no model weights; everything else must match.
    """
    errors: list[str] = []
    for nid, node in api.items():
        ct = node.get("class_type")
        info = object_info.get(ct)
        if info is None:
            errors.append(f"[{nid}] unknown node class '{ct}'")
            continue
        inputs = node.get("inputs", {})
        req = (info.get("input") or {}).get("required") or {}
        for rname, rspec in req.items():
            if rname not in inputs:
                # V3 dynamic inputs (autogrow etc.) may legitimately be absent
                if isinstance(rspec, (list, tuple)) and len(rspec) > 1 and isinstance(rspec[1], dict) and rspec[1].get("optional"):
                    continue
                if any(k.startswith(rname + ".") for k in inputs):
                    continue        # V3 动态输入以 "name.child" 形式提交
                if isinstance(rspec, (list, tuple)) and rspec and rspec[0] == "COMFY_AUTOGROW_V3" \
                        and ((rspec[1] if len(rspec) > 1 else {}).get("template") or {}).get("min", 1) == 0:
                    continue        # 允许 0 项的 autogrow（例如纯文生图时不接参考图）
                errors.append(f"[{nid}:{ct}] missing required input '{rname}'")
        for iname, val in inputs.items():
            section, spec = _input_spec(info, iname)
            if section is None:
                # dynamic / autogrow sub-inputs look like "images.image0" or "values.a"
                if "." in iname:
                    continue
                errors.append(f"[{nid}:{ct}] unknown input '{iname}'")
                continue
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str) and isinstance(val[1], int):
                src = api.get(val[0])
                if src is None:
                    errors.append(f"[{nid}:{ct}] input '{iname}' links to missing node {val[0]}")
                    continue
                src_info = object_info.get(src["class_type"]) or {}
                outs = src_info.get("output") or []
                if val[1] >= len(outs):
                    errors.append(f"[{nid}:{ct}] input '{iname}' links to slot {val[1]} but {src['class_type']} has {len(outs)} outputs")
                else:
                    want = spec[0] if isinstance(spec, (list, tuple)) else None
                    have = outs[val[1]]
                    if _typed(want) and _typed(have):
                        wset = set(want.split(","))
                        hset = set(have.split(","))
                        if not (wset & hset):
                            errors.append(f"[{nid}:{ct}] input '{iname}' expects {want} but got {have} from {src['class_type']}")
                continue
            opts = _combo_options(spec)
            if opts is not None and isinstance(val, str):
                if val not in opts:
                    is_file = val.lower().endswith(MODEL_EXTS) or "/" in val or "\\" in val
                    if not (allow_missing_files and (is_file or not opts or all(isinstance(o, str) and o.lower().endswith(MODEL_EXTS) for o in opts))):
                        # image/audio upload combos also list files; tolerate media names
                        if not val.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".flac", ".mp4", ".mov", ".webm")):
                            errors.append(f"[{nid}:{ct}] input '{iname}'='{val}' not in options {opts[:8]}{'...' if len(opts) > 8 else ''}")
            elif isinstance(spec, (list, tuple)) and spec and spec[0] in ("INT", "FLOAT") and isinstance(val, (int, float)) and len(spec) > 1 and isinstance(spec[1], dict):
                lo, hi = spec[1].get("min"), spec[1].get("max")
                if lo is not None and val < lo:
                    errors.append(f"[{nid}:{ct}] input '{iname}'={val} < min {lo}")
                if hi is not None and val > hi:
                    errors.append(f"[{nid}:{ct}] input '{iname}'={val} > max {hi}")
        if not info.get("output_node") and not any(
            isinstance(v, list) and len(v) == 2 and v[0] == nid for other in api.values() for v in other.get("inputs", {}).values()
        ):
            # dangling non-output node: harmless (ComfyUI skips it) but usually a bug
            errors.append(f"[{nid}:{ct}] output is never used (dangling node)")
    if not any((object_info.get(n["class_type"]) or {}).get("output_node") for n in api.values()):
        errors.append("graph has no output node (SaveImage/SaveVideo/...)")
    return errors
