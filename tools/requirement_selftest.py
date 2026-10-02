#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4-2 结构化技术需求对象自证（离线假数据，不连库、不启 Wren）。

validate 是纯函数，可完全离线自证；build/get 需要 DB，离线侧只测
SLOT_MAP 与 semantics.SLOT_KEYS 的对齐（确保设计文档键名不会被误用
成真实 M3 槽位键，触发 KeyError）。

用法：
    python3 tools/requirement_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys
import copy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))

# ---------------------------------------------------------------------------
# 假证据：合法 evidence 元素（至少含 level/source/content/locator）
# ---------------------------------------------------------------------------
P1_EV = [{
    "level": "P1",
    "level_name": "需求原文",
    "rank": 1,
    "source": "demand_requests.description",
    "content": "需求原文关键词命中",
    "locator": "line=12 char=5",
}]

P2_EV = [{
    "level": "P2",
    "level_name": "业务确认",
    "rank": 2,
    "source": "confirmations.answer",
    "content": "业务确认答复",
    "locator": "question_id=Q-GRAIN-1 version=1",
}]

P6_EV = [{
    "level": "P6",
    "level_name": "数据字典",
    "rank": 5,
    "source": "mdl.columns.description",
    "content": "字典描述字段命中",
    "locator": "model=dim_store column=store_code",
}]


def _claim(value, ev, conf):
    """造一个合法 SlotClaim。"""
    return {"value": value, "evidence": ev, "confidence": conf}


def _legal_object():
    """返回一个合法（validate 应全绿）的结构化技术需求对象。

    槽位键映射检查：此处构造假 M3 slots 时，用 **真实 SLOT_KEYS 名**（subject/granularity/
    time/scope/fields/risks），而不是设计文档那套 time_semantics/data_scope——
    requirement.SLOT_MAP 是唯一映射点，self-test 5 会验证 build 走 SLOT_MAP
    时对 slots['time'] / slots['scope'] 取值正确。
    """
    obj = {
        "demand_id": "DEMO-001",
        "dataset": "B",
        "version": 1,
        "source_round": 1,
        "schema_version": "a1b2c3d4",
        "subject": _claim("会员", P6_EV, 0.80),
        "granularity": _claim("按会员按月（一位会员每月一行）", P2_EV, 0.85),
        "time_semantics": _claim("统计期间：本月 + 发生时间（下单日期）", P1_EV, 0.75),
        "data_scope": _claim("限定：活跃会员，排除：已流失会员", P1_EV, 0.65),
        "output_fields": [
            _claim("会员ID", P6_EV, 0.90),
            _claim("本月消费金额合计", P1_EV + P6_EV, 0.72),
            _claim("平均客单价", P6_EV, 0.55),
            _claim("近30天复购率", P6_EV, 0.50),
            _claim("各门店TOP10商品销售额排名", P1_EV, 0.48),
        ],
        "aggregation_rules": [
            {"rule": "汇总求和", "field": "本月消费金额合计", "matched_keyword": "合计"},
            {"rule": "平均",     "field": "平均客单价",       "matched_keyword": "平均"},
            {"rule": "比率",     "field": "近30天复购率",    "matched_keyword": "率"},
            {"rule": "排名",     "field": "各门店TOP10商品销售额排名", "matched_keyword": "TOP"},
            {"rule": "分组",     "field": "各门店TOP10商品销售额排名", "matched_keyword": "各"},
        ],
        "confirmed_facts": [{
            "question_id": "Q-GRAIN-1",
            "slot": "granularity",
            "question": "这份结果里的每一行，希望代表什么？",
            "answer": "按会员按月（一位会员每月一行）（选项：其他（请补充说明））",
            "version": 1,
            "answered_by": "zhang_san",
            "answered_at": "2026-09-30T10:20:00",
        }],
        "evidence_chain": P1_EV + P2_EV + P6_EV,
        "residual_risks": [{
            "value": "R6 比率类分母不得悬空：复购率分母口径未说明",
            "evidence": [{
                "level": "P8", "level_name": "通用规则", "rank": 7,
                "source": "semantics.rules() R6",
                "locator": "RATIO_WORDS=复购率  DENOM_WORDS未命中",
                "content": "需求出现比率类指标但未说明分母口径",
            }],
            "confidence": 0.90,
            "rule_id": "R6",
            "severity": "blocking",
        }],
        "status": "待审核",
    }
    return obj


def main():
    fails = []

    # ------------------------------------------------------------------
    # 1. 合法对象 validate 通过
    # ------------------------------------------------------------------
    import requirement as req  # noqa: E402

    title = "1. 合法对象：validate 全绿通过（errors=0）"
    obj = _legal_object()
    r = req.validate(obj)
    ok = r["ok"] and len(r["errors"]) == 0
    if not ok:
        fails.append("%s FAILED: errors=%s" % (title, r["errors"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    if not ok:
        for e in r["errors"]:
            print("    ERR ", e)

    # ------------------------------------------------------------------
    # 2. 故意删掉某槽位的 evidence → validate 不通过且 errors.path 指到该字段
    # ------------------------------------------------------------------
    title = "2. 删 data_scope.evidence 键：validate 报错，path=$.data_scope.evidence"
    o2 = copy.deepcopy(obj)
    del o2["data_scope"]["evidence"]
    r = req.validate(o2)
    has_err = any(e.get("path") == "$.data_scope.evidence" for e in r["errors"])
    ok = (not r["ok"]) and has_err
    if not ok:
        fails.append("%s FAILED: errors=%s" % (title, r["errors"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    errors.path 命中：", [e["path"] for e in r["errors"]])

    # ------------------------------------------------------------------
    # 3. confidence 越界：1.5（>1）和 -0.1（<0）被抓出
    # ------------------------------------------------------------------
    title = "3. confidence 越界：subject.confidence=1.5 与 output_fields[2].confidence=-0.1 被抓"
    o3 = copy.deepcopy(obj)
    o3["subject"]["confidence"] = 1.5
    o3["output_fields"][2]["confidence"] = -0.1
    r = req.validate(o3)
    paths = [e["path"] for e in r["errors"]]
    has_hi = "$.subject.confidence" in paths
    has_lo = "$.output_fields[2].confidence" in paths
    ok = (not r["ok"]) and has_hi and has_lo
    if not ok:
        fails.append("%s FAILED: paths=%s" % (title, paths))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    errors=", [e for e in r["errors"] if "confidence" in e["path"]])

    # ------------------------------------------------------------------
    # 4. 无源结论：evidence=空数组 → 被判为契约错误
    # ------------------------------------------------------------------
    title = "4. 无源结论：granularity.evidence=[] → 报 evidence 空数组错误"
    o4 = copy.deepcopy(obj)
    o4["granularity"]["evidence"] = []
    r = req.validate(o4)
    empty_hit = any(
        "空数组" in e.get("message", "") and e["path"] == "$.granularity.evidence"
        for e in r["errors"]
    )
    ok = (not r["ok"]) and empty_hit
    if not ok:
        fails.append("%s FAILED: errors=%s" % (title, r["errors"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    errors=", [e for e in r["errors"] if "granularity" in e["path"]])

    # ------------------------------------------------------------------
    # 5. 按 SLOT_KEYS 的真实键名（time / scope / fields / risks）能全部映射上
    #    防止误用设计文档那套 time_semantics / data_scope 作为 M3 取值键
    # ------------------------------------------------------------------
    title = ("5. SLOT_MAP 对齐 semantics.SLOT_KEYS：time_semantics→time；"
             "data_scope→scope；真实键全在 SLOT_KEYS 中")
    import semantics as sem  # noqa: E402
    sm = req.SLOT_MAP
    real_keys = {sm["time_semantics"], sm["data_scope"],
                 sm["subject"], sm["granularity"],
                 sm["output_fields"], sm["residual_risks"]}
    ok1 = sm["time_semantics"] == "time"           # 文档 time_semantics ≠ 真实槽 time
    ok2 = sm["data_scope"] == "scope"               # 文档 data_scope ≠ 真实槽 scope
    ok3 = real_keys.issubset(set(sem.SLOT_KEYS))    # 六真实键全在官方 SLOT_KEYS
    # 再证明：如果有人误用文档键直接取 slots，会 KeyError
    fake_slots_m3 = {k: {"candidates": [{"value": "ok", "evidence": P1_EV, "confidence": 0.5}]}
                     for k in sem.SLOT_KEYS}
    ok4 = False
    try:
        # 错误写法：直接 slots['time_semantics'] —— 会 KeyError
        _ = fake_slots_m3["time_semantics"]
    except KeyError:
        ok4 = True
    # 正确写法：经过 SLOT_MAP 拿真实键
    ok5 = req._slot_candidates(fake_slots_m3, "time_semantics")[0]["value"] == "ok"
    ok = ok1 and ok2 and ok3 and ok4 and ok5
    if not ok:
        fails.append(
            "%s FAILED: ok1=%s ok2=%s ok3=%s ok4=%s ok5=%s sm=%s real_keys=%s SLOT_KEYS=%s"
            % (title, ok1, ok2, ok3, ok4, ok5, sm, sorted(real_keys), sem.SLOT_KEYS)
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    SLOT_MAP:", sm)
    print("    ok1(time→time)=%s  ok2(data_scope→scope)=%s  ok3(全在SLOT_KEYS)=%s" % (ok1, ok2, ok3))
    print("    ok4(误用文档名会KeyError)=%s  ok5(经SLOT_MAP能取到)=%s" % (ok4, ok5))

    # ------------------------------------------------------------------
    # 6. 契约 additionalProperties=false：顶层多塞 'extra_bogus' → 不通过
    # ------------------------------------------------------------------
    title = "6. additionalProperties=false：顶层多塞 extra_bogus='x' → 不通过"
    o6 = copy.deepcopy(obj)
    o6["extra_bogus"] = "should not be here"
    r = req.validate(o6)
    extra_hit = any(
        e.get("path") == "$.extra_bogus" and "额外字段" in e.get("message", "")
        for e in r["errors"]
    )
    ok = (not r["ok"]) and extra_hit
    if not ok:
        fails.append("%s FAILED: errors=%s" % (title, r["errors"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    errors[.extra_bogus]=", [e for e in r["errors"] if "extra_bogus" in e["path"]])

    # ------------------------------------------------------------------
    # 7. status 枚举拼写错误（如 '审核通过'）被抓出
    # ------------------------------------------------------------------
    title = "7. status 枚举错误：'审核通过'（非 待审核/已通过/已退回）被抓出"
    o7 = copy.deepcopy(obj)
    o7["status"] = "审核通过"
    r = req.validate(o7)
    status_hit = any(
        e.get("path") == "$.status" and "必须是 待审核/已通过/已退回" in e.get("message", "")
        for e in r["errors"]
    )
    ok = (not r["ok"]) and status_hit
    if not ok:
        fails.append("%s FAILED: errors=%s" % (title, r["errors"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    errors[status]=", [e for e in r["errors"] if "status" in e["path"]])

    # ------------------------------------------------------------------
    # 8. 聚合规则派生（_derive_aggregation）：合法 output_fields 派生出正确规则
    # ------------------------------------------------------------------
    title = "8. _derive_aggregation：从 output_fields 的 value 关键词派生"
    out_fields = copy.deepcopy(obj["output_fields"])
    rules = req._derive_aggregation(out_fields)
    has_sum = any(r["rule"] == "汇总求和" and r["matched_keyword"] == "合计" for r in rules)
    has_avg = any(r["rule"] == "平均" and r["matched_keyword"] == "平均" for r in rules)
    has_ratio = any(r["rule"] == "比率" and r["matched_keyword"] == "率" for r in rules)
    has_rank = any(r["rule"] == "排名" and r["matched_keyword"] == "TOP" for r in rules)
    has_group = any(r["rule"] == "分组" and r["matched_keyword"] == "各" for r in rules)
    # 空字段列表 → 空规则（不编造）
    empty_rules = req._derive_aggregation([])
    ok = (has_sum and has_avg and has_ratio and has_rank and has_group
          and empty_rules == [])
    if not ok:
        fails.append("%s FAILED: rules=%s empty=%s" % (title, rules, empty_rules))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    派生规则数=%d，空列表派生=%s 条" % (len(rules), len(empty_rules)))
    for rl in rules:
        print("     -", rl)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    print()
    if fails:
        print("❌ 有 %d 项失败：" % len(fails))
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("✅ 全部自证用例通过（%d 项）" % 8)
    sys.exit(0)


if __name__ == "__main__":
    main()
