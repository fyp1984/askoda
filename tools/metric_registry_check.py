#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径登记一致性复核（独立复核层 G2，离线）

与 `metric_planner_selftest.py` 的分工
-------------------------------------
自检守的是**编译行为**（该拒绝的拒绝、该限定的限定）；本脚本守的是**登记质量**：
口径文件里的每一条，是否真的能落到 MDL 上、引用的视图/列/模型是否都真实存在。

为什么单独一段
--------------
口径是「数据」，登记错了不会有任何报错：视图名拼错 → 编译出来的 SQL 指向不存在的对象 →
要到用户点「生成」那一刻才炸；维度写成视图里没有的列 → 同理。这类错**必须前置到门禁**。

用法：
    python3 tools/metric_registry_check.py
退出码 0 = 通过，1 = 有问题。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

import credit_metrics  # noqa: E402

MDL_PATH = os.path.join(_ROOT, "wren-docker-c", "workspace", "mdl.json")
METRICS_PATH = os.path.join(_ROOT, "db", "credit", "04_metrics.json")


def main():
    if not (os.path.exists(MDL_PATH) and os.path.exists(METRICS_PATH)):
        print("  FAIL  找不到口径文件或 MDL：%s / %s" % (METRICS_PATH, MDL_PATH))
        print("\nmetric_registry_check: 0/1")
        return 1

    metrics = credit_metrics.load_metrics(METRICS_PATH)
    with open(MDL_PATH, encoding="utf-8") as f:
        mdl = json.load(f)

    problems = credit_metrics.static_check(metrics, mdl)

    # 追加判据：MDL 里的内嵌口径必须与登记文件同源（防止改了文件忘了 --apply）
    reg = mdl.get("x_askoda_metrics") or {}
    if not reg:
        problems.append("MDL 里没有 x_askoda_metrics：口径还没注入（跑 tools/credit_metrics.py --apply）")
    else:
        if reg.get("version") != metrics.get("version"):
            problems.append("版本号不一致：MDL=%s 文件=%s" % (reg.get("version"), metrics.get("version")))
        if len(reg.get("metrics") or []) != len(metrics.get("metrics") or []):
            problems.append("口径条数不一致：MDL=%d 文件=%d"
                            % (len(reg.get("metrics") or []), len(metrics.get("metrics") or [])))
        if set((reg.get("views") or {})) != {v["name"] for v in metrics.get("views") or []}:
            problems.append("视图清单不一致：MDL=%s" % sorted(reg.get("views") or {}))

    # 视图必须真的落到 MDL 的 views 段（引擎靠它展开）
    mdl_views = {v.get("name") for v in (mdl.get("views") or [])}
    for v in metrics.get("views") or []:
        if v["name"] not in mdl_views:
            problems.append("视图 %s 未写进 MDL.views（引擎不会认）" % v["name"])

    ok = not problems
    print("  OK    口径登记一致：%d 视图 / %d 口径"
          % (len(metrics.get("views") or []), len(metrics.get("metrics") or []))
          if ok else "  FAIL  口径登记不一致（%d 处）" % len(problems))
    for p in problems:
        print("        - %s" % p)
    print("\nmetric_registry_check: %d/%d" % (0 if problems else 1, 1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
