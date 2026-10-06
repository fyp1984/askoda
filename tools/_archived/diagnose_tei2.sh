#!/usr/bin/env bash
# TEI 深度诊断 v2 · seven smile 主机
#
# 依据 fix_tei.sh 执行结果（2026-10-06 10:15）：
#   · 加了参数 + 8g 内存限 + unless-stopped 之后，仍然 12/30 失败
#   · **重启次数 = 1** → 容器真的死过一次，不是僵死
#   · ORT 后端仍失败：/models/bge-m3/onnx/model.onnx does not exist
#   · 模型目录顶层是 pytorch_model.bin / colbert_linear.pt / sparse_linear.pt
#     → **那是 PyTorch 权重格式，压根没有 ONNX 文件**
#
# 核心判断：不是参数问题，是**模型格式不匹配**。
# TEI 需要 ONNX（--dtype/后端走 onnx）或 candle 能吃的权重；
# 而 bge-m3 的 PyTorch 版是 ColBERT 结构（colbert_linear/sparse_linear），
# TEI 的 candle 后端无法正确加载 → 反复崩溃。
#
# 用法：bash diagnose_tei2.sh
# 本脚本只读+打印，不修改任何东西。

set -uo pipefail

CONTAINER="ekos-tei-embedding"

echo "=============================================================="
echo " TEI 深度诊断 v2 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 1. 模型格式判定（★ 本次核心）----------
echo ""
echo "【1】模型格式判定（核心）"
docker inspect "$CONTAINER" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null | sed 's/^/  挂载: /'
SRC=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/models/bge-m3"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)

if [ -n "${SRC:-}" ] && [ -d "$SRC" ]; then
  echo "  模型源：$SRC"
  echo "  --- 顶层文件 ---"
  ls "$SRC" 2>/dev/null | sed 's/^/    /'

  echo ""
  HAS_ONNX=0; HAS_PT=0
  if [ -d "$SRC/onnx" ]; then
    HAS_ONNX=1
    echo "  ✅存在 onnx/ 目录"
    ls -la "$SRC/onnx" 2>/dev/null | tail -n +2 | awk '{printf "      %10s  %s\n", $5, $9}'
  else
    echo "  ❌ 没有 onnx/ 目录  ← ★ 这就是 ORT 后端起不来的原因"
  fi

  [ -f "$SRC/pytorch_model.bin" ] && { HAS_PT=1; echo "  ⚠️ 有 pytorch_model.bin（PyTorch 权重，TEI 不直接吃）"; }
  [ -f "$SRC/colbert_linear.pt" ] && echo "  ⚠️ 有 colbert_linear.pt → 这是 ColBERT 结构，TEI candle 后端无法正确处理"
  [ -f "$SRC/sparse_linear.pt" ] && echo "  ⚠️ 有 sparse_linear.pt → 稀疏检索头，TEI 只做 dense，会出问题"

  echo ""
  if [ "$HAS_ONNX" -eq 0 ]; then
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║ ★ 病根确认：挂的是 PyTorch 版模型，没有 ONNX 文件    ║"
    echo "  ║   → TEI 的 ORT 后端必然起不来                ║"
    echo "  ║   → candle 后端加载 ColBERT 结构会崩            ║"
    echo "  ║   → 这解释了「参数都改对了还是挂」                    ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "  【解法】换成 ONNX 版模型。两种途径："
    echo "   A. 用本机的拉取脚本（推荐，见下方命令）"
    echo "   B. 从本机直接把 ONNX 目录 scp 过去"
  else
    echo "  模型格式看起来正确，继续看后面的检查"
  fi
else
  echo "  ⚠️ 拿不到模型源路径（容器可能已删）"
fi

# ---------- 2. 崩溃真相 ----------
echo ""
echo "【2】崩溃真相（重启次数 = 1 说明真死过）"
docker ps -a --filter "name=$CONTAINER" --format '  状态: {{.Status}}' 2>/dev/null
echo "  重启次数: $(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)"
echo "  退出码  : $(docker inspect "$CONTAINER" --format '{{.State.ExitCode}}' 2>/dev/null)  （137=OOM被杀，143=SIGTERM）"
echo "  OOMKilled: $(docker inspect "$CONTAINER" --format '{{.State.OOMKilled}}' 2>/dev/null)"
echo "  内存限制: $(docker inspect "$CONTAINER" --format '{{.HostConfig.Memory}}' 2>/dev/null) bytes"
echo "  --- 最近 25 行日志（找崩溃点）---"
docker logs --tail 25 "$CONTAINER" 2>&1 | sed 's/^/    /'

# ---------- 3. 崩溃模式 ----------
echo ""
echo "【3】崩溃模式统计"
docker logs "$CONTAINER" 2>&1 | grep -ciE "panic|overflow|candle|ORT|error" | xargs echo "  错误相关日志行数:"
echo "  --- 有无 candle 加载后的崩溃 ---"
docker logs "$CONTAINER" 2>&1 | grep -iE "panic|overflow|backtrace|memory allocation" | tail -5 | sed 's/^/    /' || echo "    无 panic"

# ---------- 4. 宿主机内存 ----------
echo ""
echo "【4】宿主机内存（macOS Docker Desktop 场景）"
if command -v vm_stat >/dev/null 2>&1; then
  echo "  （macOS：用活动监视器看 Docker Desktop 内存占用，或）"
  vm_stat 2>/dev/null | head -5 | sed 's/^/    /'
fi
echo "  Docker Desktop 分配的内存请在 GUI 里确认："
echo "    Docker Desktop → Settings → Resources → Memory"
echo "  bge-m3 用 ONNX 后端推理，建议至少给 6-8GB"

# ---------- 5. 能否复用本机已有 ONNX 模型 ----------
echo ""
echo "【5】修好后的验收标准（务必照做）"
cat <<'STD'
  换ONNX 模型后，连打 30 次必须 30/30 全过。
  只跑通一次不算——之前那次「前 5 次成功」就是假象，低负载才过。
STD

exit 0