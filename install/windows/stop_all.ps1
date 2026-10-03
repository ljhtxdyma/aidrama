# 关闭 start_all.ps1 启动的 ComfyUI 和音频服务（按命令行匹配进程）
#   powershell -ExecutionPolicy Bypass -File install\windows\stop_all.ps1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$envFile = Join-Path $Repo ".stack.env"
if (-not (Test-Path $envFile)) { throw "找不到 $envFile（先运行 install\windows\install.ps1）；为安全起见不做任何操作" }
$S = @{}
Get-Content $envFile -Encoding UTF8 | ForEach-Object { if ($_ -match "^\s*([A-Z_]+)=(.*)$") { $S[$Matches[1]] = $Matches[2].Trim() } }
$comfy = $S["COMFY_DIR"]
# 安全检查：路径必须是存在的、含 main.py 的 ComfyUI 目录，否则一个空字符串会让匹配条件命中所有进程
if (-not $comfy -or -not (Test-Path (Join-Path $comfy "main.py"))) { throw ".stack.env 里的 COMFY_DIR 无效（$comfy），为安全起见不做任何操作" }
$comfy = (Resolve-Path $comfy).Path.TrimEnd("\") + "\"
$server = (Join-Path $Repo "services\audio_server.py")

function Starts-With($text, $prefix) {
    return $text -and $text.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}
$procs = Get-CimInstance Win32_Process | Where-Object {
    $cl = $_.CommandLine
    $exe = $_.ExecutablePath
    ($cl -and $cl.IndexOf($server, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -or
    ((Starts-With $exe $comfy) -and $cl -and $cl.IndexOf("main.py", [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}
foreach ($p in $procs) {
    Write-Host "停止 PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(120, $p.CommandLine.Length)))"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $procs) { Write-Host "没有找到正在运行的服务" }
