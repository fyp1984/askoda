#!/usr/bin/env bash
# 首次初始化一键脚本 · askoda
#
# 做的事：
#   ① 采集元数据（把库表结构与字段说明写进元数据库字典）
#   ② 可选：导入 knowledge/ 下的知识文档（需要 RAGFlow 已就绪）
#   ③ 可选：种入业务口径词条
#
# 为什么用这个脚本：采集脚本要import网关内部模块并连数据库，
# 直接在宿主机跑需要先装 psycopg 等依赖，小白极易卡住。
# 本脚本改为**在网关容器内执行**——依赖已在镜像里，无需额外准备。
#
# 平台：macOS / Linux / WSL2 原生可跑。
#      Windows 原生（PowerShell/cmd）无 bash，需先用 WSL2 或 Git Bash。
#
# 用法：
#   bash tools/init_first_run.sh              # 只做① 元数据采集（安全、可重复跑）
#   bash tools/init_first_run.sh --with-knowledge   # 连② 一起做（需要 RAGFlow）
#   bash tools/init_first_run.sh --all              # 全部做

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WITH_KNOWLEDGE=0
WITH_GLOSSARY=0
for a in "$@"; do
  case "$a" in
    --with-knowledge) WITH_KNOWLEDGE=1 ;;
    --all) WITH_KNOWLEDGE=1; WITH_GLOSSARY=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$a（用 --help 看用法）"; exit 1 ;;
  esac
done

GATEWAY_CONTAINER="${GATEWAY_CONTAINER:-askoda}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 前置检查 ----------
say "检查网关容器是否在运行"
if ! docker ps --format '{{.Names}}' | grep -qx "$GATEWAY_CONTAINER"; then
  die "找不到容器「$GATEWAY_CONTAINER」。请先启动：docker compose up -d --build
    （容器没起来多半是因为 .env 里两项必填没填，见部署手册第 3 步）"
fi
echo "  ✅ $GATEWAY_CONTAINER 在运行"

# 元数据库 DSN：从 .env 取端口，账号密码用 compose 里的默认值。
# 不写死是为了跟compose 保持一致——compose 改了这里也会跟着对。
PG_PORT="$(grep -E '^ASSISTANT_PG_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' \r')"
PG_PORT="${PG_PORT:-15434}"
export ASSISTANT_DB_DSN="postgresql://assistant:assistant@host.docker.internal:${PG_PORT}/assistant"

# ---------- ① 元数据采集 ----------
say "① 采集元数据（表结构 / 字段说明）"
if docker exec -e PYTHONPATH=/app:/app/gateway \
             -e ASSISTANT_DB_DSN="$ASSISTANT_DB_DSN" \
             "$GATEWAY_CONTAINER" \
             sh -c 'cd /app && python tools/collect_metadata.py --dataset A --dataset B --seed-glossary'; then
  echo "  ✅ 元数据采集完成"
else
  die "元数据采集失败。上面是真实报错，先看是不是数据库没起：
    docker compose ps    # assistant-postgres 应该是 healthy"
fi

# ---------- ② 知识文档入库 ----------
if [ "$WITH_KNOWLEDGE" = "1" ]; then
  say "② 导入知识文档（需要 RAGFlow 已就绪）"
  echo "  这一步会读 .env 里的 KNOWLEDGE_API_KEY / KNOWLEDGE_DATASET_ID"
  if docker exec -e PYTHONPATH=/app:/app/gateway \
               -e KNOWLEDGE_API_URL="${KNOWLEDGE_API_URL:-}" \
               "$GATEWAY_CONTAINER" \
               sh -c 'cd /app && python tools/ingest_knowledge.py'; then
    echo "  ✅ 知识文档导入完成"
  else
    echo "  ⚠️ 知识文档导入失败。常见原因："
    echo "     · .env 里 KNOWLEDGE_API_KEY / KNOWLEDGE_DATASET_ID 没填"
    echo "     · RAGFlow 没启动或地址不对（看部署手册 3.3）"
    echo "     这不影响主流程，先继续下一步。"
  fi
fi

# ---------- ③ 业务口径 ----------
if [ "$WITH_GLOSSARY" = "1" ]; then
  say "③ 种入业务口径词条"
  if docker exec -e PYTHONPATH=/app:/app/gateway \
               -e ASSISTANT_DB_DSN="$ASSISTANT_DB_DSN" \
               "$GATEWAY_CONTAINER" \
               sh -c 'cd /app && python tools/seed_glossary.py --apply'; then
    echo "  ✅ 业务口径已写入"
  else
    echo "  ⚠️ 业务口径写入失败，不影响主流程"
  fi
fi

printf '\n\033[32m========================================\033[0m\n'
printf '\033[32m 初始化完成。下一步：打开浏览器验证\033[0m\n'
printf '\033[32m   http://127.0.0.1:18081/\033[0m\n'
printf '\033[32m========================================\033[0m\n'
echo
echo "再跑一次工程自检（可选）："
echo "  python3 tools/gate_all.py --layers G0,G1"
