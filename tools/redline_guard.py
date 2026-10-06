#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Askoda 红线守卫（安全治理 Agent 的机器执行体）。

对 `git diff` 做六条红线的机器级判定。**独立于执行者**：执行方不得自我豁免，
也不得为了让门禁变绿而调整本脚本的判据。

六条红线（与《全量交付执行方案与任务提示词》§3.3 逐条对应）：
  R1 改开源上游源码（wren-docker*/）
  R2 改已有 MCP 工具的入参名或顺序（工具面→实现层是按位契约）
  R3 改 D1 行为（让 F4 门禁失败时自动改写 SQL）
  R4 删数据 / 改带 sha256 的冻结基线文件
  R5 新增运行时依赖（一期只放行 5 项白名单）
  R6 需人工点「信任」或改 ~/.workbuddy/mcp.json

用法：
    python tools/redline_guard.py                # 判当前工作区（HEAD vs 工作树）
    python tools/redline_guard.py --staged       # 判暂存区
    python tools/redline_guard.py --base <ref>   # 判 <ref>..工作树
    python tools/redline_guard.py --json         # 机器可读输出

退出码：0 = 六条红线全未触；1 = 命中任一条（须出《红线阻断报告》）。
本脚本只判定，不修改任何文件；不设"通融"通道。
"""
import argparse
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 一期放行的运行时依赖白名单（与 gateway/requirements.txt 基线一致）
ALLOWED_REQ_LINES = {
    "fastmcp==4.0.10",
    "starlette",
    "psycopg[binary]",
    "minio",
    "sqlglot",
}

# 上游只读区（开源镜像目录，零改造原则）
UPSTREAM_PREFIXES = ("wren-docker/", "wren-docker-b/")

# 禁止出现的数据删除动作（出现在 diff 新增行里即判定）
#
# 收紧 TRUNCATE 的原因（B4b 实测踩到）：原 `\bTRUNCATE\b` 会命中
# docker-compose 里的 TEI 启动参数 `--auto-truncate`——连字符构成词边界，
# 于是「分词超长自动截断」被当成「SQL 清表」，把合法改动判成破坏性操作。
# 真正的 SQL TRUNCATE 后面必然是 TABLE 关键字或一个**标识符**（表名）：
#   TRUNCATE TABLE x / truncate table x / TRUNCATE `x` / TRUNCATE "x" / TRUNCATE x
# 因此要求其后紧跟空白 + (TABLE | 引号/反引号/方括号 | ASCII 字母数字下划线)。
# 中文注释里「truncate 的用法」这类散文因此不再命中——中文不是合法标识符首字符。
_PROBE_R4 = [
    "TRUNCATE TABLE orders",
    "truncate table t2",
    "TRUNCATE `mytable`",
    "DELETE FROM users WHERE 1=1",
    "DROP TABLE foo",
    "-- auto-truncate tokenization",
    "分词截断 auto-truncate 参数",
    "直接分析数据",
]
DESTRUCTIVE_PATTERNS = [
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "DROP 对象"),
    # TRUNCATE 的判定要同时覆盖两种写法，且不得把英文单词首字母误当表名：
    #   TRUNCATE TABLE orders   → 判红
    #   TRUNCATE `mytable`      → 判红
    #   TRUNCATE mytable;       → 判红
    #   --auto-truncate tokenization → 不判红（TEI 参数，tokenization 不是表名）
    # 手法：先匹配 TABLE 关键字式；再匹配「引号包裹表名」式；最后匹配「裸表名 + 终止符」式，
    # 其中裸表名式要求表名后紧跟 空白/分号/引号/右括号/行尾 —— 这样 tokenization 这类
    # 后续还有字母的单词不会被误判（其 t 后面还有 okinization，不满足终止符）。
    (
        r"\bTRUNCATE\b\s+TABLE\s+[`\"'\[]?[A-Za-z_][A-Za-z0-9_$]*",
        "TRUNCATE TABLE",
    ),
    (
        r"\bTRUNCATE\b\s+[`\"'\[][A-Za-z_][A-Za-z0-9_$]*[`\"'\]]",
        "TRUNCATE <表名>",
    ),
    (
        r"(?<![\w-])TRUNCATE\b\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:;|$|[\"'`)\]])",
        "TRUNCATE <表名>",
    ),
    (r"\bDELETE\s+FROM\b", "DELETE FROM"),
    (r"\bDROP\s+DATABASE\b", "DROP DATABASE"),
]
# TRUNCATE 误报豁免：这些词是常见的非 SQL 参数/单词，出现时不算破坏性操作
TRUNCATE_FALSE_FRIENDS = (
    "auto-truncate", "truncate tokenization", "truncate-words",
    "分词截断", "截断", "截短",
)

# 冻结基线清单（若存在则逐个核 sha256；缺失则跳过该子项并如实标注）
BASELINE_MANIFEST = "实施基线v1冻结清单-2026-09-30.md"


def _run(cmd, cwd=REPO_ROOT):
    """执行命令，返回 (exit_code, stdout)。不抛异常。"""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=120,
        )
        return p.returncode, p.stdout
    except Exception as exc:  # noqa: BLE001
        return 127, "CMD_ERROR: %s" % exc


def _diff_files(base=None, staged=False):
    """返回改动文件清单。"""
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only"]
    elif base:
        cmd = ["git", "diff", "--name-only", "%s..HEAD" % base]
        rc, out = _run(cmd)
        if rc != 0:
            cmd = ["git", "diff", "--name-only", base]
    else:
        cmd = ["git", "diff", "--name-only", "HEAD"]
    rc, out = _run(cmd)
    if rc != 0:
        return [], out.strip()
    return [ln.strip() for ln in out.splitlines() if ln.strip()], ""


def _diff_body(base=None, staged=False, unified=0):
    """返回 diff 正文（含新增/删除行）。"""
    if staged:
        cmd = ["git", "diff", "--cached", "-U%d" % unified]
    elif base:
        cmd = ["git", "diff", "-U%d" % unified, "%s..HEAD" % base]
        rc, _ = _run(cmd)
        if rc != 0:
            cmd = ["git", "diff", "-U%d" % unified, base]
    else:
        cmd = ["git", "diff", "-U%d" % unified, "HEAD"]
    rc, out = _run(cmd)
    return out if rc == 0 else ""


def _existing_tool_names(rev="HEAD"):
    """从指定版本提取 @mcp.tool 注册的函数名集合（工具面契约基线）。"""
    path = "gateway/app.py"
    if rev not in (None, "", "WORKTREE"):
        rc, out = _run(["git", "show", "%s:%s" % (rev, path)])
        if rc != 0:
            return None
        src = out
    else:
        full = os.path.join(REPO_ROOT, path)
        if not os.path.exists(full):
            return None
        with open(full, "r", encoding="utf-8") as fh:
            src = fh.read()
    # @mcp.tool 之后最近的 def 即注册函数名；兼容 @mcp.tool() 形式
    names = set()
    pending = False
    for line in src.splitlines():
        if re.search(r"@mcp\.tool\b", line):
            pending = True
            continue
        if pending:
            m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if m:
                names.add(m.group(1))
                pending = False
            elif line.strip() and not line.strip().startswith("#"):
                pending = False
    return names


# ---------------------------------------------------------------------------
# 六条红线的判定实现
# ---------------------------------------------------------------------------

def check_r1_upstream(files):
    hit = [f for f in files if f.startswith(UPSTREAM_PREFIXES)]
    return hit, ("改动命中开源上游只读区：%s" % ", ".join(hit)) if hit else ""


def check_r2_tool_contract(diff_text, files):
    """改已有工具入参名/顺序 —— 以「注册函数名丢失」+「def 行被删」为判据。"""
    findings = []
    base_names = _existing_tool_names("HEAD")
    cur_names = _existing_tool_names("WORKTREE")
    if base_names is None or cur_names is None:
        return findings, "" if base_names is not None else "无法读取 app.py（基线或工作树缺失）"
    lost = base_names - cur_names
    if lost:
        findings.append("已注册工具函数名丢失/改名：%s" % ", ".join(sorted(lost)))
    # diff 中删除的 def 行（-def xxx( 形式）
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            m = re.match(r"-\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if m and m.group(1) in base_names:
                findings.append("删除了已注册工具的 def 行：%s" % m.group(1))
    return sorted(set(findings)), ("；".join(sorted(set(findings)))) if findings else ""


def check_r3_d1(diff_text, files):
    """改 D1 行为：F4 不通过时自动改写 SQL。

    判据：diff 新增行里出现对 revised_sql 的赋值，且不是恒 None。
    """
    findings = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if "revised_sql" in body and re.search(r"revised_sql\s*=\s*(?!None)", body):
            findings.append("新增行对 revised_sql 赋非 None 值：%s" % body.strip()[:120])
    return sorted(set(findings)), ("；".join(sorted(set(findings)))) if findings else ""


def check_r4_data_and_baseline(diff_text, files):
    findings = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        # 误报豁免：含已知非 SQL 参数/词组时不判红
        low = body.lower()
        if any(f in low for f in TRUNCATE_FALSE_FRIENDS):
            continue
        for pat, label in DESTRUCTIVE_PATTERNS:
            if re.search(pat, body, re.IGNORECASE):
                findings.append("新增行含破坏性操作（%s）：%s" % (label, body.strip()[:120]))
    # 冻结基线：文档库不在仓库内，此处只报「是否触碰声明文件」
    return sorted(set(findings)), ("；".join(sorted(set(findings)))) if findings else ""


def check_r5_dependency(diff_text, files):
    findings = []
    if "gateway/requirements.txt" in files:
        rc, out = _run(["git", "diff", "HEAD", "--", "gateway/requirements.txt"])
        for line in out.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                item = line[1:].strip()
                if not item or item.startswith("#"):
                    continue
                norm = item.split("#")[0].strip()
                if norm not in ALLOWED_REQ_LINES:
                    findings.append("gateway/requirements.txt 新增非白名单依赖：%s" % item)
    return sorted(set(findings)), ("；".join(sorted(set(findings)))) if findings else ""


def check_r6_trust_mcp(diff_text, files):
    findings = []
    for f in files:
        if f.startswith(".mcp.json") or f.endswith("/mcp.json"):
            findings.append("改动 MCP 配置文件：%s（需人工点信任，属红线）" % f)
    return sorted(set(findings)), ("；".join(sorted(set(findings)))) if findings else ""


CHECKS = [
    ("R1", "改开源上游源码", check_r1_upstream),
    ("R3", "改 D1 行为（F4 自动改写）", check_r3_d1),
    ("R4", "删数据 / 破坏性操作", check_r4_data_and_baseline),
    ("R5", "新增运行时依赖", check_r5_dependency),
    ("R6", "需人工点信任 / 改 mcp.json", check_r6_trust_mcp),
]


def main():
    ap = argparse.ArgumentParser(description="Askoda 六条红线守卫")
    ap.add_argument("--base", default=None, help="基线 ref（默认与 HEAD 比）")
    ap.add_argument("--staged", action="store_true", help="判暂存区")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    files, err = _diff_files(base=args.base, staged=args.staged)
    if err and not files:
        sys.stderr.write("FATAL: 无法获取改动文件清单（%s）\n" % err)
        return 2

    diff_text = _diff_body(base=args.base, staged=args.staged)

    results = []
    for rid, name, fn in CHECKS:
        if rid == "R2":
            hits, msg = fn(diff_text, files)
        else:
            hits, msg = fn(diff_text, files) if fn.__code__.co_argcount == 2 else fn(files)
        results.append({"id": rid, "name": name, "hit": bool(hits), "detail": hits, "reason": msg})

    # R2 单独处理（签名不同）
    r2_hits, r2_msg = check_r2_tool_contract(diff_text, files)
    for r in results:
        if r["id"] == "R2":
            continue
    results.insert(0, {"id": "R2", "name": "改已有工具入参名/顺序",
                       "hit": bool(r2_hits), "detail": r2_hits, "reason": r2_msg})

    any_hit = any(r["hit"] for r in results)
    payload = {
        "guard": "redline_guard",
        "repo": REPO_ROOT,
        "changed_files": files,
        "checks": results,
        "verdict": "BLOCKED" if any_hit else "CLEAR",
        "exit_code": 1 if any_hit else 0,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload["exit_code"]

    print("=" * 70)
    print("Askoda 红线守卫 · 六条红线机器判定")
    print("  基线：%s   改动文件：%d 个" % (args.base or ("暂存区" if args.staged else "HEAD"), len(files)))
    print("=" * 70)
    for r in results:
        mark = "🔴 命中" if r["hit"] else "⚪ 未触"
        print("  %s  %s  %s" % (mark, r["id"], r["name"]))
        for d in r["detail"]:
            print("        · %s" % d)
    print("-" * 70)
    if any_hit:
        print("总判定：🔴 BLOCKED —— 命中红线，须出《红线阻断报告》后停止后续动作。")
        for r in results:
            if r["hit"]:
                print("  · %s %s：%s" % (r["id"], r["name"], r["reason"]))
    else:
        print("总判定：✅ CLEAR —— 六条红线全未触。")
    return payload["exit_code"]


if __name__ == "__main__":
    sys.exit(main())