"""审片页：每段所有抽卡结果并排播放，显示质检问题、提示词与关键帧，方便人工选条。

选好后用命令行记录：  python -m aidrama pick <工程> ep01 ep01_g03 2
"""
from __future__ import annotations

import html
import os
from pathlib import Path


def write_review(pl, ep_id: str) -> Path:
    p = pl.project
    ep = p.episode(ep_id)
    smap = p.shot_map(ep)
    out = pl.dir / "episodes" / ep_id / "review.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    def rel(path: str | None) -> str:
        if not path:
            return ""
        return Path(os.path.relpath(pl.abs(path), out.parent)).as_posix()

    rows = []
    for seg in ep.segments:
        shots = [smap[s][1] for s in seg.shots]
        kfs = "".join(f'<figure><img src="{html.escape(rel(sh.keyframe))}" loading="lazy"><figcaption>{sh.id} · {html.escape(sh.action)}</figcaption></figure>'
                      for sh in shots if sh.keyframe)
        lines = "".join(f"<li><b>{html.escape(p.character(ln.speaker).name)}</b>：{html.escape(ln.plain)} <i>({ln.emotion})</i></li>"
                        for sh in shots for ln in sh.dialogue)
        takes = []
        for i, t in enumerate(seg.takes):
            chosen = " chosen" if seg.chosen == i else ""
            issues = "".join(f"<li>{html.escape(x)}</li>" for x in t.qc.get("issues", [])) or "<li class=ok>质检通过</li>"
            cer = t.qc.get("cer")
            takes.append(f'<div class="take{chosen}"><video src="{html.escape(rel(t.path))}" controls preload="metadata"></video>'
                         f'<div class=meta>take {i + 1} · seed {t.seed} · {t.preset} · {t.seconds:.0f}s'
                         f'{f" · CER {cer:.0%}" if cer is not None else ""}{" · ✅ 已选" if chosen else ""}</div>'
                         f'<ul class=qc>{issues}</ul><code>python -m aidrama pick {html.escape(str(pl.dir))} {ep_id} {seg.id} {i + 1}</code></div>')
        prompt = html.escape(seg.prompt or "")
        rows.append(f"""
<section>
  <h2>{seg.id} <small>{seg.engine} · {len(seg.shots)} 镜 · {seg.planned:.1f}s</small></h2>
  <div class=kfs>{kfs}</div>
  <ul class=lines>{lines}</ul>
  <div class=takes>{''.join(takes) or '<p>还没有生成</p>'}</div>
  <details><summary>H3 提示词</summary><pre>{prompt}</pre></details>
</section>""")
    final = f'<video src="{html.escape(rel(ep.output))}" controls class=final></video>' if ep.output else ""
    doc = f"""<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(p.series.title)} {ep_id} 审片</title>
<style>
:root{{--bg:#fafafa;--fg:#1a1a1a;--card:#fff;--line:#e3e3e3;--accent:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--card:#1f1f1f;--line:#333}}}}
body{{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:16px 0}}
h2 small{{font-weight:normal;opacity:.6;font-size:.7em}}
.kfs,.takes{{display:flex;gap:12px;overflow-x:auto}}
figure{{margin:0;width:150px;flex:none}} figure img{{width:150px;border-radius:6px}} figcaption{{font-size:12px;opacity:.75}}
.take{{flex:none;width:260px;border:2px solid transparent;border-radius:8px;padding:6px}} .take.chosen{{border-color:var(--accent)}}
.take video{{width:100%;border-radius:6px;background:#000}} .meta{{font-size:12px;margin:4px 0}}
.qc{{font-size:12px;margin:0;padding-left:18px}} .ok{{color:#2e7d32}} code{{font-size:11px;word-break:break-all;opacity:.7}}
pre{{white-space:pre-wrap;font-size:12px}} .final{{width:min(360px,100%);border-radius:8px;background:#000}}
</style></head><body>
<h1>{html.escape(p.series.title)} · {ep_id}《{html.escape(ep.title)}》</h1>
{final}
{''.join(rows)}
</body></html>"""
    out.write_text(doc, encoding="utf-8")
    pl.log(f"[review] {out}")
    return out
