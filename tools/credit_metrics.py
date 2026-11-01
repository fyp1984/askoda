# -*- coding: utf-8 -*-
"""信贷口径注册：把「口径」写进 MDL，一处登记、三处共用

背景
----
C 库刚建好时，`sql_generate` 对信贷需求一律拒绝——不是缺表，是缺**意图库**：
每个库都要在 `gateway/planner.py` 手写一套「业务说法 → SQL 模板」。新接一个库就要
重写一遍通用句式，代码越写越长，而口径定义（业务侧的说法）与代码里的写法各写一份、
彼此不知道对方改过——「品类销售额漏 JOIN 订单表」那类**不报错的错数**就是这么来的。

这里换一种做法：**口径作为数据登记一次**，谁用谁读。

  · 引擎（Wren）：读 MDL 的 `views`，把口径视图展开成真实 SQL；
  · 网关（规划器）：读 MDL 的 `x_askoda_metrics`，按口径拼维度/时间/排序；
  · 门禁：读同一份 MDL，校验 SQL 只引用已登记对象。

三处读的是同一份文件，**改口径只改 `db/credit/04_metrics.json` + 重跑本脚本**，
不需要改代码、不需要发版。

用法
----
    # 静态校验：口径引用的视图/列/模型是否都真实存在（不碰网络）
    python tools/credit_metrics.py --check

    # 落到引擎与登记表（改口径后跑这一步才生效）
    python tools/credit_metrics.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "gateway"))

# 判据与网关共用一份实现（`gateway/metric_spec.py`）：
# 界面保存时拦的和门禁判的必须是同一套规则，否则「页面能存、门禁判红」一定会发生。
from metric_spec import (  # noqa: E402
    SQL_KEYWORDS as _SQL_KEYWORDS,  # noqa: F401  —— 保留旧引用名
    build_mdl,  # noqa: E402,F401
    static_check,  # noqa: E402,F401
    tables_in_statement as _tables_in_statement,  # noqa: E402,F401
)

DEFAULT_METRICS = os.path.join(ROOT, "db", "credit", "04_metrics.json")
DEFAULT_MDL = os.path.join(ROOT, "wren-docker-c", "workspace", "mdl.json")
DEFAULT_ENGINE = "http://127.0.0.1:9004"
DEFAULT_META_DSN = "postgresql://assistant:assistant@127.0.0.1:15434/assistant"


def load_metrics(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def deploy_engine(engine_url: str, mdl: dict, timeout: float = 120.0) -> str:
    """热部署到 Wren 引擎（不重启容器）。"""
    sys.path.insert(0, os.path.join(ROOT, "gateway"))
    from wren import WrenClient  # noqa: E402

    cli = WrenClient(engine_url, name="credit", timeout=timeout)
    txt = cli.call_text("deploy_manifest", {"mdl": mdl})
    return (txt or "").strip()[:200]


def update_registry(meta_dsn: str, key: str, mdl: dict) -> int:
    """把同一份 MDL 写进数据集登记表（网关读的是这里，不是容器内文件）。"""
    try:
        import psycopg
    except Exception:  # pragma: no cover
        print("⚠️ psycopg 不可用，跳过登记表更新（网关将读不到新口径）")
        return 0
    with psycopg.connect(meta_dsn) as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE dataset_registry SET mdl_json = %s, updated_at = now(), "
                "updated_by = %s WHERE dataset_key = %s",
                (json.dumps(mdl, ensure_ascii=False), "credit_metrics", key),
            )
            n = c.rowcount
        conn.commit()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="信贷口径注册（写进 MDL）")
    ap.add_argument("--metrics", default=DEFAULT_METRICS)
    ap.add_argument("--mdl", default=DEFAULT_MDL)
    ap.add_argument("--engine-url", default=DEFAULT_ENGINE)
    ap.add_argument("--meta-dsn", default=DEFAULT_META_DSN)
    ap.add_argument("--check", action="store_true", help="只做静态校验")
    ap.add_argument("--apply", action="store_true", help="写文件 + 部署引擎 + 更新登记表")
    args = ap.parse_args(argv)

    metrics = load_metrics(args.metrics)
    with open(args.mdl, encoding="utf-8") as f:
        mdl = json.load(f)

    problems = static_check(metrics, mdl)
    if problems:
        print("❌ 口径与 MDL 不一致（%d 处）：" % len(problems))
        for p in problems:
            print("   -", p)
        return 1
    n_views = len(metrics.get("views") or [])
    n_metrics = len(metrics.get("metrics") or [])
    print("✅ 静态校验通过：%d 个口径视图 / %d 条口径 / MDL %d 个模型"
          % (n_views, n_metrics, len(mdl.get("models") or [])))
    if not args.apply:
        return 0

    new_mdl = build_mdl(mdl, metrics)
    with open(args.mdl, "w", encoding="utf-8") as f:
        json.dump(new_mdl, f, ensure_ascii=False, indent=2)
    print("✅ 已写入 %s" % args.mdl)

    try:
        msg = deploy_engine(args.engine_url, new_mdl)
        print("✅ 引擎已装载：%s" % (msg or "ok"))
    except Exception as e:  # noqa: BLE001
        print("❌ 引擎装载失败：%s: %s" % (type(e).__name__, str(e)[:300]))
        return 1

    n = update_registry(args.meta_dsn, metrics.get("dataset") or "C", new_mdl)
    print(("✅ 登记表已更新（%d 行）" % n) if n else "⚠️ 登记表未更新")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
