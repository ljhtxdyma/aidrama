<#
aidrama 一键安装（Windows 10/11 + RTX 5090 32GB + 128GB 内存）

在仓库根目录打开 PowerShell 运行：
  powershell -ExecutionPolicy Bypass -File install\windows\install.ps1
国内网络推荐：
  powershell -ExecutionPolicy Bypass -File install\windows\install.ps1 -Source modelscope -PipMirror tuna

会安装到 -StackDir（默认 D:\aidrama-stack，没有 D 盘则用 %USERPROFILE%\aidrama-stack）：
  ComfyUI\            ComfyUI v0.38.2 + Python 3.13 + PyTorch cu130（.venv）
  index-tts\          IndexTTS-2.5 对白配音（uv 环境 + checkpoints）
  qwen-audio\         Qwen3-TTS 音色设计 + Qwen3-ASR 识别/对齐（.venv + models）
仓库内：
  .venv\              aidrama 编排器
  fonts\              思源黑体（字幕烧录）
  .stack.env          记录上述路径，start_all.ps1 读取

可以重复运行：已完成的步骤会跳过，模型下载支持断点续传。
#>
param(
    [string]$StackDir = "",
    [string[]]$Groups = @("core", "fast"),     # -Groups core,fast,lipsync 会被 PowerShell 当成数组，这里统一接收
    [ValidateSet("auto", "huggingface", "hf-mirror", "modelscope")][string]$Source = "auto",
    [ValidateSet("", "tuna", "aliyun")][string]$PipMirror = "",
    [string]$ComfyTag = "v0.38.2",
    [switch]$SkipModels,
    [switch]$SkipAudio,
    [switch]$SkipLLM
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# Windows PowerShell 5.1 在部分 Win10 上默认 TLS 1.0，GitHub 下载会失败
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$GroupList = (($Groups -join ",") -split "[,\s]+" | Where-Object { $_ }) -join ","
$Failed = New-Object System.Collections.Generic.List[string]
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
if (-not $StackDir) {
    if (Test-Path "D:\") { $StackDir = "D:\aidrama-stack" } else { $StackDir = Join-Path $env:USERPROFILE "aidrama-stack" }
}
New-Item -ItemType Directory -Force -Path $StackDir | Out-Null
$StackDir = (Resolve-Path $StackDir).Path
$Comfy = Join-Path $StackDir "ComfyUI"
$IndexTTS = Join-Path $StackDir "index-tts"
$QwenAudio = Join-Path $StackDir "qwen-audio"

function Step($m) { Write-Host "`n==== $m ====" -ForegroundColor Cyan }
function Ok($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Run {
    # 普通函数（不是高级函数），这样 --python / -m 这类参数会原样进入 $args 传给外部程序
    $exe = $args[0]
    $rest = @()
    if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "命令失败（退出码 $LASTEXITCODE）：$exe $($rest -join ' ')" }
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") + ";" + "$env:USERPROFILE\.local\bin"
}
function Need($cmd, $wingetId) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Warn "未找到 $cmd，使用 winget 安装 $wingetId"
        winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
        Refresh-Path
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd 安装后仍不可用：请关闭并重新打开 PowerShell 后再运行本脚本" }
    }
    Ok "$cmd -> $((Get-Command $cmd).Source)"
}

function Write-StackEnv {
    @(
        "STACK_DIR=$StackDir",
        "COMFY_DIR=$Comfy",
        "INDEXTTS_DIR=$IndexTTS",
        "QWEN_AUDIO_DIR=$QwenAudio"
    ) | Set-Content -Path (Join-Path $Repo ".stack.env") -Encoding UTF8
}

$PyIndex = ""
if ($PipMirror -eq "tuna") { $PyIndex = "https://pypi.tuna.tsinghua.edu.cn/simple" }
if ($PipMirror -eq "aliyun") { $PyIndex = "https://mirrors.aliyun.com/pypi/simple" }
if ($PyIndex) { $env:UV_DEFAULT_INDEX = $PyIndex }
if ($Source -eq "hf-mirror" -or $Source -eq "modelscope") { $env:HF_ENDPOINT = "https://hf-mirror.com" }
$env:UV_LINK_MODE = "copy"
$TorchCu130 = "https://download.pytorch.org/whl/cu130"
$TorchCu128 = "https://download.pytorch.org/whl/cu128"

# ---------------------------------------------------------------- 0. 体检
Step "0/8 硬件与磁盘"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = (& nvidia-smi "--query-gpu=name,driver_version,memory.total" "--format=csv,noheader" 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or -not $gpu -or ($gpu -split ",").Count -lt 2) {
        Warn "nvidia-smi 运行失败（驱动可能没装好，或升级后需要重启）：$gpu"
    } else {
        Ok "GPU: $gpu"
        $drv = [int](($gpu -split ",")[1].Trim().Split(".")[0])
        if ($drv -lt 580) { Warn "驱动主版本 $drv < 580：请先到 nvidia.cn 安装最新 Game Ready / Studio 驱动（cu130 和 comfy-kitchen 需要 r580+）" }
    }
} else { Warn "找不到 nvidia-smi，请先安装 NVIDIA 驱动" }
$drive = (Get-Item $StackDir).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB)
if ($freeGB -lt 260) { Warn "$($drive.Name): 盘剩余 $freeGB GB；core+fast 模型约 160GB，加上音频/LLM/环境建议预留 260GB 以上" } else { Ok "$($drive.Name): 盘剩余 $freeGB GB" }
Ok "安装目录：$StackDir"

# ---------------------------------------------------------------- 1. 基础工具
Step "1/8 基础工具（git / ffmpeg / uv）"
Need git "Git.Git"
Need ffmpeg "Gyan.FFmpeg"
Need uv "astral-sh.uv"
Run uv python install 3.13 3.12 3.11

# ---------------------------------------------------------------- 2. ComfyUI
Step "2/8 ComfyUI $ComfyTag + PyTorch cu130"
if (-not (Test-Path (Join-Path $Comfy "main.py"))) {
    Run git clone --depth 1 --branch $ComfyTag https://github.com/comfyanonymous/ComfyUI.git $Comfy
} else { Ok "已存在 $Comfy（如需升级：cd $Comfy; git fetch --tags; git checkout $ComfyTag）" }
$ComfyPy = Join-Path $Comfy ".venv\Scripts\python.exe"
if (-not (Test-Path $ComfyPy)) { Run uv venv --python 3.13 (Join-Path $Comfy ".venv") }
Run uv pip install --python $ComfyPy torch torchvision torchaudio --index-url $TorchCu130
Run uv pip install --python $ComfyPy -r (Join-Path $Comfy "requirements.txt")
& $ComfyPy -c "import torch;print('  torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
if ($LASTEXITCODE -ne 0) { throw "PyTorch 检查失败" }
& $ComfyPy -c "import torch,sys;sys.exit(0 if torch.cuda.is_available() else 3)"
if ($LASTEXITCODE -ne 0) { Warn "PyTorch 看不到 GPU：请升级 NVIDIA 驱动到 r580+ 后重启，再重跑本脚本（否则 ComfyUI 会用 CPU，极慢）" }

# ---------------------------------------------------------------- 3. aidrama
Step "3/8 aidrama 编排器"
$AdPy = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $AdPy)) { Run uv venv --python 3.12 (Join-Path $Repo ".venv") }
Run uv pip install --python $AdPy -e "${Repo}[dev]"
Set-Content -Path (Join-Path $Repo "aidrama.bat") -Encoding ASCII -Value "@echo off`r`nset PYTHONUTF8=1`r`n`"%~dp0.venv\Scripts\python.exe`" -m aidrama %*"
# 尽早写路径配置：后面任何一步失败，start_all / stop_all 也能用
Write-StackEnv
Ok "命令入口：$Repo\aidrama.bat"

# ---------------------------------------------------------------- 4. 字体
Step "4/8 字幕字体（思源黑体 SC）"
$Fonts = Join-Path $Repo "fonts"
New-Item -ItemType Directory -Force -Path $Fonts | Out-Null
if (-not (Test-Path (Join-Path $Fonts "SourceHanSansSC-Bold.otf"))) {
    $zip = Join-Path $env:TEMP "SourceHanSansSC.zip"
    try {
        Invoke-WebRequest -Uri "https://github.com/adobe-fonts/source-han-sans/releases/download/2.004R/SourceHanSansSC.zip" -OutFile $zip
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [System.IO.Compression.ZipFile]::OpenRead($zip)
        foreach ($e in $z.Entries) {
            if ($e.Name -in @("SourceHanSansSC-Bold.otf", "SourceHanSansSC-Medium.otf")) {
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $Fonts $e.Name), $true)
            }
        }
        $z.Dispose()
        Ok "已放到 $Fonts"
    } catch { Warn "字体下载失败（$_），字幕会回退到微软雅黑；可手动把 SourceHanSansSC-Bold.otf 放进 $Fonts" }
} else { Ok "已存在" }

# ---------------------------------------------------------------- 5. ComfyUI 模型
Step "5/8 ComfyUI 模型（分组：$GroupList，来源：$Source）"
if ($SkipModels) { Warn "按参数跳过" } else {
    try { Run $AdPy (Join-Path $Repo "scripts\download_models.py") --comfy $Comfy --groups $GroupList --source $Source }
    catch { Warn "部分模型没下载成功（$_）；安装继续，稍后重跑本脚本或 download_models.py 会自动续传"; $Failed.Add("ComfyUI 模型") }
}

function Get-Repo($repoId, $dir) {
    # 每次都调用下载命令：已完整的文件会被跳过，上次中断的会补全（不能只看 config.json 在不在）
    $order = @("huggingface", "modelscope")
    if ($Source -eq "modelscope") { $order = @("modelscope", "huggingface") }
    foreach ($s in $order) {
        try {
            if ($s -eq "modelscope") { Run uvx --from modelscope modelscope download --model $repoId --local_dir $dir }
            else { Run uvx --from huggingface_hub hf download $repoId --local-dir $dir }
            return
        } catch { Warn "$s 下载 $repoId 失败：$_" }
    }
    Warn "无法下载 $repoId，安装继续；稍后重跑本脚本会补全"
    $Failed.Add($repoId)
}

# ---------------------------------------------------------------- 6. 音频
Step "6/8 音频：IndexTTS-2.5（配音） + Qwen3-TTS/ASR（音色设计、识别、对齐）"
if ($SkipAudio) { Warn "按参数跳过" } else {
    if (-not (Test-Path (Join-Path $IndexTTS "pyproject.toml"))) {
        Run git clone --depth 1 https://github.com/index-tts/index-tts.git $IndexTTS
    }
    Push-Location $IndexTTS
    try {
        # 不装 deepspeed / flash-attn：Windows 上难编译，对单句配音速度影响很小
        if ($PyIndex) { Run uv sync --extra webui --default-index $PyIndex } else { Run uv sync --extra webui }
    } finally { Pop-Location }
    Get-Repo "IndexTeam/IndexTTS-2.5" (Join-Path $IndexTTS "checkpoints")

    $QPy = Join-Path $QwenAudio ".venv\Scripts\python.exe"
    if (-not (Test-Path $QPy)) { Run uv venv --python 3.11 (Join-Path $QwenAudio ".venv") }
    Run uv pip install --python $QPy "torch==2.8.*" "torchaudio==2.8.*" --index-url $TorchCu128
    # qwen-asr 固定 transformers==4.57.6、qwen-tts 固定 4.57.3：装 asr 的版本，tts 用 --no-deps 共存（同一小版本，接口一致）
    Run uv pip install --python $QPy qwen-asr
    Run uv pip install --python $QPy --no-deps qwen-tts
    Run uv pip install --python $QPy "accelerate==1.12.0" librosa soundfile sox onnxruntime einops
    $M = Join-Path $QwenAudio "models"
    Get-Repo "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign" (Join-Path $M "Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    Get-Repo "Qwen/Qwen3-ASR-1.7B" (Join-Path $M "Qwen3-ASR-1.7B")
    Get-Repo "Qwen/Qwen3-ForcedAligner-0.6B" (Join-Path $M "Qwen3-ForcedAligner-0.6B")
}

# ---------------------------------------------------------------- 7. LLM
Step "7/8 本地 LLM（Ollama，写剧本/分镜/扩写 H3 提示词）"
if ($SkipLLM) { Warn "按参数跳过" } else {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        try { Need ollama "Ollama.Ollama" } catch { Warn "Ollama 安装失败：$_。也可以改用云端 LLM，见 docs/02-安装部署.md" }
    }
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $have = (& ollama list) -join "`n"
        if ($have -notmatch "aidrama-llm") {
            # 依次尝试：Qwen3.8-27B（2026-08，HF / ModelScope 的 GGUF）→ Qwen3.6-27B → Qwen3.5-27B（Ollama 官方库）
            $cands = @(
                "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL",
                "modelscope.cn/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL",
                "hf.co/unsloth/Qwen3.6-27B-GGUF:UD-Q5_K_XL",
                "qwen3.6:27b",
                "qwen3.5:27b"
            )
            $ok = $false
            foreach ($c in $cands) {
                Write-Host "  ollama pull $c"
                & ollama pull $c
                if ($LASTEXITCODE -eq 0) { & ollama cp $c aidrama-llm; Ok "aidrama-llm -> $c"; $ok = $true; break }
            }
            if (-not $ok) { Warn "没有拉取成功。请手动 ollama pull <任意中文能力强的模型> 后执行：ollama cp <模型名> aidrama-llm" }
        } else { Ok "aidrama-llm 已存在" }
        [Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "2m", "User")
        Ok "已设置 OLLAMA_KEEP_ALIVE=2m（流水线在生成画面前还会主动卸载 LLM）"
    }
}

# ---------------------------------------------------------------- 8. 收尾
Step "8/8 写入路径配置 + 自检"
Write-StackEnv
try { Run $AdPy -m aidrama fetch-guides } catch { Warn "H3 官方提示词指南下载失败（不影响运行）" }
Run $AdPy -m pytest -q (Join-Path $Repo "tests") -x
if ($Failed.Count) {
    Warn "以下内容没有下载成功，请稍后重跑本脚本（会自动续传）：$($Failed -join '; ')"
}
Write-Host ""
Ok "安装完成。下一步："
Write-Host "   1) 启动服务：  powershell -ExecutionPolicy Bypass -File install\windows\start_all.ps1"
Write-Host "   2) 体检：      .\aidrama.bat doctor ;  .\aidrama.bat smoke   （真机冒烟测试，约 10~15 分钟）"
Write-Host "   3) 示例工程：  .\aidrama.bat init-demo projects\demo ;  .\aidrama.bat run projects\demo ep01"
