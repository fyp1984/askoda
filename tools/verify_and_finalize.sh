#!/usr/bin/env bash
# TEI 收尾验收 · seven smile 主机
#
# 前提：debug_start_fail.sh 已用正确的镜像名启动成功（tei-probe，端口 18099）
# 本脚本：等模型加载完 → 确认 ORT 加速路径 → 30 次稳定性 → 维度校验 → 正式重建到 18001
#
# 用法：bash verify_and_finalize.sh

set -uo pipefail

PROBE="tei-probe"
CONTAINER="ekos-tei-embedding"
PORT=18001
PROBE_PORT=18099
MODEL_DIR="$HOME/models/bge-m3-onnx"
IMG="infiniflow/text-embeddings-inference:cpu-1.8"

echo "=============================================================="
echo " TEI 收尾验收 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 1. 等 probe 容器就绪 ----------
echo ""
echo "【1】等待 probe 容器加载模型（bge-m3 需 1-3 分钟）"
for i in $(seq 1 36); do
  ST=$(docker inspect "$PROBE" --format '{{.State.Status}}' 2>/dev/null)
  C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PROBE_PORT}/health" 2>/dev/null)
  if [ "$C" = "200" ]; then
    echo "  ✅ 就绪（约 $((i*10)) 秒）"
    break
  fi
  if [ "$ST" != "running" ]; then
    echo "  ❌ 容器状态 = $ST（已退出）"
    echo ""
    echo "  --- 退出原因 ---"
    docker inspect "$PROBE" --format '  退出码={{.State.ExitCode}}  OOM={{.State.OOMKilled}}' 2>/dev/null
    echo ""
    echo "  --- 日志尾部 ---"
    docker logs "$PROBE" 2>&1 | tail -25
    exit 1
  fi
  sleep 10
  [ $i -eq 36 ] && { echo "  ❌ 6 分钟未就绪"; docker logs "$PROBE" 2>&1|tail -25; exit 1; }
done

# ---------- 2. ★ 后端类型（这次应该是 ORT）----------
echo ""
echo "【2】★ 后端类型确认（关键：ORT 还是 candle）"
docker logs "$PROBE" 2>&1 | grep -E "Could not start ORT|Starting Bert model|Starting model backend|onnx" | tail -5 | sed 's/^/  /'
echo ""
if docker logs "$PROBE" 2>&1 | grep -q "Could not start ORT"; then
  echo "  ❌ ORT 后端仍失败 —— 模型挂载或格式仍有问题"
  echo "     容器内视角再确认一次："
  echo "     docker exec $PROBE ls -la /models/bge-m3/onnx/"
  exit 1
elif docker logs "$PROBE" 2>&1 | grep -qE "onnx|ONNX"; then
  echo "  ✅ 走 ONNX/ORT 加速路径 —— 性能应明显优于 candle"
else
  echo "  ℹ️ 日志未见明确 onnx 字样，看第 4 步耗时判断（ORT 通常 <100ms/次）"
fi

# ---------- 3. 容器内视角确认模型真的挂进去了 ----------
echo ""
echo "【3】容器内视角：模型是否真的挂进去了"
docker exec "$PROBE" ls -la /models/bge-m3/onnx/ 2>&1 | head -8 | sed 's/^/  /'
echo ""
echo "  --- container 内能否读到 onnx_data（关键）---"
docker exec "$PROBE" sh -c 'test -f /models/bge-m3/onnx/model.onnx_data && echo "有，大小=$(stat -c%s /models/bge-m3/onnx/model.onnx_data 2>/dev/null)" || echo "★缺失"' 2>&1 | sed 's/^/  /'

# ---------- 4. 单次耗时（判断快慢）----------
echo ""
echo "【4】单次embedding 耗时（ORT 通常 <100ms，candle 可能几百 ms）"
T0=$(date +%s%N 2>/dev/null || date +%s)
curl -s -m 30 -X POST "http://127.0.0.1:${PROBE_PORT}/v1/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' -o /dev/null 2>&1
T1=$(date +%s%N 2>/dev/null || date +%s)
echo "  耗时约 $(( (T1-T0)/1000000 )) ms"

# ---------- 5. 维度校验 ----------
echo ""
echo "【5】★ 向量维度校验（必须 1024 = bge-m3）"
DIM=$(curl -s -m 30 -X POST "http://127.0.0.1:${PROBE_PORT}/v1/embeddings" \
      -H 'Content-Type: application/json' \
      -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null \
      | python3 -c "import sys,json;d=json.load(sys.stdin);v=d['data'][0]['embedding'];print(len(v))" 2>/dev/null)
if [ "$DIM" = "1024" ]; then
  echo "  ✅ 维度 1024 —— 与本机同模型"
else
  echo "  ❌ 维度 = ${DIM:-读取失败}，期望 1024"
  exit 1
fi

# ---------- 6. ★ 30 次稳定性 ----------
echo ""
echo "【6】★ 稳定性验收（连打 30 次，必须 30/30）"
OK=0; BAD=0
for i in $(seq 1 30); do
  C=$(curl -s -m 20 -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:${PROBE_PORT}/v1/embeddings" \
      -H 'Content-Type: application/json' \
      -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null)
  [ "$C" = "200" ] && OK=$((OK+1)) || { BAD=$((BAD+1)); echo "    第 $i 次失败 http=$C"; }
  sleep 1
done
echo ""
echo "  结果：成功 $OK / 失败 $BAD"
echo ""
if [ "$OK" -ne 30 ]; then
  echo "  ❌ 未达 30/30，**到此为止，不要切askoda**"
  echo "     容器状态：$(docker inspect "$PROBE" --format '{{.State.Status}}' 2>/dev/null)"
  echo "     重启次数：$(docker inspect "$PROBE" --format '{{.RestartCount}}' 2>/dev/null)"
  echo "     日志尾部："
  docker logs "$PROBE" 2>&1 | tail -20 | sed 's/^/       /'
  exit 1
fi
echo "  ✅ 30/30 全过"

# ---------- 7. 清理 probe，正式部署到 18001 ----------
echo ""
echo "【7】清理 probe，正式部署到 $PORT"
docker stop "$PROBE" >/dev/null 2>&1 && docker rm "$PROBE" >/dev/null 2>&1 && echo "  probe 已清理"

for OLD in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -iE "^ekos-tei"); do
  docker stop "$OLD" >/dev/null 2>&1; docker rm "$OLD" >/dev/null 2>&1
done

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --memory 8g --memory-swap 8g \
  -p "${PORT}:80" \
  -v "${MODEL_DIR}:/models/bge-m3:ro" \
  "$IMG" \
  --model-id /models/bge-m3 \
  --pooling cls \
  --auto-truncate \
  --tokenization-workers 2 \
  --max-batch-tokens 2048 \
  --max-client-batch-size 8 \
  && echo "  ✅ 正式容器已启动" || echo "  ❌ 启动失败（错误见上方输出）"

echo ""
echo "【8】等待就绪"
for i in $(seq 1 30); do
  C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/health" 2>/dev/null)
  [ "$C" = "200" ] && { echo "  ✅ 就绪（约 $((i*10)) 秒）"; break; }
  sleep 10
done

echo ""
echo "=============================================================="
echo " 正式容器状态"
echo "=============================================================="
docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}} | {{.Status}}'
echo "  重启次数：$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
echo ""
echo "  ★ 把【2】【5】【6】三段的输出贴回来 —— 我要做最后一步："
echo "    切 askoda 的 base_url 到这台机器，然后跑向量交叉比对。"
exit 0