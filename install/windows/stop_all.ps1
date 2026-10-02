# 关闭 start_all.ps1 启动的 ComfyUI 和音频服务（按命令行匹配进程）
#   powershell -ExecutionPolicy Bypass -File install\windows\stop_all.ps1
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$S = @{}
Get-Content (Join-Path $Repo ".stack.env") -Encoding UTF8 | ForEach-Object { if ($_ -match "^\s*([A-Z_]+)=(.*)$") { $S[$Matches[1]] = $Matches[2].Trim() } }
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -like "*audio_server.py*" -or
        ($_.CommandLine -like "*main.py*" -and $_.CommandLine -like "*$($S['COMFY_DIR'])*") -or
        ($_.ExecutablePath -and $_.ExecutablePath -like "$($S['COMFY_DIR'])*")
    )
}
foreach ($p in $procs) {
    Write-Host "停止 PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(120, $p.CommandLine.Length)))"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $procs) { Write-Host "没有找到正在运行的服务" }
