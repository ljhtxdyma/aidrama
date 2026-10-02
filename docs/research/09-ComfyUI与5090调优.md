# 09-ComfyUI与5090调优

> 原始调研笔记（2026-10-02，云端调研时整理）。正文结论已汇总进 docs/01-调研报告.md；这里保留细节与出处，标“未确认”的地方没有独立核实。

## 综述

**调研方式说明（先说清楚）：** 你要求做 20 次以上 WebSearch，但只做成了 4 次。第 5 次起系统返回 "this session has used its web search budget (200 of 200)"，整个会话的搜索额度已经用完。我改用别的办法补足：约 45 次 WebFetch（主要是 GitHub 上的 issue、PR、discussion 和 PyPI）；直接克隆并阅读了 ComfyUI master（commit `65787d6`，2026-10-01）、Comfy-Org/docs（docs.comfy.org 的源文件）、workflow_templates、comfy-kitchen 和 comfy-aimdo 的源码；本地装了 huggingface_hub 2.1.1、modelscope 1.40.1 和 comfy-cli 1.22.0，核对了实际命令。docs.comfy.org、blog.comfy.org、hf-mirror.com、modelscope.cn、reddit 和 PyTorch 的 wheel 索引都被网络代理拦截，B站和知乎没有覆盖到。下文凡是"未确认"或"估算"的地方都会标出来。

**版本线。** master 的 `comfyui_version.py` 写的是 0.38.0，docs 的 changelog 另外列出了 v0.38.1（2026-09-30，修复 Qwen-Image 2.1 选 int8/int4 cache 时崩溃）。0.27 到 0.38 这三个月，主线是三件事。

1. **量化格式原生化**
   - v0.27.0（6-30）原生支持 int8 convrot。它是 INT8 W8A8 量化，量化前先对权重做分组 Hadamard 旋转，把 DiT 里的离群值压平。Turing 及以上显卡都能跑，社区说法是"画质接近 BF16，速度不低于 fp8_scaled"。
   - 之后陆续加了 convrot int4（v0.28）、asym w4a8（v0.31）、w6a8（v0.38）。
   - nvfp4 和 mxfp8 只在 SM≥10 的卡上走原生算力，5090 是 sm_120，满足。
   - 官方模板（H3、FastH3、LTX-2.5、Qwen-Image 2.1、Wan-Animate 2、SeedVR2、Qwen-Edit-2511 int8）已经全部换成 `*_int8_convrot` 文件；H3 的文本编码器是 `qwen3vl_32b_minimax_h3_nvfp4_awq`。
   - main.py 启动时会直接提示：原生的 fp8/int8/w4a8 格式"即使大于显存也会比 gguf 更快"。

2. **内存管理重构**
   - comfy-aimdo（AI Model Dynamic Offloader）是一个自定义的 PyTorch 显存分配器。它用 `cuMemAddressReserve/cuMemCreate/cuMemMap` 给每个模型预留虚拟地址（VBAR），权重用到时才"缺页"调入显存，显存紧张时按优先级逐出。
   - Dynamic VRAM 从 v0.16（2026-03）起默认开启。
   - "RAM pressure cache"从 v0.3.68 起是默认的节点结果缓存：内存余量低于阈值时才逐出缓存。
   - v0.35 加入 Comfy Compiler（aimdo 内存编译器 + CUDA Graphs）。
   - v0.36/0.37 加入 fast-disk：检测到快速 NVMe 时自动开启，权重直接从磁盘流式加载、不再常驻内存；同时提供 `--disable-fast-disk` 关闭它。

3. **内置加速内核**
   - comfy-kitchen 是官方内核库：fp8/nvfp4/mxfp8/int8/int4 矩阵乘、INT8 注意力、稀疏注意力、RoPE/AdaLN 融合算子都在里面。
   - 它的 CUDA wheel 要求 CUDA runtime ≥13.0、驱动 r580+。这就是 README 写"20 系及以上必须用 cu130"的原因。
   - v0.32 加入 comfy-kitchen INT8 注意力（`--use-ck-attention` 或 ModelAttentionBackend 节点）。
   - v0.35 加入 Model Sparse Attention 节点（sol-attn、SLA，以及 FastH3 用的 VSA）。
   - v0.38 允许模型文件逐 block 声明注意力实现，目前 MiniMax H3 和 Qwen-Image 2.1 已经内置 INT8 注意力声明。

**对你的 5090 + 128GB 的结论：**
- **环境**：Python 3.13 + torch 2.14.1+cu130。
- **SageAttention 不再是必需项**：官方 FastH3 模板本身就是"ck 注意力 + VSA 稀疏"。在 Windows 上，SageAttention2 和 aimdo 的钩子还出现过启动崩溃。
- **128GB 内存够用**：H3 全套约 41GB，能全部 pin 在内存里。
- **重度多模型流水线更推荐原生 Linux**：源码里 Windows 的 pinned 内存上限是内存的 40%（约 51GB），Linux 上同一台机器约 112GB。H3 加 LTX-2.5 两套模型同时常驻，Linux 放得下，Windows 放不下。
- **5090 上 H3 的实测量级**（约 5 秒、1152×896）：turbo 4 步 71 秒，PDD 8 步 126 秒，原版 20 步 288 秒。

## 推荐环境清单

### 版本基线

| 组件 | 推荐 | 依据 |
|---|---|---|
| ComfyUI | 0.38.x（master `comfyui_version.py`=0.38.0；changelog 另列 v0.38.1） | 源码、changelog |
| Python | **3.13**（官方推荐，portable 打包 3.13.14）；custom node 装不上时退回 3.12；3.14 可用但部分节点有问题 | [system_requirements](https://github.com/Comfy-Org/docs/blob/main/installation/system_requirements.mdx)、`release-stable-all.yml` |
| PyTorch | **2.14.1 + cu130**（PyPI 2026-09-30 发布）；nightly 用 cu132 | README |
| comfy-kitchen / comfy-aimdo | 0.2.36 / 0.5.5（requirements 固定版本） | requirements.txt |
| ComfyUI-Manager | `comfyui_manager==4.2.2`（manager_requirements 固定；PyPI 最新 4.3） | manager_requirements.txt |
| comfy-cli | 1.22.0（2026-09-30） | PyPI |
| 驱动 | ≥ r580（comfy-kitchen 预编译 wheel 的要求）；Windows 用当前 610.x；Linux 用 `-open` 内核模块（Blackwell 必须） | comfy-kitchen README |
| triton-windows | 3.8.0.post29（2026-09-28），可选 | PyPI |

### Windows 11 方案

**关键坑：PyPI 上的 Windows torch 是 CPU 版。** `torch-2.14.1-cp313-win_amd64.whl` 只有 124MB，Linux 版是 555MB 且依赖 cuda-toolkit 13.0.3。所以在 Windows 上必须走 PyTorch 的 cu130 索引。

方案 A：官方 Portable，最省心。
```bat
:: 下载 ComfyUI_windows_portable_nvidia.7z（Python 3.13 + cu130），解压到短路径，例如 D:\AI\CP
cd /d D:\AI\CP
update\update_comfyui_stable.bat
python_embeded\python.exe -m pip install -r ComfyUI\manager_requirements.txt
python_embeded\python.exe -c "import torch;print(torch.__version__,torch.version.cuda,torch.cuda.get_device_capability())"
:: 期望输出 2.1x.x+cu130 (12, 0)
:: 改 run_nvidia_gpu.bat：
python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --enable-manager --disable-fast-disk --reserve-vram 1.5 --preview-method auto
```

方案 B：手动 venv，适合做 API 自动化和锁定版本。
```powershell
winget install Python.Python.3.13 Git.Git
git clone https://github.com/Comfy-Org/ComfyUI D:\AI\ComfyUI; cd D:\AI\ComfyUI
git checkout v0.38.0          # 或最新的稳定 tag
py -3.13 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install torch==2.14.1 torchvision --index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt -r manager_requirements.txt
# 可选（只有 triton 后端、torch.compile、sage 才需要）：pip install "triton-windows<3.9"
python main.py --enable-manager --disable-fast-disk --reserve-vram 1.5 --preview-method auto
```

- 核心从 v0.38 起不再依赖 torchaudio。只有 InfiniteTalk 相关的自定义节点需要时，再从 cu130 索引补装。
- Desktop（comfy.org/download）现在是一个"多安装实例管理器"，官方对新手首推。但做批量生产和 API 调用，建议用 A 或 B，方便固定版本。

### Linux 方案（Ubuntu 24.04 LTS 原生，推荐给生产）

```bash
sudo ubuntu-drivers install            # 或 sudo apt install nvidia-driver-5xx-open（≥580，必须是 open 版）
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/Comfy-Org/ComfyUI ~/ComfyUI && cd ~/ComfyUI
uv venv -p 3.13 .venv && source .venv/bin/activate
uv pip install torch==2.14.1 torchvision   # Linux 的 PyPI 轮子就是 cu130（自带 CUDA 13.0.3、cuDNN 9.24、triton 3.8）
uv pip install -r requirements.txt -r manager_requirements.txt
python main.py --listen 0.0.0.0 --port 8188 --enable-manager --disable-fast-disk --preview-method auto
```

- 用 systemd 托管时加 `Restart=on-failure`，模型放在 ext4 的 NVMe 上。
- 不需要单独装 CUDA Toolkit，除非你要自己编译 sage 或 flash-attn。
- 也可以用 comfy-cli 一键装：`pip install comfy-cli==1.22.0 && comfy --workspace ~/ComfyUI install --nvidia --cuda-version 13.0 --version latest --fast-deps`，然后 `comfy launch -- --listen 0.0.0.0 --disable-fast-disk`。
- **WSL2 可用，但不如原生**：从 v0.33.1 起 WSL 下 dynamic VRAM 保持开启。需要在 `.wslconfig` 里把内存调高（例如 `memory=112GB`），因为默认只给宿主机内存的 50%。模型不要放在 `/mnt/c`。

## 启动参数与性能调优

**推荐参数：**

| 参数 | 建议 | 原因 |
|---|---|---|
| `--disable-fast-disk` | **128GB 内存建议加** | fast-disk 会跳过内存 pin，放不下显存的权重每一步都从 SSD 重读。#16415（5090+192GB）实测从 36 s/it 退化到 50–90 s/it |
| `--reserve-vram N` | Windows 1.5–4；Linux 无头一般不用 | Windows 默认只留约 700MB。#14157：5090 + nvfp4 时 VAE decode 从 10 秒恶化到 150–300 秒，多预留 4GB 后解决 |
| `--vram-headroom N` | 同时开剪映、达芬奇等时设 2–4 | 让 DynamicVRAM 额外保留空闲显存（会把其他程序占用的显存也算进去） |
| `--preview-method auto` | 可开 | H3 有 TAESD H3 预览（v0.34） |
| `--enable-manager` | 开 | 新版 Manager 已是核心里的 pip 包 |
| `--cache-ram [活跃GB] [非活跃GB]` | 保持默认 | 默认活跃阈值取内存 10%（2–10GB），非活跃取 100%（上限 128GB） |

**不要用、或只在排障时用：**

| 参数 | 说明 |
|---|---|
| `--highvram`、`--gpu-only`、`--novram`、`--cpu` | **会直接关闭 dynamic VRAM**（`enables_dynamic_vram()` 的源码逻辑），H3 和 LTX 很容易 OOM |
| `--lowvram` | 开着 dynamic VRAM 时无效 |
| `--disable-dynamic-vram` | 官方警告这个参数"将被移除"，只在最后排障时用 |
| `--disable-cuda-malloc` | 不推荐；cu130 及以上默认就开 cudaMallocAsync |
| `--disable-comfy-compiler` | 只在出现 `aimdo memory compile error: could not start recording` 时用（#16342，5090 Win11 上出现过） |
| `--disable-pinned-memory` | 只在出现 `HostBuffer.read_file_slice failed` 时用（#15255、#14250） |

**注意力怎么选：**
- H3 和 Qwen-Image 2.1 的模型文件已经逐 block 声明了 INT8 注意力，不用额外操作。
- 其他模型（Wan、LTX）可以在工作流里加 **ModelAttentionBackend** 节点选 "comfy kitchen attention"；v0.37 说明 Wan 用它能降低峰值显存。
- `--use-ck-attention` 是全局开关，作者说"might break some"，建议按工作流用节点开。
- 长序列可以加 **Model Sparse Attention**（`BlockSparseAttention`）：序列 ≥12288 token 才生效，越长收益越大，默认 start_percent=0.2。FastH3 用 `vsa`、keep 10%。
- `--use-sage-attention` 只对 Wan、InfiniteTalk 这类模型还有意义。Windows 上 sage2 和 aimdo 有启动崩溃的先例（aimdo #64）。
- SageAttention3 在核心里只注册成 `sage3` 函数，目前没有对应的命令行参数。

**编译：**
- 内置的 Comfy Compiler（内存编译器 + CUDA Graphs）默认开着。
- `TorchCompileModel` 节点会对该模型调用 `clone(disable_dynamic=True)`，也就是对它关掉 dynamic VRAM；配合 aimdo 还有 recompile 问题（discussion #12805）。一般不建议用。

**`--fast`：** 子项有 `fp16_accumulation`、`fp8_matrix_mult`、`cublas_ops`、`autotune`。对 int8 和 nvfp4 模型收益不明，`autotune` 只是开 cudnn.benchmark，可以自己 A/B 测。

**提速主要靠步数蒸馏：**
- H3：ref2v turbo 4 步、fl2v turbo 8 步、PDD 8 步、FastH3 8 步。
- LTX-2.5 distilled：8 步 + x2 潜空间放大后再 3 步。
- Wan：lightx2v 4 步。
- Qwen-Edit-2511：Lightning 4 步。
- Wan-Animate 2：加 `WanAnimate2Cache` 节点约快一倍（PR #15362）。

**H3 尺寸约束：** 宽高步长 32，所以 1280×720 不合法，要用 1280×704 或 1280×736。帧数按 17k+5 取值，24fps 下 124≈5 秒、243≈10 秒、362≈15 秒。

## 实测性能数据表

| 模型 / 设置 | 分辨率×帧 | 环境 | 结果 | 来源 |
|---|---|---|---|---|
| H3 int8，原版 20 步 res_multistep | 1152×896×124（约 5 秒） | **RTX 5090**，权重已热，pytorch 注意力 | **288 s** | [PR #15908](https://github.com/Comfy-Org/ComfyUI/pull/15908) |
| H3 + PDD LoRA 8 步 | 同上 | 5090 | **126 s** | 同上 |
| H3 + lightx2v Turbo 4 步 | 同上 | 5090 | **71 s**（约 13.5 s/步） | 同上 |
| H3 8 步（Larry v4 int8，CFG1） | 768×512×124 | 5090 D v2，torch 2.13 cu130 | 仅采样 **15.6 s** | [PR #16681](https://github.com/Comfy-Org/ComfyUI/pull/16681) |
| H3 4 步 + 稀疏注意力 | 768p×15 s | 5090 | 总 **183 s**（其中 dense 预热阶段约 96 s；dense 用 ck int8 时约 44 s） | [PR #16072](https://github.com/Comfy-Org/ComfyUI/pull/16072) |
| H3 R2V（带参考视频） | 960×544，输出 5.17 s | 5090，CUDA13 容器 | 总 168–238 s（采样 88–101 s），match 模式提速 9–21% | [PR #15933](https://github.com/Comfy-Org/ComfyUI/pull/15933) |
| H3 REF2VA int8 + LoRA | 未注明 | 5090，110GB 内存 | 总 **141 s**；pin 99GB；TE 15.0GB、DiT 20.0GB、视频 VAE 5.0GB | [#15889](https://github.com/Comfy-Org/ComfyUI/issues/15889) |
| H3 T2V 20 步（v0.31.1） | 未注明 | 5090 Win11，192GB | 20 步用时 1:09（约 3.45 s/it）；第二次跑掉到 10 s/it，随后驱动崩溃 | [#15480](https://github.com/Comfy-Org/ComfyUI/issues/15480) |
| H3 20 步（对照：16GB 卡） | 1280×736×362 | 5070 Ti | 79 s/步，共 26 分钟 | [#15665](https://github.com/Comfy-Org/ComfyUI/issues/15665) |
| LTX-2.5 22B distilled int8 | 768×448×49 | 5090 Win，64GB | 采样约 **2.96 it/s**；VAE 解码阶段崩溃（DiT 没被逐出） | [#15606](https://github.com/Comfy-Org/ComfyUI/issues/15606) |
| LTX/Wan 融合内核 | — | 5090 | 约快 2.5–2.6% | [PR #15138](https://github.com/Comfy-Org/ComfyUI/pull/15138) |
| fast-disk（Linux，NVMe） | — | 5090 | 4.27 s/it，峰值内存 21.5GB 降到 2.4GB | [PR #16333](https://github.com/Comfy-Org/ComfyUI/pull/16333) |
| Wan2.2 14B + nvfp4 + lightx2v | — | 5090 Win | VAE 解码 150–300 s，多预留 4GB 后约 10 s | [#14157](https://github.com/Comfy-Org/ComfyUI/issues/14157) |

**按上表推算（估算，未实测）：**
- H3 1280×704、5 秒、turbo 4 步，热启动约 70–90 秒。
- 同设置 10 秒（243 帧），因为注意力开销近似随长度平方增长，估计 3–4.5 分钟。

**没找到 2026 年 5090 可靠实测的项目：** Wan2.2 lightx2v 4 步 720p、SeedVR2 3B/7B int8 超分、FastH3（VSA）、LTX-2.5 模板默认的 1280×720×5 秒两段式。

## API 自动化要点

**接口用法（来自 server.py 源码）：**
- **POST `/prompt`**
  - 请求体：`{"prompt": API格式JSON, "client_id": cid, "prompt_id": 可选UUID}`。现在允许客户端自带 prompt_id，必须是小写标准 UUID，可用来做幂等。
  - 可选 `"front": true` 插队。
  - 返回 `{prompt_id, number, node_errors}`；校验失败返回 400 和 `node_errors`。
- **WebSocket `/ws?clientId=cid`**
  - 消息类型：`execution_start`、`executing`、`progress`、`progress_state`、`executed`、`execution_cached`、`execution_success`、`execution_error`、`execution_interrupted`。
  - 完成判定：`executing` 消息里 `node` 为 `null` 且 prompt_id 匹配。二进制帧是预览图，跳过即可。
- **GET `/history/{prompt_id}`**
  - **SaveVideo 的输出也放在 `outputs[nid]["images"]` 里**（另带 `"animated":[true]`），音频在 `"audio"` 里。
  - 下载用 `/view?filename=&subfolder=&type=output`。
- **POST `/upload/image`**
  - multipart 字段：`image`、`type=input`、`subfolder`、`overwrite=true`。
  - 返回 `{name, subfolder, type}`，LoadImage 填 `"subfolder/name"`。
  - 服务端按原样写入字节，不校验类型，所以视频、音频也能用这个接口上传到 input 目录。
- **POST `/free`**：请求体 `{"unload_models": true, "free_memory": true}`。注意它只是设置标志，要等当前任务结束、worker 空闲时才执行。`free_memory` 还会清掉执行缓存，文本编码结果需要重算。
- **其他**：`/interrupt {"prompt_id"}`；`/api/jobs?status=pending,in_progress`；`POST /api/jobs/{id}/cancel`；`POST /history {"clear":true}`；`/object_info/<节点类名>` 可查输入定义。

**V3 动态输入在 API 格式里的写法**（用 `.` 拼接路径，见 `_io.py` 的 `finalize_prefix`）：
```json
"20": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
  "clip": ["3",0], "vae": ["4",0], "audio_vae": ["5",0],
  "prompt": "... <Picture 1> ...", "width": 1280, "height": 704, "length": 124,
  "ref_image_size": "match",
  "ref_images.ref_image_0": ["11",0], "ref_images.ref_image_1": ["12",0]}},
"30": {"class_type": "SaveVideo", "inputs": {"video": ["29",0], "filename_prefix": "ep01/s03",
  "format": "mp4", "format.codec": "h264",
  "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 18}},
"40": {"class_type": "BlockSparseAttention", "inputs": {"model": ["10",0],
  "selection": "vsa", "selection.keep_percent": 10, "start_percent": 0.2, "end_percent": 1.0}}
```
- Autogrow 的名字是"前缀 + 序号"：H3 的 `ref_image_0..8`、`ref_video_0..2`、`ref_audio_0..2`。
- SaveVideo 仍兼容老的顶层隐藏 `codec` 字段。
- 最稳的做法是先在 UI 里用 File → Export Workflow (API) 导出，再改参数。
- 官方模板大量使用子图，导出后节点 ID 可能带父 ID 前缀，脚本不要假设 ID 是纯数字（未确认具体格式）。

**排队与显存释放的建议：**
- 一张卡只跑一个 ComfyUI 进程，靠服务端队列串行执行。
- 任务按模型分组跑：先跑完一批 H3，切换到 LTX 之前调一次 `/free {"unload_models":true}`。
- 不要用 `--cache-none`。H3 的 32B 文本编码器结果可以被缓存，只换 seed 时会跳过编码。
- 同一个工作流原样重复提交不会重新执行，必须改 seed 或其他输入。
- 定期清 history。#15759 报告过多轮运行后内存增长，建议长跑时加守护进程定时重启（经验做法）。

## 模型下载（国内镜像）

**HuggingFace CLI（huggingface_hub 2.1.1 实测）：**
- 2.x 只提供 `hf` 命令，`huggingface-cli` 已经没有了。
- `HF_HUB_ENABLE_HF_TRANSFER` 已废弃，会报 FutureWarning。高速传输改用 `HF_XET_HIGH_PERFORMANCE=1`；`HF_HUB_DISABLE_XET=1` 可退回普通 HTTP 下载。
- 断点续传：重复执行同一条命令即可，未完成的文件会续传。

```bash
pip install -U "huggingface_hub>=2.1" modelscope
export HF_ENDPOINT=https://hf-mirror.com          # PowerShell 写法：$env:HF_ENDPOINT="https://hf-mirror.com"
# Comfy-Org 仓库的目录结构和 ComfyUI/models 一致，可以直接下到 models 目录：
hf download Comfy-Org/MiniMax-H3 \
  diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
  diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors \
  text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
  vae/minimax_h3_video_vae_int8_convrot.safetensors vae/minimax_h3_audio_vae_fp32.safetensors \
  loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors \
  --local-dir ComfyUI/models --max-workers 8
# ModelScope 写法：
modelscope download Comfy-Org/MiniMax-H3 text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors --local-dir ComfyUI/models
```

**官方模板里列出的 ModelScope 镜像仓库**（Comfy-Org 名下）：MiniMax-H3、Qwen-Image-2.1、Wan-Animate-2、SeedVR2、gemma-4、Qwen-Image-Edit_ComfyUI、Qwen-Image_ComfyUI、Wan_2.1_ComfyUI_repackaged、Wan_2.2_ComfyUI_Repackaged、HunyuanVideo_1.5_repackaged（Qwen-Edit-2511 用的 qwen_2.5_vl_7b 文本编码器在这里）、ltx-2.3。

**模板里只给了 HF 链接的文件：**
- LTX-2.5：`Lightricks/LTX-2.5`，文件为 `ltx-2.5-22b-distilled-transformer-comfy-int8-convrot`、`gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot`，另需 `Comfy-Org/gemma-4` 里的 `gemma4_e2b_it_int8_convrot`。
- FastH3：`FastVideo/FastVideo-FastH3-Comfy`。
- H3 的 fl2v turbo 8 步 LoRA：`lightx2v/Minimax-h3-Turbo`。
- 这几个在 ModelScope 上有没有镜像，未确认。

**目录注意：** `split_files/...` 结构的仓库（Wan、Qwen 的 repackaged 系列）下载后会多一层 `split_files` 目录，需要手动移动，或者用 `extra_model_paths.yaml` 指过去。

## 常见坑

1. **Windows 报 "Torch not compiled with CUDA"**：从 PyPI 装到了 CPU 版 torch，改用 cu130 索引重装。
2. **nvfp4 被上转成 fp16（约 28GB），或 int8 很慢**：torch 不是 cu13x 时，comfy-kitchen 的 CUDA 后端加载不了（#11864）。
3. **LTX-2.5 nvfp4 文件报 `mat1 and mat2 shapes`**：文件缺少 `_quantization_metadata`（#15511）。改用官方 int8_convrot 文件。
4. **高内存机器上越更新越慢**：自动 fast-disk 造成的，加 `--disable-fast-disk`（#16415）。
5. **H3 报 `could not start recording`**：加 `--disable-comfy-compiler`（#16342）。
6. **Windows 上 LTX-2.5 在 VAE 解码阶段 access violation**：DiT 没被逐出（#15606）。在采样和解码之间清一次缓存，或加大 `--reserve-vram`。
7. **"GPU is lost"、TDR 黑屏、功耗超过 80% 时驱动崩溃**：见 #15488、#15480。
   - #15488 是 5070 Ti + 64GB 内存；Windows 用 bcdedit 限制只用 32GB 后，连跑 29 次没再崩。
   - 可以试的方向：给 5090 设功耗上限（例如 `nvidia-smi -pl 500`）、检查电源和 12V-2x6 线、换 Linux（经验做法，未确认）。
8. **装了 SageAttention2 后 Windows 启动崩溃（Hook cuMemAlloc_v2 failed）**：aimdo #64。卸载 sage 即可；`--disable-dynamic-vram` 也能绕过，但不推荐。
9. **`HostBuffer.read_file_slice failed`**：加 `--disable-pinned-memory`，或者升级 aimdo。
10. **更新后显存或内存暴涨**（#16140、#16150、#16705，后者是 64GB 内存跑 H3 OOM）：生产环境锁定 tag，用 `update_comfyui_stable.bat` 更新，不要追 master。
11. **ck INT8 注意力报 `4-element alignment`**（#15494）：在 ModelAttentionBackend 里改回 pytorch attention。H3 + Larryvrh LoRA + 稀疏注意力会出伪影（#16382）。
12. **Blackwell 上 Qwen3 系文本编码器偶发 NaN、出黑图**（#15110，Z-Image）：重启，或把文本编码器放到 CPU。
13. **KJNodes 等自定义节点和核心版本不匹配，LTX 节点整块消失**（discussion #15204）：核心和节点一起更新。
14. **Windows 路径**：开启长路径支持（`LongPathsEnabled=1`、`git config --system core.longpaths true`），安装到短的纯英文路径；给 ComfyUI 和模型目录加 Defender 排除项，避免扫描 20GB 级的 safetensors 文件（经验做法）。
15. **Windows 的 pinned 内存上限是 40%**：128GB 机器上同时常驻 H3 和 LTX 会触发反复从磁盘重读。可以换 Linux，或者按模型分批跑。
16. **前端**：Chrome 需 ≥143，否则有渲染问题。

## 不确定项

- WebSearch 只做了 4 次（会话额度用尽），B站、知乎、Reddit 的中文社区实测数据没有覆盖。
- Wan2.2 lightx2v 4 步、SeedVR2、FastH3、LTX-2.5 两段式在 5090 上的实际耗时，都没找到 2026 年的可靠数据。
- torch 2.14.1 有没有稳定版 cu132 轮子未确认（PyTorch 索引被拦截）；triton-windows 3.8 对应 torch 2.14 是我按 Linux 依赖 `triton~=3.8.0` 推断的。
- hf-mirror 对 Xet 存储的支持情况未确认；ModelScope 上 Comfy-Org 镜像是否与 HF 内容完全一致未确认。
- v0.38.1 的 tag 是否已经合回 master 未确认（master 的版本文件仍是 0.38.0）。
- Windows 和 Linux 之间没有找到同机对比的跑分。"Linux 更好"的结论来自源码里的 pinned 上限，以及 Windows 特有崩溃 issue 的分布。
- int8_convrot "速度不低于 fp8" 来自社区说法；在 A100 上反而只有 fp8 的 33–50%（#14824），5090 上没有官方对比数据。
- #16705、#16150、#16342 等问题截至 10-02 仍未见官方修复。
- 子图导出成 API 格式后的节点 ID 具体格式未确认。

**主要来源：**
- 源码：[ComfyUI 源码](https://github.com/Comfy-Org/ComfyUI)（`comfy/cli_args.py`、`model_management.py`、`main.py`、`server.py`、`comfy_api/latest/_io.py`、`comfy_extras/nodes_video.py`、`nodes_minimax_h3.py`、`nodes_sparse_attention.py`、`nodes_model_advanced.py`）
- 文档：[Changelog](https://github.com/Comfy-Org/docs/blob/main/changelog/index.mdx)、[启动参数文档](https://github.com/Comfy-Org/docs/blob/main/development/comfyui-server/startup-flags.mdx)、[server 路由文档](https://github.com/Comfy-Org/docs/blob/main/development/comfyui-server/comms_routes.mdx)
- 模板与组件：[workflow_templates](https://github.com/Comfy-Org/workflow_templates/tree/main/templates)、[comfy-kitchen](https://github.com/Comfy-Org/comfy-kitchen)、[PyPI comfy-kitchen](https://pypi.org/project/comfy-kitchen/)、[comfy-aimdo](https://github.com/Comfy-Org/comfy-aimdo)、[aimdo #64](https://github.com/Comfy-Org/comfy-aimdo/issues/64)、[discussion #12805](https://github.com/Comfy-Org/ComfyUI/discussions/12805)
- PR：[#15908](https://github.com/Comfy-Org/ComfyUI/pull/15908)、[#16681](https://github.com/Comfy-Org/ComfyUI/pull/16681)、[#16072](https://github.com/Comfy-Org/ComfyUI/pull/16072)、[#15933](https://github.com/Comfy-Org/ComfyUI/pull/15933)、[#16333](https://github.com/Comfy-Org/ComfyUI/pull/16333)、[#15138](https://github.com/Comfy-Org/ComfyUI/pull/15138)、[#15861](https://github.com/Comfy-Org/ComfyUI/pull/15861)、[#15479](https://github.com/Comfy-Org/ComfyUI/pull/15479)、[#16419](https://github.com/Comfy-Org/ComfyUI/pull/16419)、[#15362](https://github.com/Comfy-Org/ComfyUI/pull/15362)
- Issue：[#15889](https://github.com/Comfy-Org/ComfyUI/issues/15889)、[#15480](https://github.com/Comfy-Org/ComfyUI/issues/15480)、[#15606](https://github.com/Comfy-Org/ComfyUI/issues/15606)、[#16415](https://github.com/Comfy-Org/ComfyUI/issues/16415)、[#15665](https://github.com/Comfy-Org/ComfyUI/issues/15665)、[#14157](https://github.com/Comfy-Org/ComfyUI/issues/14157)、[#16342](https://github.com/Comfy-Org/ComfyUI/issues/16342)、[#15488](https://github.com/Comfy-Org/ComfyUI/issues/15488)、[#15255](https://github.com/Comfy-Org/ComfyUI/issues/15255)、[#16705](https://github.com/Comfy-Org/ComfyUI/issues/16705)、[#11864](https://github.com/Comfy-Org/ComfyUI/issues/11864)、[#15511](https://github.com/Comfy-Org/ComfyUI/issues/15511)、[#15494](https://github.com/Comfy-Org/ComfyUI/issues/15494)、[#14824](https://github.com/Comfy-Org/ComfyUI/issues/14824)、[#15110](https://github.com/Comfy-Org/ComfyUI/issues/15110)
- 其他：[ConvRot 论文](https://arxiv.org/html/2512.03673v1)、[Dynamic VRAM 博文](https://blog.comfy.org/p/dynamic-vram-in-comfyui-saving-local)（只看到标题，正文被拦截）、[SageAttention](https://github.com/thu-ml/SageAttention)、[woct0rdho/SageAttention releases](https://github.com/woct0rdho/SageAttention/releases)

本地克隆的源码和文档在 `<调研时的本地缓存>`（`comfyui/`、`comfydocs/`、`wt/templates/`、`comfy-kitchen/`、`comfy-aimdo/`）。