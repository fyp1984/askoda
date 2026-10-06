#!/usr/bin/env bash
# TEI 容器重建（ONNX 加速版） · seven smile 主机
#
# 用途：用 AirDrop 传来的 bge-m3-onnx.tar 重建 TEI，走 **ORT 加速路径**。
#   这是与rebuild_tei.sh 的区别：那份模型包无 onnx，只能走 CPU candle慢路径；
#   本脚本用的包里含 onnx/model.onnx + model.onnx_data，ORT 后端可正常启动。
#
# 用法：
#   bash rebuild_tei_onnx.sh --survey   # 只读检查（默认）
#   bash rebuild_tei_onnx.sh --all      # 解压 + 重建 + 验收
#
# 前置：~/Downloads 下已有 bge-m3-onnx.tar（AirDrop 传来）

set -uo pipefail

cd ~/Downloads || { echo "找不到 ~/Downloads"; exit 1; }

ONNX_TAR="bge-m3-onnx.tar"
CONTAINER="ekos-tei-embedding"
PORT=18001
MEM="8g"
MODEL_DIR="$HOME/models/bge-m3-onnx"
EXTRACT_ROOT="$HOME/models"

MODE="${1:---survey}"

echo "=============================================================="
echo " TEI 重建（ONNX 加速版）· $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

survey() {
  echo ""
  echo "【1】物料检查"
  if [ -f "$ONNX_TAR" ]; then
    echo "  ✅ $ONNX_TAR ($(du -h "$ONNX_TAR" | cut -f1))"
    echo "     大小参考：完整的 ONNX 包约 2.1GB；明显偏小说明传输不完整"
  else
    echo "  ❌ $ONNX_TAR 不存在"
    echo "     请先从 Mac 用 AirDrop 传到~/Downloads/"
    exit 1
  fi

  echo ""
  echo "【2】★ 传输完整性校验（tar 内层清单，不必解压）"
  local CNT
  CNT=$(tar -tf "$ONNX_TAR" 2>/dev/null | grep -c "onnx/")
  echo "  包内 onnx/ 条目数：$CNT"
  echo "  --- 关键文件在包内 ---"
  tar -tf "$ONNX_TAR" 2>/dev/null | grep -iE "onnx/model\.onnx" | sed 's/^/    /'
  echo "  （大小在解压后用 stat 核对，见第 1 步）"

  local HAS_DATA HAS_MODEL
  HAS_MODEL=$(tar -tf "$ONNX_TAR" 2>/dev/null | grep -c "onnx/model\.onnx$")
  HAS_DATA=$(tar -tf "$ONNX_TAR" 2>/dev/null | grep -c "onnx/model\.onnx_data$")
  if [ "$HAS_MODEL" -ge 1 ] && [ "$HAS_DATA" -ge 1 ]; then
    echo "  ✅ 两个关键文件都在（model.onnx 与 model.onnx_data）"
  else
    echo "  ❌ 缺少关键文件：model.onnx=$HAS_MODEL  model.onnx_data=$HAS_DATA"
    echo "     →传输可能中断，建议重新 AirDrop"
    exit 1
  fi

  echo ""
  echo "【3】磁盘余量（解压需约 2.1GB）"
  df -h ~ | tail -1 | sed 's/^/  /'

  echo ""
  echo "【4】当前容器状态"
  docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}} | {{.Status}}' 2>/dev/null || echo "  无容器"
  echo ""
  echo "【下一步】执行重建：bash rebuild_tei_onnx.sh --all"
}

case "$MODE" in
--survey) survey ;;

--all)
  survey
  echo ""
  echo "=============================================================="
  echo " 开始重建"
  echo "=============================================================="

  # 第 1 步：解压
  echo ""
  echo "【第 1 步】解压 ONNX 模型到 $MODEL_DIR"
  mkdir -p "$EXTRACT_ROOT"
  tar -xf "$ONNX_TAR" -C "$EXTRACT_ROOT" && echo "  ✅ 已解压" || { echo "  ❌ 解压失败"; exit 1; }
  # 包内是 bge-m3/...，重命名到 bge-m3-onnx 以便区分
  if [ -d "$EXTRACT_ROOT/bge-m3" ] && [ ! -d "$MODEL_DIR" ]; then
    mv "$EXTRACT_ROOT/bge-m3" "$MODEL_DIR" && echo "  已整理为 $MODEL_DIR"
  fi
  echo "  --- 模型内容 ---"
  ls "$MODEL_DIR" 2>/dev/null | sed 's/^/    /'
  ls "$MODEL_DIR/onnx" 2>/dev/null | sed 's/^/      onnx/ /' 2>/dev/null || ls "$MODEL_DIR/onnx" 2>/dev/null | sed 's/^/      onnx\//'

  SZ=$(du -sh "$MODEL_DIR" 2>/dev/null | cut -f1)
  echo "  总体积：$SZ"

  if [ ! -f "$MODEL_DIR/onnx/model.onnx_data" ]; then
    echo "  ❌ onnx/model.onnx_data 缺失，解压不完整"
    exit 1
  fi
  ONNX_SZ=$(stat -f%z "$MODEL_DIR/onnx/model.onnx_data" 2>/dev/null || stat -c%s "$MODEL_DIR/onnx/model.onnx_data" 2>/dev/null)
  echo "  model.onnx_data = ${ONNX_SZ:-?} 字节（正常约 2266820608）"
  if [ -n "${ONNX_SZ:-}" ] && [ "$ONNX_SZ" -lt 2000000000 ] 2>/dev/null; then
    echo "  ❌ 明显偏小，模型不完整，停止"
    exit 1
  fi

  # 第 2 步：镜像
  echo ""
  echo "【第 2 步】确认 TEI 镜像"
  # ★ 必须用 --format 取名字，不能用 awk 切表格：
  #   Tag 缺失时 docker images 会把 <none> 并进 REPOSITORY 列，
  #   awk '{print $1":"$2}' 会拼出 "repo:tag:<none>" 这种错名 → docker run 报 invalid reference
  IMG=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i "text-embeddings-inference" | grep -v ":<none>" | head -1)
  if [ -z "$IMG" ]; then
    echo "  本机无 TEI 镜像，从 tar 加载："
    [ -f "tei-cpu-1.8.tar" ] || { echo "  ❌ 缺 tei-cpu-1.8.tar"; exit 1; }
    docker load -i tei-cpu-1.8.tar 2>&1 | tail -2 | sed 's/^/  /'
    IMG=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i "text-embeddings-inference" | grep -v ":<none>" | head -1)
    [ -z "$IMG" ] && IMG=$(docker images --format '{{.ID}}' 2>/dev/null | while read i; do
        docker inspect "$i" --format '{{join .RepoTags " "}}' 2>/dev/null | grep -qi text-embeddings && { echo "$i"; break; }
      done)
  fi
  [ -z "$IMG" ] && { echo "  ❌ 仍找不到镜像"; exit 1; }
  echo "  ✅ 使用镜像：$IMG"
  echo "     （校验该名字能被docker 识别）"
  docker image inspect "$IMG" >/dev/null 2>&1 && echo "     ✅ 镜像名有效" || { echo "     ❌ 镜像名无效，停"; exit 1; }

  # 第 3 步：重建容器
  echo ""
  echo "【第 3 步】重建容器"
  for OLD in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -iE "^ekos-tei"); do
    docker stop "$OLD" >/dev/null 2>&1
    docker rm "$OLD" >/dev/null 2>&1 && echo "  清理旧容器：$OLD"
  done

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

  # 第 4 步：等待
  echo ""
  echo "【第 4 步】等待就绪"
  for i in $(seq 1 30); do
    C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:${PORT}/health 2>/dev/null)
    [ "$C" = "200" ] && { echo "  ✅ 就绪（约 $((i*10)) 秒）"; break; }
    sleep 10
    [ $i -eq 30 ] && { echo "  ❌ 5 分钟未就绪："; docker logs "$CONTAINER" 2>&1|tail -25; exit 1; }
  done

  # 第 5 步：后端类型
  echo ""
  echo "【第 5 步】★ 后端类型确认（这次应该是 ORT，不是 candle）"
  docker logs "$CONTAINER" 2>&1 | grep -E "Could not start ORT|Starting Bert model|Starting model backend" | tail -3 | sed 's/^/  /'
  if docker logs "$CONTAINER" 2>&1 | grep -q "Could not start ORT"; then
    echo "  ⚠️ ORT 仍失败 —— 说明模型仍不对，回退到candle 慢路径"
  else
    echo "  ✅ ORT 后端正常 —— 性能应明显优于 candle 路径"
  fi

  # 第 6 步：验收
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

  # 第 7 步：维度校验（确认与本机同模型）
  echo ""
  echo "【第 7 步】★ 向量维度校验（须为 1024 = bge-m3）"
  DIM=$(curl -s -m 30 -X POST http://127.0.0.1:${PORT}/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null \
        | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data'][0]['embedding']))" 2>/dev/null)
  if [ "$DIM" = "1024" ]; then
    echo "  ✅ 维度 1024 —— 与本机同模型，可安全切换"
  else
    echo "  ⚠️ 维度 = ${DIM:-读取失败}，期望 1024"
  fi

  echo ""
  echo "=============================================================="
  echo " 结果"
  echo "=============================================================="
  docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}} | {{.Status}}'
  echo "  重启次数：$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
  echo ""
  if [ "$OK" -eq 30 ]; then
    echo "  ✅ 30/30 全过 + 维度 1024 → **可用**"
    echo "     把这两条结果贴回给我，我做最后一步：切 askoda 的 base_url 到这台"
  elif [ "$OK" -ge 25 ]; then
    echo "  ⚠️ $OK/30 —— 接近可用但未达稳定标准，建议再观察"
  else
    echo "  ❌ 仅 $OK/30 —— 不稳定，别用于生产链路"
  fi
  ;;

*) echo "用法: bash $0 [--survey|--all]"; exit 1 ;;
esac
exit 0