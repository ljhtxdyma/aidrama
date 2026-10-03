# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号为日历版本 `YYYY.MM.N`。

## [未发布]

## [2026.10.0] - 2026-10-03

首个版本：在 RTX 5090 32GB + 128GB 内存上本地完成 AI 真人短剧全流程。

### 新增
- 编排器 `aidrama`：一句话创意 → 剧集圣经 → 分镜 → 定妆照/三视图设定图/场景图 → 角色音色 → 逐句情绪配音 → 生成段规划 → 关键帧 → MiniMax H3 抽卡 → 自动质检与选条 → SeedVR2 超分 → 配乐 → 合成成片。
- 模型栈：MiniMax H3（FL2VA / Ref2VA）、Qwen-Image-2512 / Qwen-Image-Edit-2511、IndexTTS-2.5、Qwen3-TTS VoiceDesign、Qwen3-ASR + ForcedAligner、SeedVR2 7B、MiniMax Music 3；兜底 InfiniteTalk、Wan Animate 2。
- 三档预设 `quality` / `balanced` / `draft`，参数对照 ComfyUI 官方模板。
- H3 提示词确定性编译与校验（台词逐字锁定、切镜时间、听者闭嘴），可选本地 LLM 扩写并自动回退；支持 `.manual.txt` 人工覆盖。
- 自动质检：时长、音轨、黑场、冻帧、切镜时间、对白字错率；审片页 `review.html`；`pick` 人工选条；`add-take` 登记外部视频。
- 合成：转场、BGM 对白闪避、-14 LUFS、字幕避开平台 UI、GB 45438-2025 显式标识与 `AIGC` 元数据。
- 单卡显存轮转：ComfyUI / 音频服务 / Ollama 分阶段占用显卡。
- Windows / Linux 一键安装、启动、停止脚本；断点续传 + 国内镜像回退的模型下载器；`doctor` 体检；`smoke` 真机冒烟测试。
- 13 个可直接导入 ComfyUI 的示例工作流；示例工程《第七封信》。
- 文档：调研报告、安装部署、工作流 SOP、提示词指南、质检与返工、常见问题、合规与授权，以及 10 篇原始调研笔记。
- CI：ruff、shellcheck、PowerShell 5.1/7 语法、Ubuntu 与 Windows 上的测试、ComfyUI v0.38 工作流校验。

[未发布]: https://github.com/ljhtxdyma/aidrama/compare/v2026.10.0...HEAD
[2026.10.0]: https://github.com/ljhtxdyma/aidrama/releases/tag/v2026.10.0
