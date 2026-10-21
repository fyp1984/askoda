#!/usr/bin/env bash
# 跑本仓库的自测 / 门禁，自动挂上仓库自带的 sitecustomize 垫片。
#
# 为什么需要这个脚本
# ------------------
# 本机（WorkBuddy 沙箱）禁止任何"让文件从某路径消失"的操作，而 Python 的
# tempfile 用「建一个探测文件再删掉」来验证目录可用 ⇒ 探测必失败 ⇒
# 任何用到 tempfile 的脚本起手就红。
#
# ⚠️ 关键：**Python 只加载 sys.path 里第一个找到的 `sitecustomize`**。
#   本机 PYTHONPATH 指向 WorkBuddy 自带的 shim，它内部第 807 行会调
#   `tempfile.gettempdir()` ⇒ 它自己就把 sitecustomize 加载搞炸。
#   ⇒ 光"再加一个 PYTHONPATH"没用，必须让**我们的垫片排在它前面**，
#     使 WorkBuddy 那个**根本没被加载**。
#
# 用法：
#   bash tools/run_selftest.sh                    # 跑全量门禁（日常 G0+G1）
#   bash tools/run_selftest.sh tools/xxx.py       # 跑单个脚本
#   bash tools/run_selftest.sh --strict           # 传给 gate_all.py 的参数
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SHIM_DIR="$HERE/_pyshim"
if [ ! -f "$SHIM_DIR/sitecustomize.py" ]; then
  echo "FATAL: 找不到垫片 $SHIM_DIR/sitecustomize.py" >&2
  exit 2
fi

# 我们的垫片必须排在**最前面**（含 WorkBuddy 自带的 shim 之前），
# 否则 Python 会先加载 WorkBuddy 那个并因 tempfile 探测失败而炸掉。
export PYTHONPATH="$SHIM_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 解释器选择：**单脚本直跑**时必须用「带 sqlglot/psycopg 的那个」，
# 否则需要 gateway/ 的脚本（gates_selftest / reqcov_selftest / …）会直接
# `ModuleNotFoundError: No module named 'sqlglot'` 崩掉 ——
# 而那**不是脚本坏了，是用错了解释器**。
# ⚠️ 整包门禁路径（gate_all.py）不需要这个：它自己会探测并选子解释器
#    （见 gate_all.py 的 _python_candidates / REQUIRED_MODULES）。
# ⚠️ 探测口径与门禁保持一致：优先 VIRTUAL_ENV → 项目 venv → 宿主 venv → 系统 python。
PY="${PY:-}"
if [ -z "$PY" ]; then
  for _cand in \
      "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python3}" \
      "$ROOT/.venv/bin/python3" \
      "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" \
      "$(command -v python3 2>/dev/null)"
  do
    if [ -x "$_cand" ] && "$_cand" -c "import sqlglot" >/dev/null 2>&1; then
      PY="$_cand"; break
    fi
  done
fi
# 一个都没探到 ⇒ 退回 python3，让脚本自己报出真实错误（不静默换解释器）
PY="${PY:-python3}"

if [ "$#" -eq 0 ]; then
  # 默认：日常门禁（G0 静态 + G1 单元自证），不写库、不连真实服务
  exec "$PY" "$ROOT/tools/gate_all.py" --layers G0,G1
fi

# 第一个参数是 .py 脚本 ⇒ 直接跑它；否则整包传给 gate_all.py
case "$1" in
  *.py)
    script="$1"; shift
    exec "$PY" "$ROOT/$script" "$@"
    ;;
  *)
    exec "$PY" "$ROOT/tools/gate_all.py" "$@"
    ;;
esac
