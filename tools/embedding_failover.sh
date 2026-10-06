#!/usr/bin/env bash
# embedding 主备切换器 · askoda 本机
#
# 背景：知识库检索的 embedding 已迁至 seven smile（100.103.240.78:18001）为主。
#      该主机一旦异常，检索立即全断（RAGFlow 的 base_url 是静态字符串，不会自己切）。
#      本机保留 TEI 作为**备用**（compose 里已加 profiles: ["local-embed"]，默认不启动）。
#
# 策略：**seven smile 为主，本机为备**。切换有冷却与回切保护，避免抖动。
#
# 用法：
#   bash tools/embedding_failover.sh status     # 看当前状态
#   bash tools/embedding_failover.sh check      # 只探活，不改任何东西（主备都探）
#   bash tools/embedding_failover.sh switch-back # 主 → 备（本机 TEI）
#   bash tools/embedding_failover.sh switch-main # 备 → 主（seven smile）
#   bash tools/embedding_failover.sh auto       # 自动判断：主不通则切备，不通则切回主
#
# 依赖：RAGFlow MySQL（rag_flow.tenant_model_instance.extra 的 base_url）
#      实测：改完立即生效，**无需重启 RAGFlow**。

set -uo pipefail

# ---------- 配置 ----------
PRIMARY_URL="100.103.240.78:18001"      # seven smile（主）
BACKUP_URL="host.docker.internal:18002"  # 本机 TEI（备）
PRIMARY_NAME="seven smile（主）"
BACKUP_NAME="本机 TEI（备）"

MYSQL_C="filebay-knowledge-trial-mysql-1"
COOLDOWN_FILE="/tmp/askoda_embedding_failover_last"   # 记录上次切换时间戳

cd "$(dirname "$0")/.." || exit 1

# ---------- 工具函数 ----------
probe() {
  # $1=host:port  返回 0=通
  local hp="$1"
  [ "$hp" = "host.docker.internal:18002" ] && hp="127.0.0.1:18002"
  local code
  code=$(curl -s -m 8 --noproxy '*' -o /dev/null -w "%{http_code}" \
         -X POST "http://${hp}/v1/embeddings" \
         -H 'Content-Type: application/json' \
         -d '{"input":"探活","model":"/models/bge-m3"}' 2>/dev/null)
  [ "$code" = "200" ]
}

get_current() {
  # 从 RAGFlow MySQL 读当前 base_url
  docker exec "$MYSQL_C" sh -c \
    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" rag_flow -N -B -e \
     "SELECT extra FROM tenant_model_instance WHERE extra LIKE \"%1800%\"" 2>/dev/null' \
    2>/dev/null | head -1 | grep -oE '(100\.103\.240\.78|host\.docker\.internal):[0-9]+'
}

set_url() {
  # $1=目标（PRIMARY/BACKUP）  $2=目标端口
  # 拼出**完整**的 host:port，整串替换
  local NEW_URL
  if [ "$1" = "PRIMARY" ]; then
    NEW_URL="$PRIMARY_URL"
  else
    NEW_URL="$BACKUP_URL"
  fi
  # ★ 用「正则整体替换 base_url 的值」，不靠 REPLACE 猜具体字符串。
  #   踩过两个坑：
  #     ① 只换端口号 → 远端地址被改成 100.103.240.78:18002（主机没变、端口变本机的），
  #        是个不存在的组合，检索静默全断 —— 「半改」比「不改」更危险。
  #     ② 用 REPLACE 匹配已知串→ 一旦当前值是「坏地址」（如 100.103.240.78:18002），
  #        两个模式都匹配不上，切换静默失败。
  #   正解：用 REGEXP 抓出 host:port 并整体换成目标值，与当前值长什么样无关。
  docker exec "$MYSQL_C" sh -c \
    "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" rag_flow -e \
     \"UPDATE tenant_model_instance SET
        extra = REPLACE(extra,
          REGEXP_SUBSTR(extra, '[A-Za-z0-9._-]+:[0-9]+'),
          '$NEW_URL')
      WHERE extra LIKE '%1800%'\"" >/dev/null 2>&1
}

ensure_backup_up() {
  # 确保本机备用 TEI 在跑（未跑则用 profile 启动）
  if probe "$BACKUP_URL"; then
    return 0
  fi
  echo "  本机备用 TEI 未就绪，尝试启动（compose profile: local-embed）…"
  export PATH="$HOME/.orbstack/bin:$PATH"
  docker compose --profile local-embed up -d tei-embedding >/dev/null 2>&1
  echo -n "  等待模型加载（amd64 模拟，需 40~180s）"
  for i in $(seq 1 24); do
    if probe "$BACKUP_URL"; then
      echo " → 就绪（约 $((i*10)) 秒）"
      return 0
    fi
    echo -n "."
    sleep 10
  done
  echo " ✗ 240 秒仍未就绪"
  return 1
}

in_cooldown() {
  # 冷却期内不重复切，避免抖动
  [ -f "$COOLDOWN_FILE" ] || return 1
  local last now gap
  last=$(cat "$COOLDOWN_FILE")
  now=$(date +%s)
  gap=$((now - last))
  [ "$gap" -lt 300 ] && { echo "  ⏳ 冷却中（距上次切换 ${gap}s，<300s），本次不切"; return 0; }
  return 1
}

mark_switch() { date +%s > "$COOLDOWN_FILE"; }

# ---------- 命令 ----------
cmd_status() {
  echo "=============================================================="
  echo " embedding 主备状态"
  echo "=============================================================="
  local cur; cur=$(get_current)
  echo "  主（${PRIMARY_NAME}）：$PRIMARY_URL"
  echo "  备（${BACKUP_NAME}）：$BACKUP_URL"
  echo ""
  echo "  RAGFlow 当前指向：${cur:-读取失败}"
  echo ""
  echo "  --- 探活 ---"
  probe "$PRIMARY_URL" && echo "    主 ✅ 通" || echo "    主 ❌ 不通"
  probe "$BACKUP_URL"  && echo "    备 ✅ 通" || echo "    备 ⚠️ 不通（需 --profile local-embed 启动）"
  echo ""
  if [ "$cur" = "$PRIMARY_URL" ]; then
    echo "  当前用：主（${PRIMARY_NAME}）"
  elif [ "$cur" = "$BACKUP_URL" ]; then
    echo "  当前用：备（${BACKUP_NAME}）"
  else
    echo "  当前：未知"
  fi
}

cmd_check() {
  echo "--- 探活（不改动任何东西）---"
  probe "$PRIMARY_URL" && echo "  主（${PRIMARY_NAME}）✅ 通" || echo "  主（${PRIMARY_NAME}）❌ 不通"
  probe "$BACKUP_URL"  && echo "  备（${BACKUP_NAME}）✅ 通" || echo "  备（${BACKUP_NAME}）❌ 不通"
}

cmd_switch_to() {
  # $1=目标（PRIMARY/BACKUP）  $2=名称
  local target="$1" name="$2" port addr
  if [ "$target" = "PRIMARY" ]; then
    port=18001; addr="$PRIMARY_URL"
  else
    port=18002; addr="$BACKUP_URL"
  fi

  echo "→ 切到 ${name}（${addr}）"
  # 目标若是本机备，需先把备用 TEI 拉起来
  [ "$target" = "BACKUP" ] && ensure_backup_up
  # ★ 注意：probe 必须传**地址**，不能传 PRIMARY/BACKUP 这种标识符
  if ! probe "$addr"; then
    echo "  ✗ 目标不可用，放弃切换（保持现状）"
    return 1
  fi
  set_url "$target" "$port"
  local now; now=$(get_current)
  # ★ 同样要拿地址比对，不能拿 PRIMARY/BACKUP 标识符比
  if [ "$now" = "$addr" ]; then
    mark_switch
    echo "  ✅ 已切到 ${name}"
    return 0
  fi
  echo "  ❌ 切换未生效（期望=${addr} 实际=${now}）"
  return 1
}

case "${1:-status}" in
  status)      cmd_status ;;
  check)       cmd_check ;;
  switch-back) cmd_switch_to "BACKUP"  "$BACKUP_NAME" ;;
  switch-main) cmd_switch_to "PRIMARY" "$PRIMARY_NAME" ;;
  auto)
    echo "=== 自动判断 ==="
    cur=$(get_current)
    if probe "$PRIMARY_URL"; then
      echo "  主 ✅ 通"
      if [ "$cur" != "$PRIMARY_URL" ]; then
        in_cooldown && exit 0
        echo "  当前在用备→ 切回主"
        cmd_switch_to "PRIMARY" "$PRIMARY_NAME"
      else
        echo "  已在用主，无需动作"
      fi
    else
      echo "  主 ❌ 不通"
      if [ "$cur" = "$PRIMARY_URL" ]; then
        in_cooldown && exit 0
        echo "  当前在用主 → 切到备"
        cmd_switch_to "BACKUP" "$BACKUP_NAME"
      else
        echo "  已在用备，主不通 → 等主恢复后重试（可跑 auto 自动切回）"
      fi
    fi
    ;;
  *) echo "用法: bash $0 [status|check|auto|switch-back|switch-main]"; exit 1 ;;
esac
exit 0