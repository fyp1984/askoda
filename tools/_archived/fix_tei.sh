#!/usr/bin/env bash
# TEI embedding 服务修复脚本 · seven smile 主机
#
# 依据诊断结论（2026-10-06）：
#   ① 容器 Up / 重启 0 次 / OOM=false 但第6 次请求起全挂 → 进程僵死，不是崩溃
#      → 根因：无内存限制 + max-client-batch-size=32 + tokenization worker 默认 8 → 内存耗尽
#   ② --pooling cls 缺失（日志 pooling: None）→ bge-m3 必须 cls 池化，否则向量语义错
#   ③ 模型挂载路径不对（容器内 /models/bge-m3 不存在）→ ORT 后端起不来，回退 candle
#   ④ restart policy = no → 崩了不自起
#
# 用法：bash fix_tei.sh          # 默认只做安全检查并打印将要执行的命令
#      bash fix_tei.sh --apply  # 真正执行（会停旧容器、启动新容器）
#
# ⚠️ 本脚本会重建容器。若那台机器上还有别的容器在跑，本脚本只动 ekos-tei-embedding。

set -uo pipefail

CONTAINER="ekos-tei-embedding"
IMAGE="infiniflow/text-embeddings-inference:cpu-1.8"
#★ 从诊断日志抄下来的宿主挂载源（若该路径不存在，脚本会提示）
SRC="/Users/sevensimle/Documents/WorkSpace/CheersAI/CheersAI-EKOS/external/models/BAAI/bge-m3@5617a9f"
PORT=18001
MEM_LIMIT="8g"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

echo "=============================================================="
echo " TEI 修复脚本 · seven smile · $(date '+%H:%M:%S')"
echo "=============================================================="

# ---------- 前置检查 ----------
echo ""
echo "【前置检查】"

# 1. 模型源是否存在
if [ -d "$SRC" ]; then
  echo "  ✅ 模型源存在：$SRC"
  if [ -f "$SRC/onnx/model.onnx" ]; then
    echo "     onnx/model.onnx 存在（$(du -h "$SRC/onnx/model.onnx" 2>/dev/null | cut -f1)）"
  else
    echo "     ⚠️ onnx/model.onnx 不存在 → ORT 后端用不了，会回退 candle（可用但慢）"
  fi
  if [ -f "$SRC/onnx/model.onnx_data" ]; then
    echo "     onnx/model.onnx_data 存在（$(du -h "$SRC/onnx/model.onnx_data" 2>/dev/null | cut -f1)）"
  else
    echo "     ⚠️ onnx/model.onnx_data 不存在"
  fi
  echo "     顶层：$(ls "$SRC" 2>/dev/null | tr '\n' ' ')"
else
  echo "  ❌ 模型源不存在：$SRC"
  echo "     这是「ORT 后端找不到 model.onnx」的直接原因。"
  echo "     请先确认 bge-m3 实际在哪："
  echo "       find ~ -maxdepth 6 -type d -name 'bge-m3*' 2>/dev/null | head"
  echo "     找到后用下面命令替换 SRC 变量再跑本脚本。"
  exit 1
fi

# 2. Docker 是否在跑
if ! docker ps >/dev/null 2>&1; then
  echo "  ❌ Docker 未运行。请先启动 Docker Desktop 再跑本脚本。"
  exit 1
fi

# 3. 端口是否被占
if command -v lsof >/dev/null 2>&1; then
  OCCUPIED=$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN -t 2>/dev/null | head -1)
  [ -n "$OCCUPIED" ] && echo "  ⚠️ 端口 ${PORT} 已被占用（pid=$OCCUPIED）——重建容器时会释放"
fi

# 4. 磁盘
echo "  磁盘可用：$(df -h / | awk 'NR==2{print $4}')"

# ---------- 修复方案 ----------
echo ""
echo "【将要执行的修复】"
cat <<'FIX'
  1. 加 --pooling cls
     → bge-m3 官方要求 CLS 池化。缺失会让向量语义与本机不一致（静默错误）
  2. --tokenization-workers 降为 2
     → 默认起 8 个 worker，每个都吃内存
  3. --max-client-batch-size 降为 8
     → 默认 32 偏大，批量涌入会瞬间撑爆内存
  4. 加内存限制 8g
     → 原来无限制（0=不限制），会一路吃光直到宿主 OOM
  5. restart policy 改为 unless-stopped
     → 原来 no，崩了不会自起
  6. 修正模型挂载路径
     → 原来容器内 /models/bge-m3 不存在，ORT 后端起不来
FIX

if [ "$APPLY" -ne 1 ]; then
  echo ""
  echo "  以上为预览。加 --apply 执行："
  echo "    bash fix_tei.sh --apply"
  exit 0
fi

# ---------- 真正执行 ----------
echo ""
echo "【执行】"
echo "  停止旧容器..."
docker stop "$CONTAINER" >/dev/null 2>&1 && echo "    已停" || echo "    （本来就没在跑）"
docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "    已删除旧容器" || true

echo "  启动新容器（参数已修正）..."
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --memory "$MEM_LIMIT" \
  --memory-swap "$MEM_LIMIT" \
  -p "${PORT}:80" \
  -v "${SRC}:/models/bge-m3:ro" \
  "$IMAGE" \
  --model-id /models/bge-m3 \
  --pooling cls \
  --auto-truncate \
  --tokenization-workers 2 \
  --max-batch-tokens 2048 \
  --max-client-batch-size 8 \
  >/dev/null 2>&1

if [ $? -ne 0 ]; then
  echo "  ❌ 启动失败。常见原因：内存不足或模型路径问题"
  docker logs "$CONTAINER" 2>&1 | tail -20
  exit 1
fi
echo "    ✅ 已启动"

# ---------- 等待就绪 ----------
echo ""
echo "【等待就绪】（amd64 上加载 bge-m3 约需 1-3 分钟）"
for i in $(seq 1 30); do
  C=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:${PORT}/health 2>/dev/null)
  if [ "$C" = "200" ]; then
    echo "  ✅ 就绪（约 $((i*10)) 秒）"
    break
  fi
  sleep 10
  [ $i -eq 30 ] && { echo "  ❌ 5 分钟未就绪，日志："; docker logs "$CONTAINER" 2>&1 | tail -20; exit 1; }
done

# ---------- 关键验证 ----------
echo ""
echo "【验证 1】ORT 后端是否起来了（这决定性能）"
docker logs "$CONTAINER" 2>&1 | grep -E "Could not start ORT|Starting Bert model" | tail -3 | sed 's/^/  /'
if docker logs "$CONTAINER" 2>&1 | grep -q "Could not start ORT"; then
  echo "  ⚠️ ORT 后端仍失败 → 模型 onnx 文件不完整。用 candle 也能跑，但慢。"
fi

echo ""
echo "【验证 2】embedding 连打 30 次（★ 决定能否替换本机的硬标准）"
OK=0; BAD=0
for i in $(seq 1 30); do
  C=$(curl -s -m 20 -o /dev/null -w "%{http_code}" \
      -X POST http://127.0.0.1:${PORT}/v1/embeddings \
      -H 'Content-Type: application/json' \
      -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null)
  if [ "$C" = "200" ]; then OK=$((OK+1)); else BAD=$((BAD+1)); echo "    第 $i 次失败 http=$C"; fi
  sleep 1
done
echo ""
echo "  结果：成功 $OK / 失败 $BAD"
if [ "$OK" -eq 30 ]; then
  echo "  ✅ 30/30 全过 —— 可以考虑用它替换本机（本机再跑一轮30/30 交叉验证后切换）"
else
  echo "  ❌ 未达标（要求 30/30）——**不要切换本机**，把上面失败次数与容器日志贴回来"
fi

echo ""
echo "  容器状态："
docker ps -a --filter "name=$CONTAINER" --format '    {{.Names}} | {{.Status}}' | sed 's/^/  /'
echo "  重启次数：$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
echo "  内存限制：$(docker inspect "$CONTAINER" --format '{{.HostConfig.Memory}}' 2>/dev/null) bytes"
exit 0