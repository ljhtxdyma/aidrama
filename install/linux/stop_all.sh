#!/usr/bin/env bash
# 关闭 start_all.sh 启动的服务
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/.stack.env"
for name in comfyui indextts qwen_audio; do
  pidf="$STACK_DIR/logs/$name.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    kill "$(cat "$pidf")" && echo "已停止 $name"
  fi
  rm -f "$pidf"
done
