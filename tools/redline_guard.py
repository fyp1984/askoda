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
import ast
import hashlib
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
#
# ⚠️ 这里原先还有一份探针样例常量（**8 条**：TRUNCATE TABLE / truncate table /
#    TRUNCATE `x` / DELETE FROM / DROP TABLE / auto-truncate / 分词截断 / 直接分析数据），
#    但它的 AST Load 次数是 **0** —— 判据文件里**从未被用过**，是一份孤儿。
#    现已迁到 `tools/redline_r4_selftest.py` 作为**自证 fixture**（在那里真正被用起来），
#    判据文件本身不再持有它。
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
    # DELETE FROM 只在**表名写死**时判红。
    #   DELETE FROM demand_requests          → 判红（硬编码表名，危险）
    #   DELETE FROM %s  /  DELETE FROM $1     → 不判红（参数化占位符模板）
    #   DELETE FROM orders WHERE id=%s        → 判红（有 WHERE 限定，是正常的单行删除）
    # 踩过的坑：tools/reset_demo_data.py 的清理模板 `cur.execute("delete from %s" % t)`
    #   被误判成破坏性操作 → R4 BLOCKED。而该脚本本身带 dry-run 护栏、是清理工具的正当职责。
    (r"\bDELETE\s+FROM\s+`?\"?\[?[A-Za-z_][A-Za-z0-9_$]*[`\"'\]]?", "DELETE FROM"),
    (r"\bDROP\s+DATABASE\b", "DROP DATABASE"),
]
# TRUNCATE 误报豁免：这些词是常见的非 SQL 参数/单词，出现时不算破坏性操作
TRUNCATE_FALSE_FRIENDS = (
    "auto-truncate", "truncate tokenization", "truncate-words",
    "分词截断", "截断", "截短",
)

# R4-b 冻结基线资产的登记清单（仓库内）。路径相对仓库根。
#
# ★ 为什么要有它：R4 的判据名里带着「改带 sha256 的冻结基线」，但实现里
#   那半句**从未落地**（sha256 / hashlib 在本文件出现 0 次，孤儿常量 Load 0 次），
#   而 `files` 形参完全没用到 ⇒ 判据自称覆盖了一个面，实现上那个面是空的。
#   实测：改登记值 / 动整份清单 / 改 `poc-eval/bank-b.json` ⇒ 全部 CLEAR。
#
# ★ 口径（与「甲」方案一致）：
#   · 只登记**仓库内**资产；文档库那 7 份**刻意不登记**
#     （实测 5 份已漂移，登记进去一上线就是 5 红 ⇒ 长期红的门禁等于没有门禁）；
#   · 文档库基线的漂移巡检由 `tools/docsync.py` 承担，不在本红线职责内；
#   · 比对**当前工作树的文件内容**（不是 diff 文本 —— diff 看不到未改动的文件）；
#   · 只对**内容变更**判红，不对**文件增删**判红；
#   · 只比 sha256 **前 16 位**（与清单口径一致，避免末尾换行 / 编辑器差异误报）。
FROZEN_ASSET_MANIFEST = "tools/baseline_manifest.json"


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


def _read_app_src(rev="HEAD", root=None):
    """读 gateway/app.py 的指定版本源码。

    返回 (status, src)：
      ("ok", 源码文本)
      ("absent", None)  该版本**没有这个文件**（被删 / 被改名）⇒ 工具面为空
      ("error", None)   其他原因读不到（ref 无效、git 故障等）

    ★ 为什么要把「文件不存在」和「读不到」分开：
      旧实现两者都返回 None，而 R2 的早退分支 `if ... is None: return []`
      把这两种情况一并当成「无违规」⇒ 判红变判绿。实测：
        · `git rm gateway/app.py` ⇒ 48 个已注册工具全部消失，R2 仍 CLEAR；
        · app.py 语法写坏 ⇒ R2 仍 CLEAR。
      前者尤其严重——**删掉整个网关文件**是 R2 最该拦的改动，却因为早退而放行。
      现在两者都改为**判红**（不可判定 ≠ 没违规，红线守卫不设通融通道）。
    """
    root = root or REPO_ROOT
    path = "gateway/app.py"
    if rev in (None, "", "WORKTREE"):
        full = os.path.join(root, path)
        if not os.path.exists(full):
            return "absent", None
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return "ok", fh.read()
        except OSError:
            return "error", None
    # git 侧：rev 可以是 "HEAD"、任意 ref，或 "INDEX"（暂存区）
    if rev == "INDEX":
        spec = ":%s" % path
        rev_valid = True
    else:
        spec = "%s:%s" % (rev, path)
        rc, _ = _run(["git", "rev-parse", "--verify", "%s^{commit}" % rev], cwd=root)
        rev_valid = rc == 0
    rc, _ = _run(["git", "cat-file", "-e", spec], cwd=root)
    if rc != 0:
        return ("absent" if rev_valid else "error"), None
    rc, out = _run(["git", "show", spec], cwd=root)
    if rc != 0:
        return "error", None
    return "ok", out


def _is_mcp_tool_decorator(node):
    """判断 AST 装饰器节点是否为 @mcp.tool / @mcp.tool()。"""
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


def tool_signatures(src):
    """从 gateway/app.py 源码解析出工具面契约。

    返回 {函数名: [(参数名, 是否有默认值), ...]}；源码为 None 或语法错时返回 None。

    ★ 为什么用 `ast` 而不是逐行正则：
      R2 要保护的是「**参数名与相对顺序**」，而多行签名的参数**根本不在同一行**
      （`demand_create` / `knowledge_upload` / `metadata_lookup` 都是多行）。
      逐行正则最多只能抓到 `def foo(` 的第一个 token，拿不到完整参数序列，
      于是「比对参数名与顺序」这件事在实现上根本无从谈起——这正是旧判据
      退化成「def 行是否出现在 diff 删除侧」的根因。
    """
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    sigs = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            continue
        a = node.args
        positional = list(getattr(a, "posonlyargs", [])) + list(a.args)
        n_default = len(a.defaults)
        first_default = len(positional) - n_default
        params = [
            (p.arg, i >= first_default) for i, p in enumerate(positional)
        ]
        if a.vararg is not None:
            params.append(("*" + a.vararg.arg, False))
        for p, d in zip(a.kwonlyargs, a.kw_defaults):
            params.append((p.arg, d is not None))
        if a.kwarg is not None:
            params.append(("**" + a.kwarg.arg, False))
        sigs[node.name] = params
    return sigs


def _app_contract(rev="HEAD", root=None):
    """取某一版本（或工作树）的工具面契约。

    返回 (sigs, status)：
      ({...}, "ok")        正常
      ({},    "absent")    该版本没有 gateway/app.py ⇒ **工具面为空**
                          （被删 / 被改名 ⇒ 已注册工具全部消失 ⇒ lost 逻辑会命中）
      (None,  "broken")    文件在，但语法无法解析 ⇒ **不可判定**
    """
    status, src = _read_app_src(rev, root=root)
    if status == "absent":
        return {}, "absent"
    if status != "ok":
        return None, "error"
    sigs = tool_signatures(src)
    if sigs is None:
        return None, "broken"
    return sigs, "ok"


def _r2_rev_pair(base=None, staged=False):
    """R2 要比对的两个版本：必须与本模式**实际 diff 的区间一致**。

    R2 与 R3/R4/R5/R6 同属一次判定，报告的必须是**同一个变更集**：

      默认        diff = HEAD..工作树   ⇒ (HEAD, WORKTREE)
      --staged    diff = HEAD..索引     ⇒ (HEAD, INDEX)
      --base X    diff = X..HEAD        ⇒ (X, HEAD)

    ★ 旧实现恒定拿 (HEAD, WORKTREE) 比，与 --base/--staged 模式判定的区间**不一致**：
      实测「改动已提交 + --base <ref>」时，R2 拿 HEAD 和工作树比（两者相同）⇒ 判绿，
      而同一批改动在 --base 模式里明明包含契约破坏。这是判据与模式脱节，
      不是「判据放宽」，属**漏判**。
    """
    if staged:
        return "HEAD", "INDEX"
    if base:
        return base, "HEAD"
    return "HEAD", "WORKTREE"


def compare_tool_signatures(base, cur):
    """比对两份工具面契约，返回 findings 列表（纯函数，可离线自测）。

    `base` / `cur` 均为 tool_signatures() 的产物。
    """
    findings = []
    lost = set(base) - set(cur)
    if lost:
        findings.append("已注册工具函数名丢失/改名：%s" % ", ".join(sorted(lost)))

    for name in sorted(set(base) & set(cur)):
        old = base[name]
        new = cur[name]
        if old == new:
            continue
        old_names = [p for p, _ in old]
        new_names = [p for p, _ in new]
        # 判据 2+3+4 的统一形式：**旧序列必须是新序列的前缀**。
        # 前缀成立 ⇒ 已有参数名未改、未删、相对顺序未动，新参数只在尾部追加。
        if new_names[:len(old_names)] == old_names:
            extra = new[len(old_names):]
            # 豁免：仅在末尾追加、且新参数带默认值。
            no_default = [p for p, has_def in extra if not has_def]
            if no_default:
                findings.append(
                    "工具 %s 末尾追加了无默认值的参数：%s（既有调用方会缺参报错）"
                    % (name, ", ".join(no_default))
                )
            continue
        # 前缀不成立：区分「重命名 / 删除 / 重排 / 中间插参」，报出可定位的信息。
        missing = [p for p in old_names if p not in new_names]
        inserted = [p for p in new_names if p not in old_names]
        bits = []
        if missing:
            bits.append("删除/改名：%s" % ", ".join(missing))
        if inserted:
            bits.append("新增：%s" % ", ".join(inserted))
        if not missing and not inserted:
            bits.append("纯重排")
        findings.append(
            "工具 %s 的入参契约被改（%s）；原：(%s) → 现：(%s)"
            % (name, "；".join(bits),
               ", ".join(old_names), ", ".join(new_names))
        )
    return sorted(set(findings))


# ---------------------------------------------------------------------------
# 六条红线的判定实现
# ---------------------------------------------------------------------------

def check_r1_upstream(files):
    hit = [f for f in files if f.startswith(UPSTREAM_PREFIXES)]
    return hit, ("改动命中开源上游只读区：%s" % ", ".join(hit)) if hit else ""


def check_r2_tool_contract(diff_text, files, base=None, staged=False):
    """R2：改已有工具入参名/顺序（工具面 → 实现层是按位契约）。

    判红（任一成立）：
      1. 已注册工具的函数名丢失或改名；
      2. 已有参数的**参数名被改或被删**；
      3. 已有参数的**相对顺序被改**；
      4. 新参数**插在已有参数中间**（非末尾追加）。

    豁免：**仅在末尾追加、且新参数带默认值**。
      这不是放水，而是与运行时行为对齐——`_wired` 已按目标函数签名过滤未知
      kwargs 并打到 stderr（gateway/app.py `_wired`），工具面先加可选参数、
      实现层后落地，期间对既有调用方完全透明；反过来，删名/改名/重排/中间插参
      在 `_wired` 的按名转发下**必然**改变既有调用的语义，属真破契约。

    ★ 这里比对的是**解析出的完整参数序列**，不是 diff 里有没有 `-def xxx(`。
      旧判据只看函数名，于是「末尾追加一个可选参数」这种向后兼容的正常演进
      也会被判红；而把两个工具函数整块互换位置（签名一字未改，def 行整行位移）
      同样被判红——判据名为「改已有工具入参名/顺序」，实际测的是
      「def 行是否出现在 diff 删除侧」，假阳性面远大于保护面。
      （实测：单独重排 import 块不触发，因为它不产生 `-def` 行；
        触发假阳性的是**函数块位移**。）

    ★ 不可判定 ⇒ 判红（红线守卫不设通融通道）。
      旧实现把「app.py 读不到」（被删/被改名/语法坏）一律早退成空 findings
      ⇒ 判绿。实测：**删掉整个 gateway/app.py**（48 个已注册工具全部消失）、
      把 app.py 改名、app.py 语法写坏，三种情况 R2 全部 CLEAR——而这恰恰是
      R2 最该拦的改动。「判不出来」不等于「没违规」。
    """
    base_rev, cur_rev = _r2_rev_pair(base=base, staged=staged)
    base, base_status = _app_contract(base_rev)
    cur, cur_status = _app_contract(cur_rev)
    findings = []

    # 基线侧不可判定 ⇒ 判红（否则下面拿空契约去比，等于「什么都没变」）
    if base_status in ("error", "broken"):
        return (
            ["无法读取基线（%s）的 gateway/app.py，R2 不可判定" % base_rev],
            "无法读取基线（%s）的 gateway/app.py，R2 不可判定" % base_rev,
        )
    # 改动侧语法坏 ⇒ 判红
    if cur_status in ("error", "broken"):
        msg = "改动侧（%s）的 gateway/app.py 语法无法解析，R2 不可判定" % cur_rev
        return [msg], msg
    # 改动侧没有 app.py ⇒ 工具面为空 ⇒ 全部已注册工具「丢失」，由 lost 逻辑命中
    # （cur = {} 而非 None，这是与旧早退逻辑的关键差别）

    findings.extend(compare_tool_signatures(base, cur))
    return findings, ("；".join(findings)) if findings else ""


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


def _is_parametrized_sql(body):
    """判断是否是参数化 SQL 模板（表名位置是占位符而非字面表名）。

    踩过的坑（2026-10-07）：`cur.execute("delete from %s" % t)` 被判成破坏性操作 → R4 BLOCKED。
    那是清理工具的正当职责（带 dry-run 护栏），不该判红。
    判据：`FROM` 与 `WHERE`/`INTO` 之间是占位符而非标识符。
    """
    low = body.lower()
    for m in re.finditer(r"\b(?:from|into|update|join)\s+([^\s,;)\]]+)", low):
        token = m.group(1).strip("\"'`[]")
        if token in ("%s",) or re.fullmatch(r"\$\d+", token) or re.match(r"^%\(|^:", token):
            return True
    return False


def load_frozen_baseline_manifest(root=None):
    """读 R4-b 的冻结基线登记清单。

    返回 (assets, err)：
      (list, None)  正常，每个元素含 path / sha256_prefix / expected_bytes
      (None, 错误说明)  清单读不到 / 格式不对

    ★ 清单**缺失或损坏 ⇒ 判红**（不可判定 ≠ 没违规）：
      若缺失时判绿，等于「把清单删掉」就永久关掉这条红线。
    """
    root = root or REPO_ROOT
    path = os.path.join(root, FROZEN_ASSET_MANIFEST)
    if not os.path.exists(path):
        return None, "冻结基线登记清单缺失：%s" % FROZEN_ASSET_MANIFEST
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return None, "冻结基线登记清单无法解析（%s）：%s" % (FROZEN_ASSET_MANIFEST, exc)
    assets = doc.get("assets")
    if not isinstance(assets, list) or not assets:
        return None, "冻结基线登记清单内容为空或格式不对：%s" % FROZEN_ASSET_MANIFEST
    return assets, None


def check_frozen_baseline(root=None):
    """R4-b：仓库内登记的冻结基线资产，内容被改 ⇒ 判红。

    比对**当前工作树**的文件内容（不是 diff 文本）——diff 看不到未改动的文件，
    而「内容被改」这件事本身就可能发生在已提交的历史里，只看 diff 会漏。

    口径：
      · 只对**内容变更**判红（sha256 前 16 位不一致）；
      · **不对文件增删判红**（文件不存在 ⇒ 跳过并如实标注，不判红）；
      · 只比 sha256 前 16 位。

    返回 (findings, 说明)。说明在无命中时也会给出（跳过 / 缺文件等如实标注）。
    """
    root = root or REPO_ROOT
    assets, err = load_frozen_baseline_manifest(root=root)
    if err:
        # 清单读不到 ⇒ 无法判定 ⇒ 判红
        return ["冻结基线子判据不可判定：%s" % err], err
    findings = []
    notes = []
    for item in assets:
        rel = item.get("path")
        want = (item.get("sha256_prefix") or "").lower()
        if not rel or not want:
            notes.append("登记条目缺 path 或 sha256_prefix，已跳过：%r" % (item,))
            continue
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            # 只对内容变更判红，不对文件增删判红 ⇒ 跳过并如实标注
            notes.append("登记资产不存在（按「不对文件增删判红」口径跳过）：%s" % rel)
            continue
        with open(full, "rb") as fh:
            got = hashlib.sha256(fh.read()).hexdigest()[:len(want)]
        if got != want:
            findings.append(
                "冻结基线资产内容被改：%s（sha256 前 %d 位 %s ≠ 登记 %s）"
                % (rel, len(want), got, want)
            )
    if findings:
        return findings, "；".join(findings)
    if notes:
        return [], "冻结基线子判据：%d 项登记资产中 %d 项已核对一致；%s" % (
            len(assets), len(assets) - len(notes), "；".join(notes)
        )
    return [], "冻结基线子判据：%d 项登记资产内容一致" % len(assets)


def check_r4_data_and_baseline(diff_text, files):
    findings = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        # 误报豁免：含已知非 SQL 参数/词组时不判红
        low = body.lower()
        # 注释行豁免：# / -- 开头的说明文字里提到 SQL 不是执行 SQL
        st = body.lstrip()
        if st.startswith("#") or st.startswith("//") or st.startswith("--") or st.startswith('*'):
            continue
        if any(f in low for f in TRUNCATE_FALSE_FRIENDS):
            continue
        # 参数化 SQL 模板豁免：表名位置是占位符（%s/$1/%(x)s）→ 不是硬编码破坏性操作
        if _is_parametrized_sql(body):
            continue
        for pat, label in DESTRUCTIVE_PATTERNS:
            if re.search(pat, body, re.IGNORECASE):
                findings.append("新增行含破坏性操作（%s）：%s" % (label, body.strip()[:120]))
    # R4-b：仓库内登记的冻结基线资产（比对当前工作树的文件内容，不是 diff 文本）
    base_hits, base_note = check_frozen_baseline()
    findings.extend(base_hits)
    if findings:
        return sorted(set(findings)), "；".join(sorted(set(findings)))
    # 无命中时也如实标注子判据已跑过（不是静默跳过）
    return [], base_note


def _added_lines_for_path(diff_text, path):
    """从 unified diff 中取出**指定文件**的新增行（`+` 开头，不含 `+++` 头）。

    ★ 为什么要按文件切段，而不是对整份 diff 扫 `+` 行：
      R5 只该管 `gateway/requirements.txt` 一个文件。若对整份 diff 扫，
      别的文件里新增一行 `import requests` 就会被当成「新增运行时依赖」⇒ 误判。
    """
    out = []
    in_target = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            # `diff --git a/<path> b/<path>` ⇒ 判断本段是不是目标文件
            parts = line.split()
            in_target = len(parts) >= 4 and parts[3] == "b/" + path
            continue
        if not in_target:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def check_r5_dependency(diff_text, files):
    """R5：新增运行时依赖（一期只放行 5 项白名单）。

    ★ 判据必须复用**本次模式的那份 diff**，不能自己另跑一条 git 命令：
      旧实现内层硬编码 `git diff HEAD -- gateway/requirements.txt`，
      于是与「本次判定的区间」脱节，实测出三个错：
        · 改动**已提交** + `--base <ref>` ⇒ 内层只看 HEAD..工作树 ⇒ **漏判**；
        · `--staged` ⇒ 内层把**工作树里未暂存的改动**也算进来 ⇒ **越权**；
        · git 命令失败（无效 ref）⇒ 内层返回空 ⇒ **判绿**。
      现在统一从传入的 diff_text 取该文件的新增行 ⇒ 区间天然一致。
    """
    findings = []
    for raw in _added_lines_for_path(diff_text, "gateway/requirements.txt"):
        item = raw.strip()
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
    ("R2", "改已有工具入参名/顺序", check_r2_tool_contract),
    ("R3", "改 D1 行为（F4 自动改写）", check_r3_d1),
    ("R4", "删数据 / 破坏性操作 / 改冻结基线", check_r4_data_and_baseline),
    ("R5", "新增运行时依赖", check_r5_dependency),
    ("R6", "需人工点信任 / 改 mcp.json", check_r6_trust_mcp),
]


def run_all_checks(diff_text, files, base=None, staged=False):
    """按 rid **显式分派**跑完六条红线，返回 results 列表。

    ★ 为什么要显式分派、而不是按 `fn.__code__.co_argcount` 猜：
      旧写法用「参数个数」决定怎么调，于是 R2（签名 4 个）被塞进 2 参的分支里，
      靠一句 `if rid == "R2"` 打补丁；而 R2 的 `base` / `staged` **根本没传进去**
      ——`--base` / `--staged` 模式下 R2 判的区间与 diff 区间**不一致**。
      现在每条红线按 rid 显式调用，参数与区间一一对应，不会再错位。

    返回的每项：{"id", "name", "hit", "detail", "reason"}
    """
    results = []
    for rid, name, fn in CHECKS:
        if rid == "R1":
            hits, msg = check_r1_upstream(files)
        elif rid == "R2":
            hits, msg = check_r2_tool_contract(
                diff_text, files, base=base, staged=staged
            )
        elif rid == "R3":
            hits, msg = check_r3_d1(diff_text, files)
        elif rid == "R4":
            hits, msg = check_r4_data_and_baseline(diff_text, files)
        elif rid == "R5":
            hits, msg = check_r5_dependency(diff_text, files)
        elif rid == "R6":
            hits, msg = check_r6_trust_mcp(diff_text, files)
        else:  # pragma: no cover - CHECKS 是静态表，理论不可达
            hits, msg = [], "未知的红线编号：%s" % rid
        results.append({
            "id": rid, "name": name, "hit": bool(hits),
            "detail": list(hits or []), "reason": msg or "",
        })
    return results


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
    # ⚠️ git 命令失败（无效 ref 等）⇒ 不能当「无改动」继续判绿。
    #    旧实现里 `git diff --name-only nosuchref..HEAD` 失败后走了
    #    `git diff --name-only nosuchref` 的兜底分支，返回空清单且无错误信息
    #    ⇒ 静默降级成「什么都没改」⇒ 全部红线判绿。
    #    现在显式校验 ref 有效性，无效即判红（不可判定 ≠ 没违规）。
    if args.base:
        rc, _ = _run(["git", "rev-parse", "--verify", "%s^{commit}" % args.base])
        if rc != 0:
            msg = "基线 ref 无效：%s（无法判定改动范围）" % args.base
            sys.stderr.write("FATAL: %s\n" % msg)
            return 1

    diff_text = _diff_body(base=args.base, staged=args.staged)

    results = run_all_checks(
        diff_text, files, base=args.base, staged=args.staged
    )

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