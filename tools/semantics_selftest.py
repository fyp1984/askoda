# -*- coding: utf-8 -*-
"""语义分析 · 闸门与降级自证（离线可跑，不依赖容器）

守什么
------
本文件守两类**沉默失效**——它们不会报错，只会悄悄让结论变得不可信：

  A. 技术词闸门失效
     "确认问题不许含表名/字段名/SQL 技术语"是 M3 的验收标准之一。闸门若失效，
     表现不是报错，而是**物理表名被平静地写给业务**（实测踩到过：题干干净，
     选项里带着表名）。所以必须用负例证明它真的会拦，而不是摆着好看。

  B. 证据源缺失被读成"没问题"
     `collect()` 在某个来源取不到时返回空列表，与"查过确实没有"无法区分。
     若无人把关，P7（表关系）取不到时 R1（一对多，blocking 级）会被静默跳过——
     "订单金额 vs 商品明细金额核对"这种教科书级的一对多需求会报告"无风险"。
     所以 `degradation()` 必须有断言：缺了就要显式说出来。

用法：
    python tools/semantics_selftest.py
退出码：0 全过；1 有断言失败
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))
sys.path.insert(0, HERE)

import semantics as se  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        print("  \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)


# ---------------------------------------------------------------------------
# A. 结构常量
# ---------------------------------------------------------------------------
def test_constants():
    print("\nA. 结构常量")
    check("六槽位齐备", se.SLOT_KEYS == ["subject", "granularity", "time", "scope", "fields", "risks"],
          "实际：%s" % se.SLOT_KEYS)
    check("槽位中文名与槽位一一对应", set(se.SLOT_LABELS) == set(se.SLOT_KEYS))
    check("置信度基线覆盖 P1–P9",
          all(("P%d" % i) in se.LEVEL_BASE for i in range(1, 10)),
          "缺：%s" % [("P%d" % i) for i in range(1, 10) if ("P%d" % i) not in se.LEVEL_BASE])
    check("业务确认(P2)是最高可信来源",
          se.LEVEL_BASE["P2"] == max(se.LEVEL_BASE.values()),
          "P2=%s，最大=%s" % (se.LEVEL_BASE["P2"], max(se.LEVEL_BASE.values())))
    check("模型推断(P9)是兜底最低分",
          se.LEVEL_BASE["P9"] == min(se.LEVEL_BASE.values()),
          "P9=%s" % se.LEVEL_BASE["P9"])
    check("需确认上限低于业务确认分（不许高置信地不确定）",
          se.CONF_CAP_NEEDS_CONFIRM < se.LEVEL_BASE["P2"])
    check("降级上限不高于需确认上限",
          se.CONF_CAP_DEGRADED <= se.CONF_CAP_NEEDS_CONFIRM,
          "降级=%s 需确认=%s" % (se.CONF_CAP_DEGRADED, se.CONF_CAP_NEEDS_CONFIRM))


# ---------------------------------------------------------------------------
# B. 置信度
# ---------------------------------------------------------------------------
def test_confidence():
    print("\nB. confidence —— 交付级结论才是高置信")
    check("无证据 = 模型推断 0.30", se.confidence([]) == 0.30, "实际 %s" % se.confidence([]))
    check("P2 证据 = 0.85", se.confidence(["P2"]) == 0.85, "实际 %s" % se.confidence(["P2"]))
    check("多来源取最高（低优先级不得拉低）",
          se.confidence(["P1", "P6"]) == se.LEVEL_BASE["P6"],
          "实际 %s" % se.confidence(["P1", "P6"]))
    check("置信度不超过 0.95 上限", se.confidence(["P2"], extra=0.5) == 0.95,
          "实际 %s" % se.confidence(["P2"], extra=0.5))
    check("cap 生效", se.confidence(["P2"], cap=0.5) == 0.5,
          "实际 %s" % se.confidence(["P2"], cap=0.5))


# ---------------------------------------------------------------------------
# C. 结论构造：来源必须可追溯
# ---------------------------------------------------------------------------
def test_cand():
    print("\nC. _cand —— 每条结论都带来源")
    full = [
        {"level": "P6", "level_name": "数据字典", "source": "column_docs",
         "locator": "B.dim_store.store_name", "content": "门店名称"},
    ]
    c = se._cand("subject", "dim_store", full)
    check("证据被压成引用摘要", c["evidence"][0]["source"] == "column_docs"
          and c["evidence"][0]["locator"] == "B.dim_store.store_name")
    check("引用摘要保留 level / level_name",
          c["evidence"][0]["level"] == "P6" and c["evidence"][0]["level_name"] == "数据字典")
    check("置信度按证据等级自动计算", c["confidence"] == se.LEVEL_BASE["P6"],
          "实际 %s" % c["confidence"])
    check("默认不需确认", c["needs_confirmation"] is False)

    c2 = se._cand("granularity", "未定", full, needs=True)
    check("needs=True 时置信度被压到上限以内",
          c2["confidence"] <= se.CONF_CAP_NEEDS_CONFIRM, "实际 %s" % c2["confidence"])
    check("needs=True 被标记", c2["needs_confirmation"] is True)

    c3 = se._cand("scope", "x", [], conf=0.4)
    check("无证据也可构造（但只给显式给定的低分）",
          c3["evidence"] == [] and c3["confidence"] == 0.4)


# ---------------------------------------------------------------------------
# D. 技术词闸门（负例为主）
# ---------------------------------------------------------------------------
def test_tech_gate():
    print("\nD. 技术词闸门 —— 用负例证明它真的会拦")
    tech = ["dwd_order_di", "dwd_order_detail_di", "dim_member", "member_phone"]

    check("命中物理表名", se._technical_hits("按 dwd_order_di 查", tech) != [],
          "实际 %s" % se._technical_hits("按 dwd_order_di 查", tech))
    check("大小写不敏感", "dwd_order_di" in se._technical_hits("查 DWD_ORDER_DI", tech),
          "实际 %s" % se._technical_hits("查 DWD_ORDER_DI", tech))
    check("下划线标识符不被词边界误放行",
          se._technical_hits("看 dwd_order_detail_di", tech) != [])
    check("词边界拦截子串误伤：sum 不应命中 summary",
          se._technical_hits("这段 summary 很长", ["sum"]) == [],
          "实际 %s" % se._technical_hits("这段 summary 很长", ["sum"]))
    check("词边界仍然放行真命中：sum 命中 sum(x)",
          se._technical_hits("求 sum(x)", ["sum"]) != [])
    check("过短词条被忽略（避免噪音）",
          se._technical_hits("这行代表什么", ["行"]) == [],
          "实际 %s" % se._technical_hits("这行代表什么", ["行"]))
    check("内置中文技术词命中：主键",
          "主键" in se._technical_hits("请给出主键", []))
    check("内置 SQL 词命中：GROUP BY",
          "GROUP BY" in se._technical_hits("这里要不要 GROUP BY", []))


def test_question_gate():
    print("\nE. 问题闸门 —— 干净的要放行，脏的要拦下")

    base_state = {"granularity": {"needs_confirmation": True}, "time": {}, "rules": {}}
    clean_bundle = {"text": "", "technical_terms": []}

    out = se.questions(clean_bundle, base_state)
    qids = [q["question_id"] for q in out["confirmation_questions"]]
    check("干净问题被放行（Q-GRAIN-1）", "Q-GRAIN-1" in qids, "实际 %s" % qids)
    check("放行的问题自带影响面",
          all(str(q.get("impact_scope") or "").strip() for q in out["confirmation_questions"]))
    check("放行的问题自带可选答案",
          all(q.get("options") for q in out["confirmation_questions"]))
    check("无问题被误拦", out["dropped_questions"] == [],
          "被拦：%s" % [d["question_id"] for d in out["dropped_questions"]])

    # 负例一：问题文本命中技术语 → 必须拦下，且如实报出被拦的term。
    # 这里人为把"每一行"标成技术语——它确实出现在 Q-GRAIN-1 的题干里，
    # 于是可以验证"闸门是活的"，而不是只验证"当前这批问题恰好干净"。
    dirty = {"text": "", "technical_terms": ["每一行"]}
    out2 = se.questions(dirty, base_state)
    qids2 = [q["question_id"] for q in out2["confirmation_questions"]]
    dropped2 = {d["question_id"]: d["blocked_terms"] for d in out2["dropped_questions"]}
    check("题干命中技术语 → 被拦下", "Q-GRAIN-1" not in qids2, "仍放行：%s" % qids2)
    check("被拦原因如实披露（不是静默丢弃）",
          "Q-GRAIN-1" in dropped2 and "每一行" in dropped2.get("Q-GRAIN-1", []),
          "实际 %s" % dropped2)

    # 负例二：选项里带技术语 → 也必须拦下（实测踩过的洞：只扫题干会漏）
    dirty_opt_bundle = {"text": "", "technical_terms": ["其他（请补充说明）"]}
    out3 = se.questions(dirty_opt_bundle, base_state)
    qids3 = [q["question_id"] for q in out3["confirmation_questions"]]
    check("选项命中技术语 → 同样被拦下（闸门扫全部对外文本）",
          "Q-GRAIN-1" not in qids3, "仍放行：%s" % qids3)

    # 负例三：缺影响面 / 缺选项 → 拦下。
    # 这两条分支在真实生成逻辑里被"凡是 add() 都必须传 impact 与 options"约束住，
    # 正常路径构造不出来，所以改为**源码级断言**：确认实现里确实有这两个处置分支。
    # 断言写死文案是有意的——文案是留给复核人看的，改文案就该改测试。
    src = open(os.path.join(ROOT, "gateway", "semantics.py"), encoding="utf-8").read()
    for label, why in (
        ("缺少 impact_scope：说不清影响的问题不问", "缺影响面的问题必须拦下"),
        ("未给建议选项：业务无法作答的问题不问", "缺选项的问题必须拦下"),
    ):
        check("闸门分支存在：%s" % why, label in src, "未在实现中找到该处置文案")


# ---------------------------------------------------------------------------
# F. 证据源降级
# ---------------------------------------------------------------------------
def test_degradation():
    print("\nF. degradation —— 把「取不到」显式说出来")

    ok_bundle = {
        "status": {
            "P1": {"ok": True, "count": 1},
            "P6": {"ok": True, "count": 21},
            "P7": {"ok": True, "count": 8},
        }
    }
    check("全部就绪时无降级", se.degradation(ok_bundle) == [],
          "实际 %s" % se.degradation(ok_bundle))

    bad_bundle = {
        "status": {
            "P6": {"ok": True, "count": 21},
            "P7": {"ok": False, "count": 0, "error": "FileNotFoundError: /workspace-b/mdl.json"},
            "P2": {"ok": False, "count": 0, "error": "meta db down"},
        }
    }
    deg = se.degradation(bad_bundle)
    by = {d["level"]: d for d in deg}
    check("P7 缺失被报出", "P7" in by, "实际 %s" % list(by))
    check("P7 缺失判为 blocking（它支撑 blocking 级 R1）",
          by["P7"]["severity"] == "blocking", "实际 %s" % by["P7"]["severity"])
    check("P7 缺失说明影响的是 R1 未生效",
          "R1" in by["P7"]["impact"], "实际 %r" % by["P7"]["impact"])
    check("P7 缺失保留原始错误（便于排障）",
          "mdl.json" in (by["P7"]["error"] or ""), "实际 %r" % by["P7"]["error"])
    check("仅支撑参考性判断的 P2 缺失只算 warning",
          by["P2"]["severity"] == "warning", "实际 %s" % by["P2"]["severity"])
    check("就绪的来源不出现", "P6" not in by)

    risks = se._degraded_risks(deg)
    check("降级转为风险结论（与规则风险同列，免得被漏读）",
          len(risks) == len(deg), "%d vs %d" % (len(risks), len(deg)))
    check("降级风险带独立 rule_id 便于聚合",
          all(r.get("rule_id") == "SRC-UNAVAILABLE" for r in risks))
    check("降级风险保留严重级",
          {r.get("severity") for r in risks} == {"blocking", "warning"},
          "实际 %s" % {r.get("severity") for r in risks})
    check("降级风险也有来源可追溯",
          all(r["evidence"] and r["evidence"][0]["source"] == "evidence.status" for r in risks))


# ---------------------------------------------------------------------------
# G. 泛化词门控（主体精度）
# ---------------------------------------------------------------------------
def test_generic_gate():
    print("\nG. _tables_of —— 泛化词不足以单独确立主体")

    def hit(table, label, term):
        return {"table": table, "label": label, "term": term, "matched": term}

    strong = hit("t_strong", "坪效", "坪效")          # term == clean_label → 非泛化
    weak1 = hit("t_weak1", "归属门店", "门店")        # 去修饰变体 → 泛化
    weak2 = hit("t_weak2", "所属大区", "大区")        # 另一个泛化词
    check("术语命中（term==规范标签）不算泛化", se._is_derived_hit(strong) is False)
    check("去修饰变体算泛化", se._is_derived_hit(weak1) is True)

    got = se._tables_of([strong, weak1])
    check("有强命中 → 该表入选", "t_strong" in got, "实际 %s" % sorted(got))
    check("仅 1 个泛化词 → 该表被剔除（门控生效）",
          "t_weak1" not in got, "实际 %s" % sorted(got))

    got2 = se._tables_of([hit("t_w2", "归属门店", "门店"), hit("t_w2", "所属大区", "大区")])
    check("≥%d 个不同泛化词 → 该表入选" % se.GENERIC_ONLY_MIN,
          "t_w2" in got2, "实际 %s" % sorted(got2))


def main():
    print("=" * 66)
    print("语义分析 · 闸门与降级自证")
    print("=" * 66)
    test_constants()
    test_confidence()
    test_cand()
    test_tech_gate()
    test_question_gate()
    test_degradation()
    test_generic_gate()
    print()
    print("-" * 66)
    print("共 %d 项断言，失败 %d 项" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ %s" % f)
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
