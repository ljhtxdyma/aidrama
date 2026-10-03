#!/usr/bin/env bash
# 关闭 start_all.sh 启动的服务
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO/.stack.env" ]] || { echo "找不到 $REPO/.stack.env"; exit 1; }
# shellcheck disable=SC1091
source "$REPO/.stack.env"
for name in comfyui indextts qwen_audio; do
  pidf="$STACK_DIR/logs/$name.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    pid="$(cat "$pidf")"
    cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null)"
    # 只杀我们启动的进程：命令行里必须有 audio_server.py 或 ComfyUI 目录（防止 PID 被系统复用后误杀）
    if [[ "$cmd" == *audio_server.py* || "$cmd" == *"$COMFY_DIR"* ]]; then
      kill "$pid" && echo "已停止 $name"
    else
      echo "pid $pid 已不是 $name（可能重启过），跳过"
    fi
  fi
  rm -f "$pidf"
done
