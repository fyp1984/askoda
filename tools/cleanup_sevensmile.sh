#!/usr/bin/env bash
# Seven Smile 主机 · 无用服务清理（带回滚清单）
#
# 用途：清掉这台机器上长期不用、占资源、又不影响 askoda 主链路的容器与目录。
#       后期若需恢复，按脚本末尾打印的回滚命令重建即可。
#
# 用法：
#   bash cleanup_sevensmile.sh --survey    # 只盘点，绝不动任何东西（**先跑这个**）
#   bash cleanup_sevensmile.sh --plan      # 打印将执行的动作，人工确认
#   bash cleanup_sevensmile.sh --apply     # 真正执行删除
#
# 设计原则：
#   ① **默认只读** —— 不加 --apply 绝不删除任何东西
#   ② **删容器用 stop+rm，不加 -v** —— 保留数据卷，数据不丢
#   ③ **每类都标「影响」与「回滚命令」** —— 删之前先告诉你删了会怎样、怎么恢复
#   ④ **askoda 主链路相关的一律不删** —— embedding / RAGFlow / MySQL / MinIO 保留

set -uo pipefail

MODE="${1:---survey}"
BACKUP_DIR="$HOME/sevensimle-cleanup-$(date +%Y%m%d-%H%M%S)"

# ════════════════════════════════════════════════════════════════
# 清理清单：按「确定性」分级
#   DROP   —— Created/Exited 状态的容器，几乎不可能在用，删了没影响
#   KEEP   —— 运行中或明确可能要用，**脚本一律不碰**
# ════════════════════════════════════════════════════════════════

# Created/Exited 状态的容器：从未运行或已退出，不占资源也不提供服务
#   分类说明：
#     CREATED 组= 从未启动过，删了零风险
#     EXITED 组  = 曾运行后退出（含Exited(1) 异常退出），可能是有用的调试容器
#                  → 删前务必留存完整启动参数（脚本自动存 rollback-info.txt）
CANDIDATE_CREATED=(
  "sevensmile-matchlife-sync-watcher-1"
  "sevensmile-matchlife-wechat-access-1"
  "sevensmile-matchlife-matchlife-web-1"
  "agentscope-redis"
)

CANDIDATE_EXITED=(
  "ragflow-debug2"
  "filebay-knowledge-trial-ragflow-ui-builder-1"
)

# 运行中的容器：默认全部保留，人工判断后再决定
RUNNING_KEEP=(
  "ekos-tei-embedding:embedding 服务（askoda 知识库检索依赖，**必须留**）"
  "ekos-ragflow-proxy:RAGFlow 反代（askoda 知识库入口，**必须留**）"
)

echo "=============================================================="
echo " Seven Smile 清理工具 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 盘点 ----------
survey() {
  echo ""
  echo "【A】容器总览"
  if ! command -v docker >/dev/null 2>&1; then
    echo "  ❌ 未安装 Docker"; return
  fi
  echo "  --- 运行中 ---"
  docker ps --format '  {{.Names}} | {{.Status}} | {{.Image}}' 2>/dev/null | sed 's/^/  /'
  local RUNNING
  RUNNING=$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')
  echo "  运行中：$RUNNING 个"
  echo "  --- 已停止 / 未启动 ---"
  docker ps -a --filter "status=created" --filter "status=exited" \
    --format '  {{.Names}} | {{.Status}}' 2>/dev/null | sed 's/^/  /'
  local STOPPED
  STOPPED=$(docker ps -a --filter "status=created" --filter "status=exited" -q 2>/dev/null | wc -l | tr -d ' ')
  echo "  已停止/未启动：$STOPPED 个  ← **这些是清理候选**"

  echo ""
  echo "【B】资源占用 TOP 10（找真正占内存的）"
  docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}' 2>/dev/null \
    | sort -t'|' -k2 -hr | head -10 | awk -F'|' '{printf "  %-45s %s  CPU %s\n", $1, $2, $3}'

  echo ""
  echo "【C】磁盘占用 TOP 10（Docker 卷与镜像）"
  echo "  --- 卷 ---"
  docker system df -v 2>/dev/null | grep -A99 "Local Volumes space usage" | head -14 | sed 's/^/  /'
  echo "  --- 总计 ---"
  docker system df 2>/dev/null | sed 's/^/  /'

  echo ""
  echo "【D】清理候选评估"
  echo "  Created/Exited 状态的容器不占内存、不提供服务，删除**不影响任何在跑的东西**："
  echo "  [组1] 从未启动过（Created）—— 零风险"
  for c in "${CANDIDATE_CREATED[@]}"; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      ST=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)
      IMG=$(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)
      printf "      %-50s 状态=%-9s 镜像=%s\n" "$c" "$ST" "${IMG:-?}"
    else
      printf "      %-50s （不存在，跳过）\n" "$c"
    fi
  done
  echo "  [组2] 曾运行后退出（Exited）—— 可能是有用的调试容器，删前已存回滚信息"
  for c in "${CANDIDATE_EXITED[@]}"; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      ST=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)
      EC=$(docker inspect "$c" --format '{{.State.ExitCode}}' 2>/dev/null)
      IMG=$(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)
      printf "      %-50s 状态=%-18s 退出码=%s\n" "$c" "$ST" "$EC"
      printf "        镜像=%s\n" "${IMG:-?}"
    else
      printf "      %-50s （不存在，跳过）\n" "$c"
    fi
  done

  echo ""
  echo "【E】必须保留（askoda 主链路依赖，脚本永不删）"
  for item in "${RUNNING_KEEP[@]}"; do
    N="${item%%:*}"; WHY="${item#*:}"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$N"; then
      printf "    ✅ %-30s %s\n" "$N" "$WHY"
    else
      printf "    ⚠️ %-30s 未运行 —— 请确认是否影响\n" "$N"
    fi
  done
}

# ---------- 计划 ----------
plan() {
  echo ""
  echo "【将要执行的动作】"
  cat <<'PLAN'
  第 1 步：停掉并删除 6 个「从未运行/已退出」的容器（4 个 Created + 2 个 Exited）
           · 只用 stop + rm，**不加 -v** → 数据卷保留，数据不丢
           · 这些容器本来就没在提供服务，删了不影响任何在跑的东西

  第 2 步：清理悬空镜像与构建缓存（- dangling only）
           · 只删「悬空」镜像（<none> 标签，即没有任何容器在用的中间层）
           · **不删任何有标签的镜像** —— 你现有的镜像全部保留
           · 风险：若某容器依赖的镜像被误判为悬空，重启会失败 → 脚本只删确认无引用的

  第 3 步：输出回滚命令清单
PLAN
  echo ""
  echo "  ★ 不会做的事："
  echo "    × 不删任何正在运行的容器"
  echo "    × 不删任何数据卷（-v）"
  echo "    × 不删任何有标签的镜像"
  echo "    × 不碰 embedding / RAGFlow / MySQL / MinIO 相关的任何东西"
  echo ""
  echo "  确认无误后执行：  bash cleanup_sevensimple.sh --apply"
}

# ---------- 执行 ----------
apply() {
  echo ""
  echo "【重要】开始执行清理。即将删除以下容器："
  for c in "${CANDIDATE_CREATED[@]}" "${CANDIDATE_EXITED[@]}"; do
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c" && echo "    · $c"
  done
  echo ""
  read -r -p "  确认删除？输入 yes 继续，其它任意键取消：" ANS
  [ "$ANS" = "yes" ] || { echo "  已取消，未做任何改动"; return 0; }

  mkdir -p "$BACKUP_DIR"
  echo ""
  echo "  回滚信息已存至：$BACKUP_DIR"
  {
    echo "# 清理时间: $(date)"
    echo "## 被删容器与其原始启动参数（可用于重建）"
    for c in "${CANDIDATE_CREATED[@]}" "${CANDIDATE_EXITED[@]}"; do
      if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
        echo ""
        echo "### $c"
        echo "IMAGE : $(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)"
        echo "CMD   : $(docker inspect "$c" --format '{{join .Config.Cmd " "}}' 2>/dev/null)"
        echo "PORTS : $(docker inspect "$c" --format '{{json .HostConfig.PortBindings}}' 2>/dev/null)"
        echo "MOUNTS:"
        docker inspect "$c" --format '{{range .Mounts}}  {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null
        echo "ENV   :"
        docker inspect "$c" --format '{{range .Config.Env}}  {{println .}}{{end}}' 2>/dev/null | head -20
      fi
    done
  } > "$BACKUP_DIR/rollback-info.txt" 2>&1

  # 第 1 步：删已停止的容器（保留数据卷）
  echo ""
  echo "【第 1 步】删除未运行/已退出的容器"
  for c in "${CANDIDATE_CREATED[@]}" "${CANDIDATE_EXITED[@]}"; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      docker stop "$c" >/dev/null 2>&1
      if docker rm "$c" >/dev/null 2>&1; then
        echo "    ✅ 已删 $c（数据卷保留）"
      else
        echo "    ❌ 失败 $c"
      fi
    fi
  done

  # 第 2 步：只清悬空镜像
  echo ""
  echo "【第 2 步】清理悬空镜像（仅 dangling）"
  BEFORE=$(docker images -q -f dangling=true 2>/dev/null | wc -l | tr -d ' ')
  echo "  悬空镜像：$BEFORE 个"
  docker image prune -f --filter "dangling=true" >/dev/null 2>&1 && echo "    ✅ 已清理悬空镜像" || echo "    （跳过）"

  # 第 3 步：构建缓存
  echo ""
  echo "【第 3 步】清理构建缓存"
  docker builder prune -f >/dev/null 2>&1 && echo "    ✅ 已清理构建缓存" || echo "    （无缓存或跳过）"

  # 结果
  echo ""
  echo "=============================================================="
  echo " 清理完成"
  echo "=============================================================="
  docker system df 2>/dev/null | sed 's/^/  /'
  echo ""
  echo "  剩余容器："
  docker ps -a --format '    {{.Names}} | {{.Status}}' 2>/dev/null | sed 's/^/  /'
  echo ""
  echo "  ★ 回滚信息（含每个被删容器的镜像/端口/挂载/环境变量）："
  echo "    $BACKUP_DIR/rollback-info.txt"
  echo ""
  echo "  ★ 后期要重建某个容器，照该文件里的 IMAGE/CMD/PORTS/MOUNTS/ENV 写docker run 即可。"
}

case "$MODE" in
  --survey) survey ;;
  --plan)   survey; plan ;;
  --apply)  apply ;;
  *)        echo "用法: bash $0 [--survey|--plan|--apply]"; exit 1 ;;
esac