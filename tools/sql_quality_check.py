#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G2 独立复核：SQL 质量不得低于基线（回归护栏）。

这一层为什么必须存在
--------------------
`tools/sql_quality_selftest.py` 证的是**测量工具本身**对不对（评分口径、回退方向）。
但工具对了，不代表**产品没退步**。本脚本才是那个"拿现在的产品去撞基线"的动作：

  · 改一个规划器分支、换一句 prompt、升一次依赖 ⇒ 逐题重跑；
  · 指标或逐题成绩低于 `tools/sql_quality_baseline.json` ⇒ 判红；
  · 只是比基线好 ⇒ 绿，并把"哪几题从不过变过"报出来（改善要可见）。

⚠️ 本脚本**不改基线、不改题集**。要让红线动，只能改代码，或人主动跑
   `sql_quality_baseline.py --update`（那是显式动作，会留下 updated_at）。

为什么"分数低"不算红、只有"分数掉"才算红
------------------------------------------
基线记的是**当前真实水平**（含已知差距，如跨指标组合、分组内取最大）。
把"没达到理想值"判红，结果是**天天红 ⇒ 团队绕过门禁**，比不做更糟。
真正要防的是**本来会的现在不会了**——那一定意味着有人改坏了东西。
⇒ 判据：不低于基线。理想值写进题集的 note，不写进红线。

退出码：0 = 不低于基线；1 = 回退或环境有问题（缺 MDL / 缺基线 / 题集空）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sql_quality_baseline as qb  # noqa: E402


def main():
    print("=" * 78)
    print("SQL 质量基线复核 · 当前实现 vs 基线")
    print("  题集：%s" % os.path.relpath(qb.CORPUS_PATH, HERE))
    print("  基线：%s" % os.path.relpath(qb.BASELINE_PATH, HERE))
    print("=" * 78)

    if not os.path.isfile(qb.BASELINE_PATH):
        print("❌ 基线文件不存在：%s" % qb.BASELINE_PATH)
        print("   ⇒ 先跑 tools/sql_quality_baseline.py 看分数，确认后 --update 写入。")
        print("   ⚠️ 这里**不会**自动生成基线：自动生成等于把「当前的错」定成标准。")
        return 1

    import json
    with open(qb.BASELINE_PATH, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    try:
        records, metrics = qb.run()
    except SystemExit as e:
        print("❌ 无法完成测量：%s" % e)
        return 1

    has_drop, drops = qb.compare(metrics, baseline)
    case_drops = qb.case_regressions(records, baseline)
    improved = qb.new_passes(records, baseline)

    print("\n当前分数：")
    for key, label in qb.METRICS:
        old = (baseline.get("metrics") or {}).get(key)
        cur = metrics.get(key, 0.0)
        mark = "  " if old is None or cur + 1e-9 >= float(old) else "↓ "
        print("  %s%-18s %-8s %s" % (mark, key, qb.pct(cur), label))
    print("  题数 %d（该出数 %d / 该拒绝 %d）"
          % (metrics["total"], metrics["sql_cases"], metrics["refuse_cases"]))

    known = [r for r in records
             if (baseline.get("cases") or {}).get(r["id"]) is False and not r["ok"]]
    if known:
        print("\n已知差距（基线里就不过，不算本次回退；修好会自动变成改善）：")
        for r in known:
            print("  %-6s %s —— %s" % (r["id"], r["text"], r["why"]))

    if improved:
        print("\n✅ 相比基线改善（从不过变过）：%s"
              % "、".join(r["id"] for r in improved))

    if drops or case_drops:
        print("\n❌ 总判定：低于基线 —— 有回退。")
        for key, label, old, cur in drops:
            print("  [指标] %-18s %s → %s   %s" % (key, qb.pct(old), qb.pct(cur), label))
        for r in case_drops:
            print("  [逐题] %-6s %s —— %s" % (r["id"], r["text"], r["why"]))
        print("\n  ⇒ 先确认是不是本次改动引入的（对照 git diff 看规划器/prompt/依赖）。")
        print("  ⇒ 确实要接受新水平，必须由人显式跑 `--update` 并说明理由，")
        print("     **不要**为了让这一层变绿而改题集里的期望值。")
        return 1

    print("\n总判定：✅ 不低于基线（%d 题整体达标率 %s）"
          % (metrics["total"], qb.pct(metrics["overall_rate"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
