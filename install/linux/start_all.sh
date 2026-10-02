#!/usr/bin/env bash
# 后台启动 ComfyUI（8188）、IndexTTS（9001）、Qwen 音频服务（9002），日志在 <stack>/logs/
#   bash install/linux/start_all.sh            # 全部
#   bash install/linux/start_all.sh comfy      # 只启动 ComfyUI
#   COMFY_ARGS="--listen 0.0.0.0 --port 8188 --disable-fast-disk" bash install/linux/start_all.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO/.stack.env" ]] || { echo "找不到 $REPO/.stack.env，请先运行 install/linux/install.sh"; exit 1; }
# shellcheck disable=SC1091
source "$REPO/.stack.env"
ONLY="${1:-all}"
COMFY_ARGS="${COMFY_ARGS:---listen 127.0.0.1 --port 8188 --disable-fast-disk}"
LOGS="$STACK_DIR/logs"; mkdir -p "$LOGS"
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
if [[ -z "${HF_ENDPOINT:-}" ]] && ! curl -sI --max-time 5 https://huggingface.co >/dev/null; then
  export HF_ENDPOINT="https://hf-mirror.com"; echo "huggingface.co 不可达，使用 HF_ENDPOINT=$HF_ENDPOINT"
fi

launch() {  # launch <name> <workdir> <cmd...>
  local name="$1" dir="$2"; shift 2
  if [[ -f "$LOGS/$name.pid" ]] && kill -0 "$(cat "$LOGS/$name.pid")" 2>/dev/null; then echo "  $name 已在运行"; return; fi
  (cd "$dir" || exit 1; nohup "$@" >"$LOGS/$name.log" 2>&1 </dev/null & echo $! >"$LOGS/$name.pid")
  echo "  启动 $name（日志 $LOGS/$name.log）"
}
wait_http() {  # wait_http <url> <name> <seconds>
  local t=0
  until curl -sf --max-time 3 "$1" >/dev/null; do
    sleep 2; t=$((t + 2))
    if (( t >= $3 )); then echo "  [!] $2 在 $3 秒内没有响应，看日志：$LOGS"; return 0; fi
  done
  echo "  [OK] $2 已就绪"
}

if [[ "$ONLY" == all || "$ONLY" == comfy ]]; then
  # shellcheck disable=SC2086
  launch comfyui "$COMFY_DIR" "$COMFY_DIR/.venv/bin/python" main.py $COMFY_ARGS
fi
if [[ "$ONLY" == all || "$ONLY" == audio ]]; then
  launch indextts "$INDEXTTS_DIR" "$INDEXTTS_DIR/.venv/bin/python" "$REPO/services/audio_server.py" \
    --engines indextts --port 9001 --indextts-dir "$INDEXTTS_DIR" --indextts-version 2.5
  M="$QWEN_AUDIO_DIR/models"
  launch qwen_audio "$QWEN_AUDIO_DIR" "$QWEN_AUDIO_DIR/.venv/bin/python" "$REPO/services/audio_server.py" \
    --engines voicedesign,asr --port 9002 --voicedesign-model "$M/Qwen3-TTS-12Hz-1.7B-VoiceDesign" \
    --asr-model "$M/Qwen3-ASR-1.7B" --aligner-model "$M/Qwen3-ForcedAligner-0.6B"
fi

echo "等待服务启动…"
[[ "$ONLY" == all || "$ONLY" == comfy ]] && wait_http http://127.0.0.1:8188/system_stats ComfyUI 180
if [[ "$ONLY" == all || "$ONLY" == audio ]]; then
  wait_http http://127.0.0.1:9001/health "IndexTTS 服务" 60
  wait_http http://127.0.0.1:9002/health "Qwen 音频服务" 60
fi
"$REPO/.venv/bin/python" -m aidrama doctor || true
