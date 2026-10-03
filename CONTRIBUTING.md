# 开发与版本管理约定

本仓库按标准 Git 工作流管理：`main` 永远是可用版本，所有改动走分支和 Pull Request，CI 全绿才合并，发布打 tag。

## 1. 分支

| 分支 | 用途 | 规则 |
|---|---|---|
| `main` | 稳定版，`git clone` 下来直接能用 | 不直接 push，只通过 PR 合并；建议在 GitHub 上开启分支保护（见第 6 节） |
| `feature/<简短英文名>` | 新功能，如 `feature/ltx-preview` | 从最新 `main` 拉出，完成后发 PR |
| `fix/<简短英文名>` | 修 bug，如 `fix/subtitle-drift` | 同上 |
| `docs/<简短英文名>` | 只改文档 | 同上 |

```bash
git switch main && git pull
git switch -c fix/subtitle-drift
# …修改、提交…
git push -u origin fix/subtitle-drift      # 然后在 GitHub 上发 PR
```

合并后删除分支。需要同步 `main` 的新改动时用 `git merge main`（不要 rebase 已经推送过的分支）。

## 2. 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：`类型: 简述`，简述写清“做了什么”，正文写“为什么”。

| 类型 | 用于 |
|---|---|
| `feat` | 新功能（新命令、新模型、新阶段） |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `refactor` | 不改行为的重构 |
| `test` | 只改测试 |
| `ci` / `chore` | CI、依赖、杂项 |

例：`fix: 字幕在全硬切时间线上逐段漂移`。一次提交只做一件事，测试和代码一起提交。

## 3. 本地开发

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"     # Windows：.venv\Scripts\python.exe
.venv/bin/python -m ruff check .                          # 静态检查
.venv/bin/python -m pytest -q                             # 全部测试（不需要 GPU，约 2 分钟）
.venv/bin/python -m aidrama init-demo /tmp/demo && .venv/bin/python -m aidrama run /tmp/demo ep01 --mock --takes 1
```

`--mock` 不连接 ComfyUI、LLM、TTS，用占位素材把整条流水线跑一遍；每个提交给 ComfyUI 的工作流都会对照 `tests/data/object_info_comfyui_0.38.json` 做静态校验。

## 4. 改 ComfyUI 工作流时（`aidrama/graphs/`）

1. 参数以 ComfyUI 官方模板（`comfyui-workflow-templates` 包）为准；偏离官方值要在代码注释里写明原因。
2. 用到新节点时，把它加进节点快照 `tests/data/object_info_comfyui_0.38.json`（从 ComfyUI 的 `/object_info` 取）。
3. 重新导出示例工作流并一起提交：`python -m aidrama export-workflows workflows`（CI 会检查 `workflows/` 是否与代码同步）。
4. 用 ComfyUI 自己的校验跑一遍：`<ComfyUI>/.venv/bin/python scripts/comfy_validate.py --comfy <ComfyUI> --stub-models "workflows/*.json"`（CI 也会跑）。
5. 有 GPU 时，再跑 `aidrama smoke` 和示例工程，确认真机效果。

## 5. Pull Request

- 用模板填写：改了什么、为什么、怎么验证的。
- 必须通过 CI：ruff、shellcheck、PowerShell 5.1/7 语法检查、Ubuntu 与 Windows 上的 pytest、ComfyUI v0.38 工作流校验。
- 改了行为就同步改文档（`README.md`、`docs/`），并在 `CHANGELOG.md` 的“未发布”里记一笔。
- 合并方式：普通 merge（保留提交历史），不要 force push 到 `main`。

## 6. 建议的 GitHub 设置（仓库所有者在网页上操作一次）

Settings → Branches → Add branch ruleset（或 Branch protection rule），对 `main`：

- Require a pull request before merging
- Require status checks to pass：勾选 CI 里的各项检查
- Block force pushes、Restrict deletions

## 7. 发布

版本号用日历版本 `YYYY.MM.N`（与模型生态的更新节奏一致），例如 `2026.10.0`、`2026.10.1`、`2026.11.0`。

1. 在 `main` 上确认 CI 全绿。
2. 把 `CHANGELOG.md` 的“未发布”改成新版本号和日期；同步 `pyproject.toml` 的 `version`。
3. 提交（`chore: release 2026.10.1`）并合并到 `main`。
4. 打 tag 并推送：`git tag -a v2026.10.1 -m "aidrama 2026.10.1" && git push origin v2026.10.1`。
5. 在 GitHub 上基于该 tag 创建 Release，正文贴 CHANGELOG 对应段落。

## 8. 不进仓库的东西

模型权重、Python 环境、生成的视频/音频/图片、安装脚本生成的本机文件（`.stack.env`、`aidrama.bat`、`aidrama.sh`、`fonts/`）都已在 `.gitignore` 里。
每部剧的工程目录（`projects/`）建议单独建 Git 仓库管理，见 [docs/03-工作流SOP.md](docs/03-工作流SOP.md) 的“用 Git 管理剧集工程”。
