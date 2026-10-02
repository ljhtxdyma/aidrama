<#
启动全部本地服务（各开一个窗口，日志直接显示在窗口里）：
  ComfyUI            http://127.0.0.1:8188   画面 / 视频 / 超分 / 配乐
  IndexTTS-2.5       http://127.0.0.1:9001   对白配音
  Qwen3-TTS + ASR    http://127.0.0.1:9002   音色设计 / 对白识别 / 字幕对齐
Ollama 作为 Windows 服务自动运行，不在这里启动。

  powershell -ExecutionPolicy Bypass -File install\windows\start_all.ps1
  powershell -ExecutionPolicy Bypass -File install\windows\start_all.ps1 -Only comfy
#>
param(
    [ValidateSet("all", "comfy", "audio")][string]$Only = "all",
    [string]$ComfyArgs = "--listen 127.0.0.1 --port 8188 --disable-fast-disk --reserve-vram 1.5"
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$envFile = Join-Path $Repo ".stack.env"
if (-not (Test-Path $envFile)) { throw "找不到 $envFile，请先运行 install\windows\install.ps1" }
$S = @{}
Get-Content $envFile -Encoding UTF8 | ForEach-Object { if ($_ -match "^\s*([A-Z_]+)=(.*)$") { $S[$Matches[1]] = $Matches[2].Trim() } }

function Q($s) { return '"' + $s + '"' }
function Wait-Http($url, $name, $seconds) {
    # 直接用 WebRequest 并把 Proxy 置空：本机地址不走系统代理（Clash 等），PowerShell 5.1 / 7 通用
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $seconds) {
        try {
            $req = [System.Net.WebRequest]::Create($url); $req.Proxy = $null; $req.Timeout = 3000
            $req.GetResponse().Close()
            Write-Host "  [OK] $name 已就绪 ($url)" -ForegroundColor Green
            return $true
        } catch { Start-Sleep -Seconds 2 }
    }
    Write-Host "  [!]  $name 在 $seconds 秒内没有响应，请看它的窗口里的报错" -ForegroundColor Yellow
    return $false
}

# HuggingFace 不通时自动切镜像（IndexTTS 首次运行会下载几个小模型）
if (-not $env:HF_ENDPOINT) {
    try { Invoke-WebRequest -Uri "https://huggingface.co" -Method Head -TimeoutSec 5 -UseBasicParsing | Out-Null }
    catch { $env:HF_ENDPOINT = "https://hf-mirror.com"; Write-Host "  huggingface.co 不可达，使用 HF_ENDPOINT=$env:HF_ENDPOINT" }
}

if ($Only -in @("all", "comfy")) {
    $py = Join-Path $S["COMFY_DIR"] ".venv\Scripts\python.exe"
    Start-Process -FilePath $py -ArgumentList ("main.py " + $ComfyArgs) -WorkingDirectory $S["COMFY_DIR"]
}
if ($Only -in @("all", "audio")) {
    $server = Join-Path $Repo "services\audio_server.py"
    $ipy = Join-Path $S["INDEXTTS_DIR"] ".venv\Scripts\python.exe"
    Start-Process -FilePath $ipy -WorkingDirectory $S["INDEXTTS_DIR"] -ArgumentList (
        "$(Q $server) --engines indextts --port 9001 --indextts-dir $(Q $S["INDEXTTS_DIR"]) --indextts-version 2.5")
    $qpy = Join-Path $S["QWEN_AUDIO_DIR"] ".venv\Scripts\python.exe"
    $M = Join-Path $S["QWEN_AUDIO_DIR"] "models"
    Start-Process -FilePath $qpy -WorkingDirectory $S["QWEN_AUDIO_DIR"] -ArgumentList (
        "$(Q $server) --engines voicedesign,asr --port 9002 " +
        "--voicedesign-model $(Q (Join-Path $M 'Qwen3-TTS-12Hz-1.7B-VoiceDesign')) " +
        "--asr-model $(Q (Join-Path $M 'Qwen3-ASR-1.7B')) --aligner-model $(Q (Join-Path $M 'Qwen3-ForcedAligner-0.6B'))")
}

Write-Host "等待服务启动…"
if ($Only -in @("all", "comfy")) { Wait-Http "http://127.0.0.1:8188/system_stats" "ComfyUI" 180 | Out-Null }
if ($Only -in @("all", "audio")) {
    Wait-Http "http://127.0.0.1:9001/health" "IndexTTS 服务" 60 | Out-Null
    Wait-Http "http://127.0.0.1:9002/health" "Qwen 音频服务" 60 | Out-Null
}
& (Join-Path $Repo ".venv\Scripts\python.exe") -m aidrama doctor
