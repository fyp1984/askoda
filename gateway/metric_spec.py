# -*- coding: utf-8 -*-
"""口径规格：校验与装配（纯函数，不碰库、不碰网络）。

为什么单独一个模块
------------------
口径是**数据**，写错了不会有任何报错：视图名拼错 → 编译出来的 SQL 指向不存在的对象，
要等用户点「生成」那一刻才炸；维度写成视图里没有的列 → 同理。这类错必须在**保存时就拦住**。

同一份判据被三处使用：
  · 网关保存接口（`metric_store.save`）—— 界面点保存时拦；
  · 门禁 G2（`tools/metric_registry_check.py`）—— 提交前拦；
  · 命令行工具（`tools/credit_metrics.py --check`）—— 重建库时拦。
**三处必须同一份实现**，否则「界面能存、门禁判红」这种事一定会发生。

从 `tools/credit_metrics.py` 搬过来的原因
----------------------------------------
原来是工具脚本里的函数，网关要用就得反过来 import 工具目录，
而镜像里根本没有 `tools/`。搬进 `gateway/` 之后方向就对了：工具 import 网关。
"""
from __future__ import annotations

import copy

# 视图语句里允许出现的关键字（除此之外的标识符都当作表名去核对）
SQL_KEYWORDS = {
    "select", "from", "join", "left", "right", "inner", "outer", "full", "on",
    "where", "group", "by", "order", "as", "and", "or", "case", "when", "then",
    "else", "end", "coalesce", "nullif", "sum", "count", "distinct", "min", "max",
    "avg", "date_trunc", "desc", "asc", "limit", "with", "union", "all", "having",
    "in", "not", "is", "null", "cast", "timestamp", "date", "month", "year", "day",
    "interval",
}

TIME_MODES = ("snapshot", "flow")


def tables_in_statement(stmt: str) -> set:
    """从视图语句里抽出被引用的表名（够用即可，不做完整 SQL 解析）。"""
    import re

    out = set()
    for mt in re.finditer(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.]*)", stmt or "", re.I):
        name = mt.group(1)
        if name.lower() not in SQL_KEYWORDS:
            out.add(name)
    return out


def static_check(metrics: dict, mdl: dict) -> list:
    """口径 ↔ MDL ↔ 视图 三方一致性。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    models = {m.get("name") for m in (mdl.get("models") or [])}
    views = metrics.get("views") or []
    vnames = [v.get("name") for v in views]
    if len(vnames) != len(set(vnames)):
        problems.append("视图名重复：%s" % vnames)
    vmap = {v.get("name"): v for v in views}

    for v in views:
        name = v.get("name")
        if not name:
            problems.append("存在没有 name 的视图")
            continue
        if not v.get("statement"):
            problems.append("视图 %s 缺 statement" % name)
            continue
        for t in tables_in_statement(v["statement"]):
            # 允许写库内全名（public.acct_loan）；MDL 里登记的是模型名
            base = t.split(".")[-1]
            if t not in models and base not in models:
                problems.append("视图 %s 引用了 MDL 里没有的对象：%s" % (name, t))
        if not v.get("columns"):
            problems.append("视图 %s 缺 columns（规划器与门禁都靠它认列）" % name)

    keys = set()
    for m in metrics.get("metrics") or []:
        key = m.get("key")
        if not key:
            problems.append("存在没有 key 的口径")
            continue
        if key in keys:
            problems.append("口径 key 重复：%s" % key)
        keys.add(key)
        vn = m.get("view")
        if vn not in vmap:
            problems.append("口径 %s 指向未登记的视图：%s" % (key, vn))
            continue
        cols = set(vmap[vn].get("columns") or [])
        for d in m.get("dims") or []:
            if d not in cols:
                problems.append("口径 %s 的维度 %s 不在视图 %s 的列里" % (key, d, vn))
        if not m.get("expr"):
            problems.append("口径 %s 缺 expr" % key)
        if not m.get("aliases"):
            problems.append("口径 %s 缺 aliases（业务说法没有别名就永远命中不了）" % key)
        if m.get("time_mode") not in TIME_MODES:
            problems.append("口径 %s 的 time_mode 必须是 snapshot 或 flow" % key)
        if m.get("time_col") not in cols:
            problems.append("口径 %s 的 time_col %s 不在视图列里" % (key, m.get("time_col")))
    return problems


def build_mdl(mdl: dict, metrics: dict) -> dict:
    """把口径写进 MDL：views 给引擎执行，x_askoda_metrics 给规划器读。"""
    out = copy.deepcopy(mdl)
    out["views"] = [
        {"name": v["name"], "statement": v["statement"]} for v in metrics.get("views") or []
    ]
    out["x_askoda_metrics"] = {
        "dataset": metrics.get("dataset"),
        "version": metrics.get("version"),
        "views": {
            v["name"]: {"columns": v.get("columns") or [], "comment": v.get("comment") or ""}
            for v in metrics.get("views") or []
        },
        "metrics": metrics.get("metrics") or [],
    }
    return out


def section_of(mdl: dict) -> dict:
    """从 MDL 里取回口径段（界面展示与「保存前」的基线）。

    视图语句从 `mdl["views"]` 取：`x_askoda_metrics` 里只存列与注释，
    语句在引擎那一侧。界面上要能看见「这条口径基于哪段 SQL」，
    也得能原样带回去——否则保存一次就会把视图语句清空。
    """
    sec = (mdl or {}).get("x_askoda_metrics") or {}
    stmts = {v.get("name"): v.get("statement") or "" for v in ((mdl or {}).get("views") or [])}
    return {
        "dataset": sec.get("dataset"),
        "version": sec.get("version"),
        "views": [
            {"name": name, "columns": (meta or {}).get("columns") or [],
             "comment": (meta or {}).get("comment") or "",
             "statement": stmts.get(name) or ""}
            for name, meta in (sec.get("views") or {}).items()
        ],
        "metrics": sec.get("metrics") or [],
    }


def _s(v, limit=200) -> str:
    return (v or "").strip()[:limit] if isinstance(v, str) else ("" if v is None else str(v).strip()[:limit])


def _list(v) -> list:
    if isinstance(v, str):
        v = [x.strip() for x in v.replace("，", ",").replace("、", ",").split(",")]
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        s = _s(x, 60)
        if s and s not in out:
            out.append(s)
    return out


def normalize(payload: dict, dataset: str, base: dict | None = None) -> dict:
    """把界面传来的草稿规整成合法的口径段。

    只做**结构**规整（去空白、去重、补默认、剔掉不认识的键），不做业务判断；
    业务判断统一交给 `static_check`，这样界面报错与门禁报错永远一致。
    """
    payload = payload or {}
    base = base or {}
    metrics = []
    seen = set()
    for raw in payload.get("metrics") or []:
        if not isinstance(raw, dict):
            continue
        key = _s(raw.get("key"), 64)
        if not key or key in seen:
            continue
        seen.add(key)
        metrics.append({
            "key": key,
            "label": _s(raw.get("label"), 60) or key,
            "aliases": _list(raw.get("aliases")),
            "view": _s(raw.get("view"), 64),
            "expr": _s(raw.get("expr"), 400),
            "unit": _s(raw.get("unit"), 20),
            "fmt": _s(raw.get("fmt"), 20) or "num",
            "time_mode": _s(raw.get("time_mode"), 20) or "snapshot",
            "time_col": _s(raw.get("time_col"), 64),
            "dims": _list(raw.get("dims")),
            "definition": _s(raw.get("definition"), 400),
            "caveats": _s(raw.get("caveats"), 400),
            "source": _s(raw.get("source"), 200),
        })

    views = []
    for raw in payload.get("views") or []:
        if not isinstance(raw, dict):
            continue
        name = _s(raw.get("name"), 64)
        if not name:
            continue
        views.append({
            "name": name,
            "statement": (raw.get("statement") or "").strip()[:6000],
            "columns": _list(raw.get("columns")),
            "comment": _s(raw.get("comment"), 300),
        })

    return {
        "dataset": dataset or base.get("dataset") or "",
        "version": _s(payload.get("version"), 40) or base.get("version") or "",
        "views": views,
        "metrics": metrics,
    }


def seed_orphans(seed: dict, section: dict) -> list:
    """种子里有、库里没有的口径。

    种子文件（`db/credit/04_metrics.json`）现在是**初始种子**，不再是真源；
    但「文件里写了、库里没有」说明有人改了文件却没导入 —— 这条必须报出来，
    否则重建库时口径会静默少一半。
    """
    have = {m.get("key") for m in (section.get("metrics") or [])}
    out = []
    for m in (seed.get("metrics") or []):
        k = m.get("key")
        if k and k not in have:
            out.append(k)
    return out
