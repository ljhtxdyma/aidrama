#!/usr/bin/env bash
# aidrama 一键安装（Ubuntu 22.04/24.04 + RTX 5090 32GB + 128GB 内存）
#
#   bash install/linux/install.sh
#   bash install/linux/install.sh --source modelscope --pip-mirror tuna      # 国内网络
#   bash install/linux/install.sh --stack /data/aidrama-stack --groups core,fast,lipsync
#
# 安装内容与 Windows 版一致：ComfyUI v0.38.2（Python 3.13 + PyTorch cu130）、aidrama、思源黑体、
# ComfyUI 模型、IndexTTS-2.5、Qwen3-TTS/ASR、Ollama + 本地 LLM。可重复运行，已完成的步骤会跳过。
set -euo pipefail

STACK="${HOME}/aidrama-stack"
GROUPS_="core,fast"
SOURCE="auto"
PIP_MIRROR=""
COMFY_TAG="v0.38.2"
SKIP_MODELS=0; SKIP_AUDIO=0; SKIP_LLM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack) STACK="$2"; shift 2;;
    --groups) GROUPS_="$2"; shift 2;;
    --source) SOURCE="$2"; shift 2;;
    --pip-mirror) PIP_MIRROR="$2"; shift 2;;
    --comfy-tag) COMFY_TAG="$2"; shift 2;;
    --skip-models) SKIP_MODELS=1; shift;;
    --skip-audio) SKIP_AUDIO=1; shift;;
    --skip-llm) SKIP_LLM=1; shift;;
    -h|--help) sed -n '2,10p' "$0"; exit 0;;
    *) echo "未知参数 $1"; exit 1;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$STACK"; STACK="$(cd "$STACK" && pwd)"
COMFY="$STACK/ComfyUI"; INDEXTTS="$STACK/index-tts"; QWEN_AUDIO="$STACK/qwen-audio"
step() { printf '\n\033[36m==== %s ====\033[0m\n' "$*"; }
ok()   { printf '  \033[32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[!]\033[0m  %s\n' "$*"; }

case "$PIP_MIRROR" in
  tuna) export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple";;
  aliyun) export UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple";;
esac
[[ "$SOURCE" == "hf-mirror" || "$SOURCE" == "modelscope" ]] && export HF_ENDPOINT="https://hf-mirror.com"
TORCH_CU130="https://download.pytorch.org/whl/cu130"
TORCH_CU128="https://download.pytorch.org/whl/cu128"

# ---------------------------------------------------------------- 0
step "0/8 硬件与磁盘"
if command -v nvidia-smi >/dev/null; then
  gpu="$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | head -1)"
  ok "GPU: $gpu"
  drv="$(echo "$gpu" | cut -d, -f2 | tr -d ' ' | cut -d. -f1)"
  (( drv < 580 )) && warn "驱动 $drv < 580：sudo ubuntu-drivers install 或 sudo apt install nvidia-driver-580-open（Blackwell 必须用 -open 内核模块）"
  grep -qi "open" /proc/driver/nvidia/version 2>/dev/null || warn "当前不是 open 内核模块驱动；RTX 50 系需要 nvidia-driver-xxx-open"
else
  warn "找不到 nvidia-smi，请先安装 NVIDIA 驱动（nvidia-driver-580-open 或更新）"
fi
free_gb=$(df -BG --output=avail "$STACK" | tail -1 | tr -dc '0-9')
(( free_gb < 260 )) && warn "剩余 ${free_gb}GB；core+fast 模型约 160GB，建议预留 260GB 以上" || ok "剩余 ${free_gb}GB"
ok "安装目录：$STACK"

# ---------------------------------------------------------------- 1
step "1/8 基础工具（git / ffmpeg / uv）"
need_apt=()
for c in git ffmpeg curl unzip; do command -v "$c" >/dev/null || need_apt+=("$c"); done
if (( ${#need_apt[@]} )); then
  warn "安装 ${need_apt[*]}（需要 sudo）"
  sudo apt-get update && sudo apt-get install -y "${need_apt[@]}"
fi
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh || python3 -m pip install --user uv
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version)"
uv python install 3.13 3.12 3.11

# ---------------------------------------------------------------- 2
step "2/8 ComfyUI $COMFY_TAG + PyTorch cu130"
[[ -f "$COMFY/main.py" ]] || git clone --depth 1 --branch "$COMFY_TAG" https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
[[ -x "$COMFY/.venv/bin/python" ]] || uv venv --python 3.13 "$COMFY/.venv"
uv pip install --python "$COMFY/.venv/bin/python" torch torchvision torchaudio --index-url "$TORCH_CU130"
uv pip install --python "$COMFY/.venv/bin/python" -r "$COMFY/requirements.txt"
"$COMFY/.venv/bin/python" -c "import torch;print('  torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"

# ---------------------------------------------------------------- 3
step "3/8 aidrama 编排器"
[[ -x "$REPO/.venv/bin/python" ]] || uv venv --python 3.12 "$REPO/.venv"
uv pip install --python "$REPO/.venv/bin/python" -e "${REPO}[dev]"
cat > "$REPO/aidrama.sh" <<'EOS'
#!/usr/bin/env bash
exec "$(cd "$(dirname "$0")" && pwd)/.venv/bin/python" -m aidrama "$@"
EOS
chmod +x "$REPO/aidrama.sh"
ok "命令入口：$REPO/aidrama.sh"

# ---------------------------------------------------------------- 4
step "4/8 字幕字体（思源黑体 SC）"
mkdir -p "$REPO/fonts"
if [[ ! -f "$REPO/fonts/SourceHanSansSC-Bold.otf" ]]; then
  tmpz="$(mktemp --suffix .zip)"
  if curl -fL --retry 3 -o "$tmpz" https://github.com/adobe-fonts/source-han-sans/releases/download/2.004R/SourceHanSansSC.zip; then
    unzip -o -j "$tmpz" "OTF/SimplifiedChinese/SourceHanSansSC-Bold.otf" "OTF/SimplifiedChinese/SourceHanSansSC-Medium.otf" -d "$REPO/fonts" >/dev/null
    ok "已放到 $REPO/fonts"
  else
    warn "字体下载失败；可 sudo apt install fonts-noto-cjk 作为替代"
  fi
  rm -f "$tmpz"
else ok "已存在"; fi

# ---------------------------------------------------------------- 5
step "5/8 ComfyUI 模型（分组：$GROUPS_，来源：$SOURCE）"
if (( SKIP_MODELS )); then warn "按参数跳过"; else
  "$REPO/.venv/bin/python" "$REPO/scripts/download_models.py" --comfy "$COMFY" --groups "$GROUPS_" --source "$SOURCE"
fi

get_repo() {  # get_repo <repo_id> <dir>
  local id="$1" dir="$2"
  if [[ -f "$dir/config.json" || -f "$dir/config.yaml" ]]; then ok "已存在 $dir"; return; fi
  local order=(huggingface modelscope); [[ "$SOURCE" == "modelscope" ]] && order=(modelscope huggingface)
  for s in "${order[@]}"; do
    if [[ "$s" == modelscope ]]; then
      uvx --from modelscope modelscope download --model "$id" --local_dir "$dir" && return
    else
      uvx --from huggingface_hub hf download "$id" --local-dir "$dir" && return
    fi
    warn "$s 下载 $id 失败，换下一个来源"
  done
  echo "无法下载 $id"; return 1
}

# ---------------------------------------------------------------- 6
step "6/8 音频：IndexTTS-2.5 + Qwen3-TTS/ASR"
if (( SKIP_AUDIO )); then warn "按参数跳过"; else
  [[ -f "$INDEXTTS/pyproject.toml" ]] || git clone --depth 1 https://github.com/index-tts/index-tts.git "$INDEXTTS"
  (cd "$INDEXTTS" && if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then uv sync --extra webui --default-index "$UV_DEFAULT_INDEX"; else uv sync --extra webui; fi)
  get_repo IndexTeam/IndexTTS-2.5 "$INDEXTTS/checkpoints"

  QPY="$QWEN_AUDIO/.venv/bin/python"
  [[ -x "$QPY" ]] || uv venv --python 3.11 "$QWEN_AUDIO/.venv"
  uv pip install --python "$QPY" "torch==2.8.*" "torchaudio==2.8.*" --index-url "$TORCH_CU128"
  # qwen-asr 固定 transformers==4.57.6、qwen-tts 固定 4.57.3：装 asr 的版本，tts 用 --no-deps 共存
  uv pip install --python "$QPY" qwen-asr
  uv pip install --python "$QPY" --no-deps qwen-tts
  uv pip install --python "$QPY" "accelerate==1.12.0" librosa soundfile sox onnxruntime einops
  get_repo Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign "$QWEN_AUDIO/models/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
  get_repo Qwen/Qwen3-ASR-1.7B "$QWEN_AUDIO/models/Qwen3-ASR-1.7B"
  get_repo Qwen/Qwen3-ForcedAligner-0.6B "$QWEN_AUDIO/models/Qwen3-ForcedAligner-0.6B"
fi

# ---------------------------------------------------------------- 7
step "7/8 本地 LLM（Ollama）"
if (( SKIP_LLM )); then warn "按参数跳过"; else
  if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh || warn "Ollama 安装失败，可改用云端 LLM（见 docs/02-安装部署.md）"
  fi
  if command -v ollama >/dev/null; then
    if ! ollama list | grep -q "aidrama-llm"; then
      pulled=""
      for c in "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL" "modelscope.cn/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL" \
               "hf.co/unsloth/Qwen3.6-27B-GGUF:UD-Q5_K_XL" "qwen3.6:27b" "qwen3.5:27b"; do
        echo "  ollama pull $c"
        if ollama pull "$c"; then ollama cp "$c" aidrama-llm; pulled="$c"; break; fi
      done
      [[ -n "$pulled" ]] && ok "aidrama-llm -> $pulled" || warn "没有拉取成功：手动 ollama pull <模型> 后执行 ollama cp <模型> aidrama-llm"
    else ok "aidrama-llm 已存在"; fi
  fi
fi

# ---------------------------------------------------------------- 8
step "8/8 写入路径配置 + 自检"
cat > "$REPO/.stack.env" <<EOF
STACK_DIR=$STACK
COMFY_DIR=$COMFY
INDEXTTS_DIR=$INDEXTTS
QWEN_AUDIO_DIR=$QWEN_AUDIO
EOF
"$REPO/.venv/bin/python" -m aidrama fetch-guides || warn "H3 官方提示词指南下载失败（不影响运行）"
"$REPO/.venv/bin/python" -m pytest -q "$REPO/tests" -x
ok "安装完成。下一步："
echo "   1) 启动服务：  bash install/linux/start_all.sh"
echo "   2) 体检：      ./aidrama.sh doctor && ./aidrama.sh smoke   （真机冒烟测试，约 10~15 分钟）"
echo "   3) 示例工程：  ./aidrama.sh init-demo projects/demo && ./aidrama.sh run projects/demo ep01"
