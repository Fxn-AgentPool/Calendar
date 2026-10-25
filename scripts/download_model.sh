#!/bin/sh
# Download a GGUF model into models/.
#
#   scripts/download_model.sh                # default: qwen3-4b
#   scripts/download_model.sh qwen2.5-7b
#   scripts/download_model.sh https://huggingface.co/.../model-Q4_K_M.gguf
#
# Then set LLM_MODEL_FILE in .env to the printed file name and run
# `docker compose up -d llm`.
set -eu

cd "$(dirname "$0")/.."

choice=${1:-qwen3-4b}
case "$choice" in
    qwen3-4b)
        # ~2.5 GB. Fast, good at structured output. Default.
        url=https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
        ;;
    qwen2.5-7b)
        # ~4.7 GB. Slower, sometimes better with messy input.
        url=https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf
        ;;
    llama3.2-3b)
        # ~2.0 GB. Fastest of the three.
        url=https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf
        ;;
    https://*)
        url=$choice
        ;;
    *)
        echo "usage: $0 [qwen3-4b | qwen2.5-7b | llama3.2-3b | <https url to a .gguf>]" >&2
        exit 2
        ;;
esac

file=$(basename "${url%%\?*}")
case "$file" in
    *.gguf) ;;
    *) echo "URL must point to a .gguf file" >&2; exit 1 ;;
esac

mkdir -p models
if [ -f "models/$file" ]; then
    echo "models/$file already exists"
else
    echo "Downloading $file ..."
    # Resumes an interrupted download.
    curl -fL --retry 3 --continue-at - -o "models/$file.part" "$url"
    mv "models/$file.part" "models/$file"
fi

echo "sha256: $(sha256sum "models/$file" | cut -d' ' -f1)"
echo
echo "Set this in .env:"
echo "LLM_MODEL_FILE=$file"
