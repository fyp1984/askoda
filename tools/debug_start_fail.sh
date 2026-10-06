#!/usr/bin/env bash
# TEI 启动失败排查 · seven smile 主机
#
# 上一轮 rebuild_tei_onnx.sh 报「启动失败 + No such container」，
# 但真实错误被脚本的 >/dev/null 2>&1 吞掉了。本脚本**不吞错误**，把真实原因打出来。
#
# 用法：bash debug_start_fail.sh

set -uo pipefail

CONTAINER="ekos-tei-embedding"
TEST_NAME="tei-probe"
PORT=18099
MODEL_DIR="$HOME/models/bge-m3-onnx"

echo "=============================================================="
echo " TEI 启动失败排查 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 1. 镜像真实名称（★ 上轮解析错误的地方）----------
echo ""
echo "【1】镜像真实信息"
echo "  --- docker images 原始输出（前3行）---"
docker images | head -3 | sed 's/^/    /'
echo ""
IMG_FULL=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i "text-embeddings-inference" | head -1)
if [ -n "${IMG_FULL:-}" ]; then
  echo "  ✅ 精确取到的镜像名：$IMG_FULL"
else
  echo "  ⚠️ 按 Repository:Tag 找不到，尝试按 ID"
  IMG_ID=$(docker images --format '{{.ID}} {{.Repository}}' 2>/dev/null | grep -i "text-embeddings" | head -1 | awk '{print $1}')
  if [ -n "${IMG_ID:-}" ]; then
    IMG_FULL="$IMG_ID"
    echo "     改用镜像 ID：$IMG_ID"
  else
    echo "  ❌ 确实找不到 TEI 镜像 —— 需重新 docker load tei-cpu-1.8.tar"
    exit 1
  fi
fi

# ---------- 2. 模型挂载点校验 ----------
echo ""
echo "【2】模型挂载点校验"
if [ -d "$MODEL_DIR" ]; then
  echo "  ✅ 目录存在：$MODEL_DIR"
  echo "     onnx/model.onnx      : $([ -f "$MODEL_DIR/onnx/model.onnx" ] && echo 有 || echo '★缺')"
  echo "     onnx/model.onnx_data : $([ -f "$MODEL_DIR/onnx/model.onnx_data" ] && echo 有 || echo '★缺')"
else
  echo "  ❌ 目录不存在：$MODEL_DIR"
  echo "     容器挂载不存在的目录会直接创建空目录 → 模型读不到 → 启动失败"
fi

# ---------- 3. 端口占用 ----------
echo ""
echo "【3】端口 18001 占用检查"
if command -v lsof >/dev/null 2>&1; then
  OCC=$(lsof -nP -iTCP:18001 -sTCP:LISTEN -t 2>/dev/null | head -1)
  if [ -n "$OCC" ]; then
    echo "  ⚠️ 18001 被 pid=$OCC 占用 —— 需先释放，否则 docker run 会报 port is allocated"
  else
    echo "  ✅ 18001 空闲"
  fi
fi

# ---------- 4. Docker 实际可用内存（★ 关键）----------
echo ""
echo "【4】Docker 可用内存（★ 常见失败原因）"
echo "  Docker Desktop → Settings → Resources → Memory 分配了多少？"
echo "  下面用一次实测：不带 --memory 限制，试跑 30 秒看是否被 OOM"
docker run --rm --name "$TEST_NAME-mem" "$IMG_FULL" --help >/dev/null 2>&1
echo "  镜像可执行性: $?"

# ---------- 5. ★ 原样重跑 docker run，保留真实错误 ----------
echo ""
echo "=============================================================="
echo " 【5】★ 真实启动测试（不吞任何错误输出）"
echo "=============================================================="
docker rm -f "$TEST_NAME" >/dev/null 2>&1

echo ""
echo "执行命令（参数与上轮完全一致）："
echo "  docker run -d --name $TEST_NAME \\"
echo "    --memory 8g --memory-swap 8g \\"
echo "    -p ${PORT}:80 \\"
echo "    -v ${MODEL_DIR}:/models/bge-m3:ro \\"
echo "    $IMG_FULL \\"
echo "    --model-id /models/bge-m3 --pooling cls --auto-truncate \\"
echo "    --tokenization-workers 2 --max-batch-tokens 2048 --max-client-batch-size 8"
echo ""

# 注意：这里**不重定向**，让所有错误直接显示
docker run -d \
  --name "$TEST_NAME" \
  --memory 8g --memory-swap 8g \
  -p "${PORT}:80" \
  -v "${MODEL_DIR}:/models/bge-m3:ro" \
  "$IMG_FULL" \
  --model-id /models/bge-m3 \
  --pooling cls \
  --auto-truncate \
  --tokenization-workers 2 \
  --max-batch-tokens 2048 \
  --max-client-batch-size 8
RC=$?
echo ""
echo "──────────────────────────────────────────────────────────────"
echo "退出码：$RC"
if [ $RC -ne 0 ]; then
  echo "❌ 启动失败。**上面 docker 输出的就是真实错误**，请贴给我。"
  echo ""
  echo "常见错误对照："
  echo "  · 'port is already allocated'     → 18001/18099 被占用"
  echo "  · 'no such image'                → 镜像名错，需重新 docker load"
  echo "  · 'invalid reference format'      → 镜像名解析错误（用上面精确取到的名字）"
  echo "  · 容器立刻退出/OOM                → 内存不够，把 --memory 8g 去掉试跑"
  echo ""
  echo "容器状态："
  docker ps -a --filter "name=$TEST_NAME" --format '  {{.Names}} | {{.Status}}' | sed 's/^/  /'
  echo "  退出码: $(docker inspect "$TEST_NAME" --format '{{.State.ExitCode}}' 2>/dev/null)"
  echo "  OOM    : $(docker inspect "$TEST_NAME" --format '{{.State.OOMKilled}}' 2>/dev/null)"
  echo ""
  echo "若容器已创建但立刻退出，看它的日志："
  echo "  docker logs $TEST_NAME 2>&1 | tail -30"
else
  echo "✅ 启动命令本身成功"
  echo ""
  echo "  容器状态："
  docker ps -a --filter "name=$TEST_NAME" --format '    {{.Names}} | {{.Status}}' | sed 's/^/  /'
  echo ""
  echo "  若状态是 Up，说明用这个名字就能起来。把上面【1】的镜像名告诉我，我改脚本。"
  echo "  若 30 秒后变成 Exited，看日志：docker logs $TEST_NAME 2>&1 | tail -30"
fi
echo ""
echo "★ 排完把输出贴回来。这次会把真实错误显示出来。"
exit 0