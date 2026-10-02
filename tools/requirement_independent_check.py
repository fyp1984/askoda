#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4-2 独立对抗复扫（验收方自写，不复用交付方的 requirement_selftest）。

目的有三：
1. **契约一致性交叉验证**：手写校验器（requirement.validate）的必填字段集、
   status 枚举、SlotClaim 子字段，必须与 JSON 契约文件（structured_requirement.schema.json）
   **逐项一致**——两处定义漂移是这类"手写校验"最典型的隐性缺陷。
2. **对抗输入**：交付方的自证只测了它自己想得到的输入；这里专塞"看着合法、实则越界"的输入
   （边界值 0/1、超界 1e-7、bool 冒充 int、null 冒充 str、空数组 vs 缺失、非数组型数组字段）。
3. **边界正例**：confidence 恰为 0 / 1 必须**通过**（闭区间 [0,1]），防止实现把边界写成开区间。

在容器内运行：
    docker cp tools/requirement_independent_check.py demand-gateway:/tmp/
    docker exec -e PYTHONPATH=/app demand-gateway python /tmp/requirement_independent_check.py
退出码 0 = 全过。
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))

import requirement as req  # noqa: E402
import semantics as sem  # noqa: E402

fails = []


def ev(level="P1", src="independent.check"):
    return [{"level": level, "level_name": "独立复扫", "rank": 1,
             "source": src, "locator": "n/a", "content": "独立构造的证据"}]


def claim(value, conf=0.8, evidence=None):
    return {"value": value, "evidence": ev() if evidence is None else evidence,
            "confidence": conf}


def base_obj():
    return {
        "demand_id": "IND-001",
        "dataset": "B",
        "version": 1,
        "source_round": 1,
        "schema_version": "deadbeef",
        "subject": claim("会员"),
        "granularity": claim("按会员按月"),
        "time_semantics": claim("统计期间：本月"),
        "data_scope": claim("活跃会员"),
        "output_fields": [claim("会员ID"), claim("消费金额合计")],
        "aggregation_rules": [],
        "confirmed_facts": [],
        "evidence_chain": ev(),
        "residual_risks": [],
        "status": "待审核",
    }


def expect_fail(title, obj, path_substr):
    r = req.validate(obj)
    paths = [e.get("path", "") for e in r.get("errors", [])]
    hit = any(path_substr in p for p in paths)
    ok = (r.get("ok") is False) and hit
    print("[%s] %s" % ("OK" if ok else "!!", title))
    if not ok:
        fails.append("%s -> ok=%s paths=%s" % (title, r.get("ok"), paths))
    else:
        print("     命中 path：", [p for p in paths if path_substr in p][:3])
    return ok


def expect_pass(title, obj):
    r = req.validate(obj)
    ok = r.get("ok") is True and len(r.get("errors", [])) == 0
    print("[%s] %s" % ("OK" if ok else "!!", title))
    if not ok:
        fails.append("%s -> errors=%s" % (title, r.get("errors")))
    return ok


def main():
    print("=" * 78)
    print("第一段 · 契约一致性交叉验证（手写校验器 vs JSON 契约文件）")
    print("=" * 78)
    schema_path = os.path.join(os.path.dirname(req.__file__),
                               "contracts", "structured_requirement.schema.json")
    if not os.path.exists(schema_path):
        print("  找不到契约文件：", schema_path)
        sys.exit(2)
    sch = json.load(open(schema_path, encoding="utf-8"))

    sch_req = list(sch.get("required", []))
    sch_props = list(sch.get("properties", {}).keys())
    checks = [
        ("契约 required 与 properties 键集合一致",
         set(sch_req) == set(sch_props)),
        ("契约 additionalProperties 为 false（顶层不许加字段）",
         sch.get("additionalProperties") is False),
        ("校验器 REQUIRED_TOP == 契约 required",
         set(req.REQUIRED_TOP) == set(sch_req)),
        ("契约 status 枚举 == 校验器 VALID_STATUS",
         set(sch["properties"]["status"]["enum"]) == set(req.VALID_STATUS)),
        ("SlotClaim 必填集 == {value,evidence,confidence}",
         set(sch["definitions"]["SlotClaim"]["required"])
         == {"value", "evidence", "confidence"}),
        ("契约 evidence 有 minItems=1（无源结论=违规）",
         sch["definitions"]["SlotClaim"]["properties"]["evidence"].get("minItems") == 1),
        ("契约 confidence 闭区间 [0,1]",
         sch["definitions"]["SlotClaim"]["properties"]["confidence"].get("minimum") == 0.0
         and sch["definitions"]["SlotClaim"]["properties"]["confidence"].get("maximum") == 1.0),
        ("契约单值槽位字段数 == 校验器 SINGLE_SLOT_FIELDS 数",
         len(req.SINGLE_SLOT_FIELDS) == 4),
    ]
    for title, cond in checks:
        print("[%s] %s" % ("OK" if cond else "!!", title))
        if not cond:
            fails.append("契约一致性：" + title)

    print()
    print("=" * 78)
    print("第二段 · 独立对抗用例（越界 / 类型冒充 / 缺失）")
    print("=" * 78)

    o = base_obj()
    expect_pass("基准对象应通过（保证后续失败是突变造成的，而非基准本身坏了）", copy.deepcopy(o))

    # --- 必填字段逐个删除：每个都必须被抓 ---
    for k in req.REQUIRED_TOP:
        m = copy.deepcopy(o)
        del m[k]
        expect_fail("删必填顶层字段 '%s'" % k, m, "$")

    # --- 额外字段 ---
    m = copy.deepcopy(o); m["bogus_extra"] = 1
    expect_fail("多塞顶层字段 'bogus_extra'", m, "$.bogus_extra")

    # --- 字符串型字段的非法值 ---
    for k in ("demand_id", "dataset", "schema_version"):
        m = copy.deepcopy(o); m[k] = ""
        expect_fail("%s 为空串" % k, m, "$." + k)
        m2 = copy.deepcopy(o); m2[k] = None
        expect_fail("%s 为 None（冒充 str）" % k, m2, "$." + k)

    # --- 整数型字段的非法值 ---
    m = copy.deepcopy(o); m["version"] = 0
    expect_fail("version=0（<1）", m, "$.version")
    m = copy.deepcopy(o); m["version"] = 1.0
    expect_fail("version=1.0（float 冒充 int）", m, "$.version")
    m = copy.deepcopy(o); m["version"] = True
    expect_fail("version=True（bool 冒充 int）", m, "$.version")
    m = copy.deepcopy(o); m["source_round"] = 0
    expect_fail("source_round=0（<1）", m, "$.source_round")

    # --- status 枚举 ---
    for bad in ("审核通过", "待审批", "已批准", "", "pending"):
        m = copy.deepcopy(o); m["status"] = bad
        expect_fail("status=%r 不在枚举内" % bad, m, "$.status")

    # --- SlotClaim 子字段 ---
    m = copy.deepcopy(o); del m["subject"]["confidence"]
    expect_fail("subject 缺 confidence 键", m, "$.subject.confidence")
    m = copy.deepcopy(o); del m["granularity"]["value"]
    expect_fail("granularity 缺 value 键", m, "$.granularity.value")
    m = copy.deepcopy(o); m["time_semantics"]["evidence"] = []
    expect_fail("time_semantics.evidence=[]（无源结论）", m, "$.time_semantics.evidence")
    m = copy.deepcopy(o); m["data_scope"]["evidence"] = "not-a-list"
    expect_fail("data_scope.evidence 非数组", m, "$.data_scope.evidence")

    # --- confidence 越界（含极小越界，防比较写成 <1 漏掉）---
    m = copy.deepcopy(o); m["subject"]["confidence"] = 1.0000001
    expect_fail("subject.confidence=1.0000001（刚超上界）", m, "$.subject.confidence")
    m = copy.deepcopy(o); m["subject"]["confidence"] = -1e-7
    expect_fail("subject.confidence=-1e-7（刚超下界）", m, "$.subject.confidence")
    m = copy.deepcopy(o); m["output_fields"][1]["confidence"] = 2
    expect_fail("output_fields[1].confidence=2", m, "$.output_fields[1].confidence")
    m = copy.deepcopy(o); m["output_fields"][0]["confidence"] = "0.5"
    expect_fail("output_fields[0].confidence='0.5'（str 冒充数字）", m, "$.output_fields[0].confidence")
    m = copy.deepcopy(o); m["subject"]["confidence"] = True
    expect_fail("subject.confidence=True（bool 冒充数字）", m, "$.subject.confidence")

    # --- output_fields 结构 ---
    m = copy.deepcopy(o); m["output_fields"] = {"a": 1}
    expect_fail("output_fields 是 dict 而非数组", m, "$.output_fields")
    m = copy.deepcopy(o); m["output_fields"] = [{"value": "x", "confidence": 0.5}]
    expect_fail("output_fields[0] 缺 evidence 键", m, "$.output_fields[0]")

    # --- 其余数组字段类型 ---
    for k in ("aggregation_rules", "confirmed_facts", "evidence_chain", "residual_risks"):
        m = copy.deepcopy(o); m[k] = "not-a-list"
        expect_fail("%s 非数组" % k, m, "$." + k)

    # --- residual_risks 元素结构 ---
    m = copy.deepcopy(o); m["residual_risks"] = [{"value": "r", "evidence": ev()}]
    expect_fail("residual_risks[0] 缺 confidence 子字段", m, "$.residual_risks[0]")

    # --- 顶层非 dict ---
    expect_fail("顶层是 list（非对象）", ["x"], "$")

    print()
    print("=" * 78)
    print("第三段 · 边界正例（必须通过，防实现把闭区间写成开区间）")
    print("=" * 78)
    m = copy.deepcopy(o); m["subject"]["confidence"] = 0.0
    expect_pass("subject.confidence=0（下界闭）", m)
    m = copy.deepcopy(o); m["subject"]["confidence"] = 1.0
    expect_pass("subject.confidence=1（上界闭）", m)
    m = copy.deepcopy(o); m["output_fields"] = []
    expect_pass("output_fields=[]（契约未设 minItems，应允许）", m)
    m = copy.deepcopy(o); m["status"] = "已通过"
    expect_pass("status=已通过（合法枚举）", m)

    print()
    print("=" * 78)
    if fails:
        print("❌ 独立复扫发现 %d 项问题：" % len(fails))
        for f in fails:
            print("   -", f)
        sys.exit(1)
    print("✅ 独立复扫全过：契约一致 + 对抗用例全被抓 + 边界正例全通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
