#!/bin/bash
# 下载 BAAI/bge-m3 (ONNX 格式, 供 infiniflow/text-embeddings-inference:cpu-1.8 使用)
# 源: https://hf-mirror.com/BAAI/bge-m3  (hf-mirror 是 huggingface.co 的国内镜像, 本机直连 hf.co 不通)
set -u
DEST="${1:-/Users/FYP/Documents/WorkSpace/askoda/.models/bge-m3}"
BASE="https://hf-mirror.com/BAAI/bge-m3/resolve/main"
mkdir -p "$DEST/onnx"

FILES="onnx/config.json onnx/tokenizer.json onnx/tokenizer_config.json onnx/special_tokens_map.json onnx/sentencepiece.bpe.model onnx/model.onnx onnx/model.onnx_data config.json 1_Pooling/config.json"

for f in $FILES; do
  out="$DEST/$f"
  mkdir -p "$(dirname "$out")"
  if [ -s "$out" ]; then echo "SKIP(exists) $f"; continue; fi
  # -C - 断点续传; --retry 3; -m 900 单文件 15 分钟硬超时, 不死等
  code=$(curl -sSL -C - --retry 3 --retry-delay 2 -m 900 \
    -o "$out" -w "%{http_code}" "$BASE/$f" 2>/dev/null)
  sz=$(stat -f%z "$out" 2>/dev/null || echo 0)
  echo "DONE code=$code size=$sz  $f"
done
echo "ALL_DOWNLOADS_FINISHED"
