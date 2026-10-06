#!/usr/bin/env bash
# TEI 容器重建 · seven smile 主机（用本地 tar 包）
#
# 物料检查结论（2026-10-06 10:52实测）：
#   tei-cpu-1.8.tar  6.4G  → **Docker OCI 镜像归档**（blobs/sha256/...），可 docker load
#   bge-m3.tar       2.1G  → **普通模型文件包**，含 pytorch_model.bin / colbert_linear.pt /
#                                sparse_linear.pt，**不含 onnx/**
#
# ⚠️ 重要预期管理：
#   模型包是 **PyTorch 版，没有 ONNX** → TEI 的 ORT 后端仍然起不来，会回退 candle（慢但能用）。
#   这是物料本身的限制，不是重建方法不对。重建后预期是「能服务，但走 CPU 慢路径」。
#
#   想要 ORNX 加速路径 → 需另传 ONNX 版模型（Mac 上 .models/bge-m3，11 个文件，2.2GB）
#
# 用法：
#   bash rebuild_tei.sh --survey   # 只读检查（默认）
#   bash rebuild_tei.sh --load     # 只做 docker load + 解压模型
#   bash rebuild_tei.sh --start    # 重建容器并验收（30 次连打）
#   bash rebuild_tei.sh --all      # load + start 全做

set -uo pipefail

cd ~/Downloads || { echo "找不到 ~/Downloads"; exit 1; }

TEI_TAR="tei-cpu-1.8.tar"
MODEL_TAR="bge-m3.tar"
CONTAINER="ekos-tei-embedding"
PORT=18001
MEM="8g"
MODEL_DIR="$HOME/models/bge-m3"

MODE="${1:---survey}"

echo "=============================================================="
echo " TEI 容器重建 · $(date '+%Y-%m-%d %H:%M:%S') · 模式=$MODE"
echo "=============================================================="

# ---------- 盘点 ----------
if [ "$MODE" = "--survey" ]; then
  echo ""
  echo "【1】当前状态"
  docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}} | {{.Status}}' 2>/dev/null || echo "  无容器"
  echo ""
  echo "【2】磁盘余量（load 6.4G 镜像 + 解压 2.1G 模型，需约 10GB 可用）"
  df -h ~ | tail -1 | sed 's/^/  /'
  echo ""
  echo "【3】镜像是否已存在（若已存在则无需 load）"
  docker images | grep -iE "text-embeddings|tei" | sed 's/^/  /' || echo "  未找到 TEI 镜像"
  echo ""
  echo "【4】物料确认"
  for f in "$TEI_TAR" "$MODEL_TAR"; do
    [ -f "$f" ] && echo "  ✅ $f ($(du -h "$f"|cut -f1))" || echo "  ❌ $f 缺失"
  done
  echo ""
  echo "【下一步】"
  echo "  bash rebuild_tei.sh --all     # load + 解压 + 重建 + 验收"
  echo ""
  echo "★ 预期：重建后能服务，但走 CPU candle 慢路径（模型包无 ONNX，ORT 后端用不了）"
  exit 0
fi

# ---------- 加载镜像 + 解压模型 ----------
if [ "$MODE" = "--load" ] || [ "$MODE" = "--all" ]; then
  echo ""
  echo "【第 1 步】加载 TEI 镜像（6.4G，需几分钟）"
  if docker images | grep -qiE "text-embeddings-inference"; then
    echo "  ✅ 镜像已存在，跳过 load"
  else
    if [ ! -f "$TEI_TAR" ]; then
      echo "  ❌ 找不到 $TEI_TAR"; exit 1
    fi
    echo "  加载中，请耐心..."
    docker load -i "$TEI_TAR" 2>&1 | tail -3 | sed 's/^/  /'
  fi
  echo "  --- 加载后的镜像 ---"
  docker images | grep -iE "text-embeddings|tei" | sed 's/^/  /'

  echo ""
  echo "【第 2 步】解压模型"
  if [ -d "$MODEL_DIR" ] && [ -f "$MODEL_DIR/config.json" ]; then
    echo "  ✅ 模型已存在：$MODEL_DIR（跳过解压）"
  else
    mkdir -p "$(dirname "$MODEL_DIR")"
    tar -xf "$MODEL_TAR" -C "$(dirname "$MODEL_DIR")" && echo "  ✅ 已解压到 $MODEL_DIR"
  fi
  echo "  --- 模型内容 ---"
  ls "$MODEL_DIR" 2>/dev/null | sed 's/^/    /'
  SZ=$(du -sh "$MODEL_DIR" 2>/dev/null | cut -f1)
  echo "  总体积：$SZ"
  if [ -f "$MODEL_DIR/onnx/model.onnx" ]; then
    echo "  ✅ 含 ONNX → ORT 后端可用"
  else
    echo "  ⚠️ 无 onnx/ → ORT 后端不可用，会回退 candle（慢路径，但能服务）"
  fi
fi

# ---------- 重建容器 ----------
if [ "$MODE" = "--start" ] || [ "$MODE" = "--all" ]; then
  echo ""
  echo "【第 3 步】重建容器"
  if [ ! -f "$MODEL_DIR/config.json" ]; then
    echo "  ❌ 模型未就位：$MODEL_DIR —— 请先跑 --load"
    exit 1
  fi

  # 停旧容器
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    docker stop "$CONTAINER" >/dev/null 2>&1
    docker rm "$CONTAINER" >/dev/null 2>&1 && echo "  旧容器已删除"
  fi

  # 清掉可能残留的停止容器（上一轮 fix 脚本建的）
  for OLD in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -iE "^ekos-tei"); do
    docker stop "$OLD" >/dev/null 2>&1; docker rm "$OLD" >/dev/null 2>&1 && echo "  清理旧容器 $OLD"
  done

  IMG=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i "text-embeddings-inference" | grep -v ":<none>" | head -1)
  [ -z "$IMG" ] && { echo "  ❌ 没找到 TEI 镜像，先跑 --load"; exit 1; }
  echo "  使用镜像：$IMG"

  echo "  启动中..."
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --memory "$MEM" --memory-swap "$MEM" \
    -p "${PORT}:80" \
    -v "${MODEL_DIR}:/models/bge-m3:ro" \
    "$IMG" \
    --model-id /models/bge-m3 \
    --pooling cls \
    --auto-truncate \
    --tokenization-workers 2 \
    --max-batch-tokens 2048 \
    --max-client-batch-size 8 \
    >/dev/null 2>&1 && echo "  ✅ 已启动" || { echo "  ❌ 启动失败"; docker logs "$CONTAINER" 2>&1|tail -20; exit 1; }

  echo ""
  echo "【第 4 步】等待就绪"
  for i in $(seq 1 30); do
    C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:${PORT}/health 2>/dev/null)
    [ "$C" = "200" ] && { echo "  ✅ 就绪（约 $((i*10)) 秒）"; break; }
    sleep 10
    [ $i -eq 30 ] && { echo "  ❌ 5 分钟未就绪："; docker logs "$CONTAINER" 2>&1|tail -25; exit 1; }
  done

  echo ""
  echo "【第 5 步】后端类型确认"
  docker logs "$CONTAINER" 2>&1 | grep -E "Could not start ORT|Starting Bert model|Starting model backend" | tail -3 | sed 's/^/  /'
  if docker logs "$CONTAINER" 2>&1 | grep -q "Could not start ORT"; then
    echo "  ℹ️ 预期内：模型包无 ONNX，走 candle CPU 路径"
  fi

  echo ""
  echo "【第 6 步】★ 稳定性验收（连打 30 次）"
  OK=0; BAD=0
  for i in $(seq 1 30); do
    C=$(curl -s -m 20 -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:${PORT}/v1/embeddings \
        -H 'Content-Type: application/json' -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null)
    [ "$C" = "200" ] && OK=$((OK+1)) || { BAD=$((BAD+1)); echo "    第 $i 次失败 http=$C"; }
    sleep 1
  done
  echo ""
  echo "  结果：成功 $OK / 失败 $BAD"
  echo ""
  if [ "$OK" -eq 30 ]; then
    echo "  ✅ 30/30 全过 —— 服务稳定"
  elif [ "$OK" -ge 25 ]; then
    echo "  ⚠️ $OK/30 —— 接近可用，但未达稳定标准"
  else
    echo "  ❌ 仅 $OK/30 —— 仍不稳定，别用于生产链路"
  fi
  echo ""
  echo "  容器状态："
  docker ps -a --filter "name=$CONTAINER" --format '    {{.Names}} | {{.Status}}'
  echo "  重启次数：$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
fi
exit 0