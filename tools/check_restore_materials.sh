#!/usr/bin/env bash
# TEI 重建物料检查 · seven smile 主机
#
# 背景：Downloads 目录下发现两个归档包
#   tei-cpu-1.8.tar   —— 疑为 TEI 镜像归档（docker save 产物）
#   bge-m3.tar        —— 疑为模型归档
#
# 本脚本**只读检查**，不加载、不解压、不修改任何东西。
# 目的：先确认这两个包到底是什么、模型是不是 ONNX 格式，再决定怎么重建。
#
# 用法：bash check_restore_materials.sh
#
# 为什么要先查：
#   上一轮问题就是「挂的是 PyTorch 版模型、没有 ONNX 文件」，
#   而 ONNX 在 bge-m3 仓库的 onnx/ 子目录里。这个包若是完整的，恰好能解决那个问题。
#   但不能假设——必须先看包里有什么。

set -uo pipefail

cd ~/Downloads 2>/dev/null || { echo "找不到 ~/Downloads"; exit 1; }

TEI_TAR="tei-cpu-1.8.tar"
MODEL_TAR="bge-m3.tar"

echo "=============================================================="
echo " 重建物料检查 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 文件基本信息 ----------
echo ""
echo "【1】文件基本信息"
for f in "$TEI_TAR" "$MODEL_TAR"; do
  if [ -f "$f" ]; then
    printf "  %-20s %s\n" "$f" "$(du -h "$f" | cut -f1)"
  else
    printf "  %-20s ★ 不存在\n" "$f"
  fi
done
echo ""
echo "  提示：bge-m3 的 ONNX 版约 2.2GB；若 bge-m3.tar 明显小于此，说明不含 ONNX。"

# ---------- 判断类型 ----------
echo ""
echo "【2】★ 关键判断：这是 docker save 镜像，还是普通文件归档？"

check_type() {
  local f="$1"
  [ -f "$f" ] || { echo "  $f —— 不存在"; return; }
  echo ""
  echo "  ── $f ──"
  # docker save 产物的特征：老格式= <hash>/ 层目录；新格式(OCI)= blobs/sha256/<hash> + oci-layout + index.json
  local HEAD
  HEAD=$(tar -tf "$f" 2>/dev/null | head -30)
  if echo "$HEAD" | grep -qE "^blobs/sha256/[a-f0-9]{64}" \
     || echo "$HEAD" | grep -qE "^(oci-layout|index\.json)$" \
     || echo "$HEAD" | grep -qE "^[a-f0-9]{64}/"; then
    echo "  ✅ 判断：**Docker 镜像归档**（docker save 产物）"
    echo "     → 用 docker load -i 加载"
    if echo "$HEAD" | grep -qE "^blobs/sha256/"; then
      echo "     → OCI 格式（blobs/sha256/ + index.json）"
    fi
  else
    echo "  ℹ️ 判断：**普通文件归档**（tar 打包的目录或文件）"
    echo "     → 解压后按目录使用"
    echo ""
    echo "     包内前 30 项："
    echo "$HEAD" | sed 's/^/       /'
  fi
}

check_type "$TEI_TAR"
check_type "$MODEL_TAR"

# ---------- 深度检查模型包 ----------
echo ""
echo "【3】★ 模型包内容详查（决定能否解决上一轮的 ONNX 缺失问题）"
if [ -f "$MODEL_TAR" ]; then
  echo ""
  echo "  --- 顶层结构 ---"
  tar -tf "$MODEL_TAR" 2>/dev/null | awk -F/ '{print $1"/"$2}' | sort -u | head -20 | sed 's/^/    /'

  echo ""
  echo "  --- 判定 ONNX 是否存在（★ 关键）---"
  ONNX_HIT=$(tar -tf "$MODEL_TAR" 2>/dev/null | grep -iE "\.onnx" | head -10)
  if [ -n "$ONNX_HIT" ]; then
    echo "  ✅ 包内有 ONNX 文件："
    echo "$ONNX_HIT" | sed 's/^/       /'
  else
    echo "  ❌ 包内**没有 .onnx 文件** → 若是 PyTorch 版，重建后仍会复现上一轮的问题"
    echo "     （TEI 的 ORT 后端会再次报 model.onnx does not exist）"
  fi

  echo ""
  echo "  --- 关键文件大小（模型是否完整）---"
  # tar -tvf 能看到大小
  tar -tvf "$MODEL_TAR" 2>/dev/null | grep -iE "\.onnx_data|\.onnx|pytorch_model\.bin|\.safetensors" \
    | awk '{printf "    %10s  %s\n", $3, $NF}' | head -10

  PT=$(tar -tvf "$MODEL_TAR" 2>/dev/null | grep -c "pytorch_model.bin")
  if [ "$PT" -gt 0 ]; then
    echo ""
    echo "  ⚠️ 含 pytorch_model.bin → 这是 PyTorch 版权重"
    echo "     TEI 可用 candle 后端加载，但 bge-m3 的 ColBERT 结构可能出问题"
  fi
fi

# ---------- 结论 ----------
echo ""
echo "=============================================================="
echo " 检查结论"
echo "=============================================================="
echo ""
echo "【下一步该怎么走】"
echo ""
echo "  情况 A：两个都是 Docker 镜像归档"
echo "    → 最省事：docker load 加载镜像，模型在镜像里，直接重建容器即可"
echo "       docker load -i tei-cpu-1.8.tar"
echo "       docker load -i bge-m3.tar    # 若这是模型镜像包"
echo ""
echo "  情况 B：tei-cpu-1.8.tar 是镜像归档，bge-m3.tar 是模型文件包"
echo "    → 加载镜像 → 解压模型 → 挂载进容器"
echo ""
echo "  情况 C：模型包里没有 onnx/（只有 pytorch_model.bin）"
echo "    → 重建后仍会复现上一轮问题，不建议重建；"
echo "       要么从 Mac 传本机的 ONNX 版，要么接受用 candle 慢路径"
echo ""
echo "★ 把本脚本的输出贴给我，我给你对应的重建命令（不猜）。"
echo ""
echo "  提醒：解包前先看大小，确认不会把磁盘撑爆："
echo "    df -h ~ | tail -1"
echo "    tar -tvf $MODEL_TAR | wc -l"
exit 0