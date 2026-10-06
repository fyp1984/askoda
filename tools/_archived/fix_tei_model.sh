#!/usr/bin/env bash
# TEI 模型替换 · seven smile 主机（一步到位）
#
# 病根：挂的是 PyTorch 版模型（pytorch_model.bin + colbert_linear.pt + sparse_linear.pt），
#       **没有 ONNX 文件** → TEI 的 ORT 后端必然起不来，candle 后端加载 ColBERT 结构会崩。
#       参数调对了也没用（fix_tei.sh 那轮12/30 就是这么来的）。
#
# 解法：换成 ONNX 版模型（与本机 askoda 用的是同一份，来自 hf-mirror 官方仓库）。
#
# 用法：
#   bash fix_tei_model.sh            # 预览，不动任何东西
#   bash fix_tei_model.sh --scp      # 从本机 Mac 传过去（在 Mac 上执行）
#   bash fix_tei_model.sh --download  # 在 seven smile 上直接下载（若它能连 hf-mirror）
#   bash fix_tei_model.sh --apply    # 只重建容器（前提：模型已就位）

set -uo pipefail

CONTAINER="ekos-tei-embedding"
IMAGE="infiniflow/text-embeddings-inference:cpu-1.8"
PORT=18001
MEM="8g"

# 目标模型目录（ONNX 版）
TARGET="/Users/sevensimle/Documents/WorkSpace/CheersAI/CheersAI-EKOS/external/models/BAAI/bge-m3-onnx"
# ONNX 版需要的文件（与本机 .models/bge-m3/ 实测结构一致，共 11 个）
# 注意：sentencepiece.bpe.model 在本机位于 onnx/ 下，顶层没有，故只从 onnx/ 取
FILES=(
  "onnx/model.onnx"
  "onnx/model.onnx_data"
  "onnx/config.json"
  "onnx/sentencepiece.bpe.model"
  "onnx/special_tokens_map.json"
  "onnx/tokenizer_config.json"
  "tokenizer.json"
  "config.json"
  "tokenizer_config.json"
  "special_tokens_map.json"
  "1_Pooling/config.json"
)

MODE="${1:-preview}"

echo "=============================================================="
echo " TEI 模型替换 · $(date '+%Y-%m-%d %H:%M:%S') · 模式=$MODE"
echo "=============================================================="

case "$MODE" in
# ---------------------------------------------------------------- 本机 Mac 执行
--scp)
  SRC_LOCAL="/Users/FYP/Documents/WorkSpace/askoda/.models/bge-m3"
  echo ""
  echo "【从本机 Mac 传ONNX 模型到 seven smile】"
  echo "  源  ：$SRC_LOCAL"
  echo "  目标：sevensimle@100.103.240.78:$TARGET"
  echo ""
  if [ ! -f "$SRC_LOCAL/onnx/model.onnx_data" ]; then
    echo "  ❌ 本机没有 onnx/model.onnx_data，先跑 .models/fetch_bge_m3.sh 拉取"
    exit 1
  fi
  echo "  待传文件："
  for f in "${FILES[@]}"; do
    [ -f "$SRC_LOCAL/$f" ] && echo "    ✅ $f ($(du -h "$SRC_LOCAL/$f" | cut -f1))" || echo "    ⚠️  $f 不存在"
  done
  echo ""
  echo "  执行传输（约 2.2GB，走 Tailscale 链路，请耐心）："
  echo ""
  echo "    ssh sevensimle@100.103.240.78 \"mkdir -p $TARGET/onnx $TARGET/1_Pooling\""
  echo "    scp -r $SRC_LOCAL/onnx/*           sevensimle@100.103.240.78:$TARGET/onnx/"
  echo "    for f in tokenizer.json config.json tokenizer_config.json special_tokens_map.json sentencepiece.bpe.model; do \\"
  echo "      scp $SRC_LOCAL/\$f sevensimle@100.103.240.78:$TARGET/; \\"
  echo "    done"
  echo "    scp -r $SRC_LOCAL/1_Pooling       sevensimle@100.103.240.78:$TARGET/"
  echo ""
  echo "  传完后校验（关键：两个 onnx 文件大小必须对得上）："
  echo "    ssh sevensimle@100.103.240.78 'ls -la $TARGET/onnx/'"
  echo "    ★ model.onnx 应约 725KB，model.onnx_data 应约 2.1GB"
  echo ""
  echo "  传完后再执行：  bash fix_tei_model.sh --apply"
  exit 0
  ;;

# ---------------------------------------------------------------- 远端下载
--download)
  echo ""
  echo "【在 seven smile 上直接从 hf-mirror 下载 ONNX 模型】"
  BASE="https://hf-mirror.com/BAAI/bge-m3/resolve/main"
  echo "  目标：$TARGET"
  mkdir -p "$TARGET/onnx" "$TARGET/1_Pooling" || { echo "  ❌ 创建目录失败"; exit 1; }
  echo ""
  echo "  先测网络连通性（不通就别硬试）："
  if ! curl -sI -m 10 -o /dev/null -w "%{http_code}" "$BASE/config.json" | grep -q "200"; then
    echo "  ❌ hf-mirror 不通 → 改用 --scp 方式从Mac 传"
    exit 1
  fi
  echo "  ✅ 网络通，开始下载…"
  for f in "${FILES[@]}"; do
    printf "    %-40s" "$f"
    curl -sL -m 1800 --retry 2 -o "$TARGET/$f" "$BASE/$f" && echo "✅" || { echo "❌"; break; }
  done
  echo ""
  echo "  校验："
  ls -la "$TARGET/onnx/" 2>/dev/null | sed 's/^/    /'
  echo "  ★ model.onnx_data 应约 2.1GB；若明显偏小说明没下全"
  echo ""
  echo "  下载完成后执行：  bash fix_tei_model.sh --apply"
  exit 0
  ;;

# ---------------------------------------------------------------- 重建容器
--apply)
  echo ""
  echo "【前置检查】"
  if [ ! -f "$TARGET/onnx/model.onnx" ]; then
    echo "  ❌ $TARGET/onnx/model.onnx 不存在"
    echo "     请先跑 --download，或用 --scp 从Mac 传。"
    exit 1
  fi
  echo "  ✅ ONNX 模型就位"
  SZ=$(stat -f%z "$TARGET/onnx/model.onnx_data" 2>/dev/null || stat -c%s "$TARGET/onnx/model.onnx_data" 2>/dev/null)
  echo "     model.onnx_data = ${SZ:-未知} 字节"
  if [ -n "${SZ:-}" ] && [ "$SZ" -lt 2000000000 ] 2>/dev/null; then
    echo "     ⚠️ 明显偏小（正常约 2266820608），模型可能没下全，**建议别往下走**"
    exit 1
  fi

  echo ""
  echo "【重建容器】（挂载 ONNX 版 + 全部参数）"
  docker stop "$CONTAINER" >/dev/null 2>&1
  docker rm -f "$CONTAINER" >/dev/null 2>&1
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --memory "$MEM" --memory-swap "$MEM" \
    -p "${PORT}:80" \
    -v "${TARGET}:/models/bge-m3:ro" \
    "$IMAGE" \
    --model-id /models/bge-m3 \
    --pooling cls \
    --auto-truncate \
    --tokenization-workers 2 \
    --max-batch-tokens 2048 \
    --max-client-batch-size 8 \
    >/dev/null 2>&1 && echo "  ✅ 已启动" || { echo "  ❌ 启动失败"; docker logs "$CONTAINER" 2>&1|tail -20; exit 1; }

  echo ""
  echo "【等待就绪】"
  for i in $(seq 1 30); do
    C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:${PORT}/health 2>/dev/null)
    [ "$C" = "200" ] && { echo "  ✅ 就绪（约 $((i*10)) 秒）"; break; }
    sleep 10
    [ $i -eq 30 ] && { echo "  ❌ 5 分钟未就绪："; docker logs "$CONTAINER" 2>&1|tail -25; exit 1; }
  done

  echo ""
  echo "【验证 1】ORT 后端是否起来（这次应该是 ORT，不是 candle）"
  docker logs "$CONTAINER" 2>&1 | grep -E "Could not start ORT|Starting Bert model|Starting model backend" | tail -3 | sed 's/^/  /'
  if docker logs "$CONTAINER" 2>&1 | grep -q "Could not start ORT"; then
    echo "  ⚠️ ORT 仍失败 → 模型还是不对，检查 onnx 目录内容"
  else
    echo "  ✅ ORT 后端正常 —— 性能会好很多"
  fi

  echo ""
  echo "【验证 2】★ 连打 30 次（硬标准，必须 30/30）"
  OK=0; BAD=0
  for i in $(seq 1 30); do
    C=$(curl -s -m 20 -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:${PORT}/v1/embeddings \
        -H 'Content-Type: application/json' -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null)
    [ "$C" = "200" ] && OK=$((OK+1)) || { BAD=$((BAD+1)); echo "    第 $i 次失败 http=$C"; }
    sleep 1
  done
  echo ""
  echo "  结果：成功 $OK / 失败 $BAD"
  if [ "$OK" -eq 30 ]; then
    echo "  ✅ 30/30 全过 —— 可把输出贴给我，我做向量比对（确认与本机同模型）后再切"
  else
    echo "  ❌ 未达标 → **不要切换本机**。把容器日志贴回来："
    echo "     docker logs --tail 40 $CONTAINER"
  fi
  echo ""
  echo "  容器状态："
  docker ps -a --filter "name=$CONTAINER" --format '    {{.Names}} | {{.Status}}'
  echo "  重启次数：$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)  （应为 0）"
  exit 0
  ;;

# ---------------------------------------------------------------- 预览
*)
  echo ""
  echo "【当前状态】"
  docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}} | {{.Status}}' 2>/dev/null
  echo "  重启次数: $(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
  SRC=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/models/bge-m3"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
  [ -n "${SRC:-}" ] && echo "  当前挂载: $SRC"
  if [ -n "${SRC:-}" ] && [ -d "$SRC" ]; then
    echo ""
    echo "【当前模型格式判定】"
    if [ -d "$SRC/onnx" ] && [ -f "$SRC/onnx/model.onnx" ]; then
      echo "  ✅ 是 ONNX 模型（正确）"
    else
      echo "  ❌ 没有 onnx/model.onnx —— 挂的是 PyTorch 版，这是病根"
      [ -f "$SRC/pytorch_model.bin" ] && echo "     确认：有 pytorch_model.bin"
      [ -f "$SRC/colbert_linear.pt" ] && echo "     确认：有 colbert_linear.pt（ColBERT 结构，TEI candle 后端处理不了）"
    fi
  fi
  echo ""
  echo "【三步操作】"
  echo "  ① 传ONNX 模型（二选一）"
  echo "     在你的 Mac 上执行： bash fix_tei_model.sh --scp        # 约 2.2GB 走 Tailscale"
  echo "     或在 seven smile 上： bash fix_tei_model.sh --download  # 需该机能连 hf-mirror"
  echo ""
  echo "  ② 重建容器：      bash fix_tei_model.sh --apply"
  echo "  ③ 把 30 次连打结果贴回来"
  echo ""
  echo "  模型源清单（12 个文件）："
  printf "    %s\n" "${FILES[@]}"
  ;;
esac
exit 0