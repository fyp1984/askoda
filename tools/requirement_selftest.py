#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化需求派生 + 校验自证（离线，不需要数据库）。

为什么单独做这个自证
--------------------
`requirement.py` 是「需求理解 → 结构化需求」这一段的落点，是主链路之一，
此前**没有任何单元测试**。而它的两个关键函数一旦悄悄改错，外部表现全是
"看起来对但不对"，最难归因：

  · `_derive_aggregation` —— 派生聚合规则。它有个明确的**反兜底设计**：
    完全命中不到聚合词时必须返回 `[]`，**绝不能编造"默认求和"**。
    一旦这层塌了，`aggregation_rules` 就会凭空多出一条规则，
    下游 SQL 按不存在的要求去聚合，且**全程无报错**。

  · `validate` —— 结构化需求的出口校验，契约是 **JSON Schema 严格模式**
    （`additionalProperties=false` + 15 个必填顶层字段）。
    它是需求能不能进 SQL 生成的关卡，放行了非法需求就会一路错到底。

这两个函数都是**纯函数**（不碰 DB、不碰网络），所以可以离线钉死。

契约事实（实测自 gateway/requirement.py:74-88）：
  · 聚合规则名是**中文**：汇总求和 / 计数 / 平均 / 比率 / 排名 / 趋势对比 / 分组
    （不是 sum/avg/ratio——写断言时按实际值来，别按直觉命名写）
  · `REQUIRED_TOP` 共 15 项，比"看起来该有的"多不少

用法：
    python3 tools/requirement_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import requirement as rq  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# _derive_aggregation：从输出字段派生聚合规则
# ---------------------------------------------------------------------------

@case("聚合：「合计/总计/总和」应派生出「汇总求和」")
def _t_agg_sum():
    for word in ("合计销售额", "总计销售额", "销售额总和"):
        out = rq._derive_aggregation([{"value": word}])
        assert out, "「%s」必须派生出一条聚合规则，实际为空" % word
        assert out[0]["rule"] == "汇总求和", (
            "「%s」应派生成「汇总求和」，实际=%r" % (word, out[0]["rule"])
        )
        assert out[0]["matched_keyword"], "必须记录命中的聚合词，否则无法解释派生来源"


@case("聚合：「平均/均值/店均」应派生出「平均」")
def _t_agg_avg():
    for word in ("平均客单价", "均值销售额", "店均坪效"):
        out = rq._derive_aggregation([{"value": word}])
        assert out, "「%s」必须派生出一条聚合规则" % word
        assert out[0]["rule"] == "平均", (
            "「%s」应派生成「平均」，实际=%r" % (word, out[0]["rule"])
        )


@case("聚合：「占比/比例/百分比」应派生出「比率」")
def _t_agg_ratio():
    for word in ("销售占比", "复购率比例", "订单百分比"):
        out = rq._derive_aggregation([{"value": word}])
        assert out, "「%s」必须派生出一条聚合规则" % word
        assert out[0]["rule"] == "比率", (
            "「%s」应派生成「比率」，实际=%r" % (word, out[0]["rule"])
        )


@case("聚合：「排名/最高/前十」应派生出「排名」")
def _t_agg_rank():
    for word in ("门店排名", "最高销售额门店", "前十门店"):
        out = rq._derive_aggregation([{"value": word}])
        assert out, "「%s」必须派生出一条聚合规则" % word
        assert any(r["rule"] == "排名" for r in out), (
            "「%s」应派生出「排名」，实际 rules=%r" % (word, [r["rule"] for r in out])
        )


@case("聚合·反兜底：命中不到任何聚合词必须返回 []，不许编造默认求和")
def _t_agg_no_default():
    # 这是本文件最重要的一条断言。实测踩过：一旦这里返回了
    # [{"rule": "汇总求和", ...}]，下游就会凭空按求和聚合，且全程无报错。
    for value in ("会员手机号", "", None, "会员等级"):
        out = rq._derive_aggregation([{"value": value}])
        assert out == [], (
            "「%r」不含任何聚合词，必须返回 []（不得编造默认规则），实际=%r"
            % (value, out)
        )


@case("聚合：零字段 / 空输入必须返回 []")
def _t_agg_empty():
    assert rq._derive_aggregation([]) == []
    assert rq._derive_aggregation(None) == []


@case("聚合：同一字段命中多个聚合词时不产生重复项")
def _t_agg_dedup():
    # 「合计」与「汇总」都命中同一个字段时，按 (rule, value, word) 去重，
    # 不应因为遍历多个关键词就吐出多条同义规则。
    out = rq._derive_aggregation([{"value": "合计销售额"}])
    keys = [(r["rule"], r["field"], r["matched_keyword"]) for r in out]
    assert len(keys) == len(set(keys)), "派生结果出现重复项：%r" % keys


@case("聚合：多字段各自独立派生")
def _t_agg_multi_field():
    out = rq._derive_aggregation([
        {"value": "合计销售额"},
        {"value": "门店名称"},
        {"value": "平均客单价"},
    ])
    rules = [r["rule"] for r in out]
    assert "汇总求和" in rules and "平均" in rules, (
        "两个字段应分别派生出「汇总求和」与「平均」，实际 rules=%r" % rules
    )
    assert rules.count("汇总求和") == 1, "「汇总求和」不应重复派生：%r" % rules


@case("聚合：派生项三字段齐全（rule/field/matched_keyword）")
def _t_agg_shape():
    out = rq._derive_aggregation([{"value": "合计销售额"}])
    for r in out:
        for k in ("rule", "field", "matched_keyword"):
            assert k in r, "派生项缺字段 %r，实际键=%r" % (k, list(r.keys()))


# ---------------------------------------------------------------------------
# validate：结构化需求出口校验（严格 JSON Schema）
# ---------------------------------------------------------------------------

def _slot(value="门店日汇总", evidence=None, confidence=0.8):
    """槽位必须是三字段结构：值 + 证据 + 置信度。

    这一点是这个契约的**核心设计**，不是形式主义：
    `evidence` 必填意味着**每个槽位都必须说得出"你凭什么这么判"**，
    没有依据的槽位在校验阶段就被拒。这正是"口径可解释"的落地方式，
    所以本文件把它钉成断言——一旦 sub-schema 被放松，需求就能凭空长出
    没有出处的槽位，下游 SQL 会照着编。
    """
    return {
        "value": value,
        "evidence": evidence if evidence is not None else [
            {"level": "P6", "source": "column_docs", "detail": "自证用例构造的证据"}
        ],
        "confidence": confidence,
    }


def _full_obj():
    """按契约造一份结构完整的需求对象（各字段给最小可用形态）。"""
    return {
        "demand_id": "DR-TEST-0001",
        "dataset": "B",
        "version": 1,
        "source_round": 1,
        "schema_version": "v1",
        "subject": _slot(),
        "granularity": _slot("日"),
        "time_semantics": _slot("按 stat_date"),
        "data_scope": _slot("门店营业面积>0"),
        "output_fields": [
            {"field": "store_name", "value": "门店名称", "evidence": _slot()["evidence"],
             "confidence": 0.8}
        ],
        "aggregation_rules": [],
        "confirmed_facts": [],
        "evidence_chain": [],
        "residual_risks": [],
        # status 是中文枚举（契约实测：待审核/已通过/已退回）
        "status": "待审核",
    }


@case("校验：结构完整的正常需求应通过（无 errors）")
def _t_validate_ok():
    res = rq.validate(_full_obj())
    errs = res.get("errors") or []
    assert not errs, "这份结构化需求应当通过校验，实际 errors=%r" % (errs,)


@case("校验：缺 demand_id 必须报错（不能放行无主的需求）")
def _t_validate_need_id():
    obj = _full_obj()
    del obj["demand_id"]
    res = rq.validate(obj)
    errs = res.get("errors") or []
    assert errs, "缺 demand_id 的结构化需求必须被拒，实际未报错"
    assert any("demand_id" in str(e.get("message", "")) for e in errs), (
        "报错信息应点名 demand_id，实际=%r" % (errs,)
    )


@case("校验：每个必填顶层字段缺失都必须被逐个报出（不能只报第一个）")
def _t_validate_each_required():
    missing = []
    for field in rq.REQUIRED_TOP:
        obj = _full_obj()
        del obj[field]
        res = rq.validate(obj)
        errs = res.get("errors") or []
        hit = [e for e in errs if field in str(e.get("message", ""))]
        if not hit:
            missing.append(field)
    assert not missing, (
        "这些必填字段缺失后未被报错，校验器存在漏检：%r（合计 %d 个必填字段）"
        % (missing, len(rq.REQUIRED_TOP))
    )


@case("校验：未声明的额外字段必须被拒（additionalProperties=false）")
def _t_validate_no_extra():
    obj = _full_obj()
    obj["slots"] = {"subject": {}}  # 契约里没有 slots
    res = rq.validate(obj)
    errs = res.get("errors") or []
    assert errs, "契约外的额外字段必须被拒，实际未报错"
    assert any("slots" in str(e) for e in errs), (
        "报错信息应点名多余字段 slots，实际=%r" % (errs,)
    )


@case("校验：返回结构必须含 errors 键，调用方才能无脑判")
def _t_validate_shape():
    res = rq.validate(_full_obj())
    assert "errors" in res, (
        "validate 必须始终返回 errors 键，否则下游 `res.get('errors')` "
        "会把非法需求误判为通过。实际返回键=%r" % list(res.keys())
    )
    assert isinstance(res["errors"], list), "errors 必须是 list，实际=%r" % type(res["errors"])


@case("校验：非法输入不应抛异常（校验器本身不能成为故障源）")
def _t_validate_no_raise():
    for obj in ({}, {"demand_id": None}, {"demand_id": "X"}, "not-a-dict"):
        try:
            res = rq.validate(obj)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                "validate(%r) 抛异常 %s: %s —— 校验器必须吞掉非法输入并返回 errors"
                % (obj, type(e).__name__, e)
            ) from None
        assert isinstance(res, dict) and "errors" in res


@case("契约：REQUIRED_TOP 必须覆盖需求/分析/口径/证据/审计五类字段")
def _t_required_top_covers():
    # 契约是四段链路的载体，少任何一类都会让下游拿不到依据。
    needed = ("demand_id", "schema_version", "aggregation_rules",
              "confirmed_facts", "evidence_chain", "residual_risks", "status")
    lack = [n for n in needed if n not in rq.REQUIRED_TOP]
    assert not lack, (
        "REQUIRED_TOP 缺少关键字段 %r —— 这会让下游 SQL/审计环节拿不到依据" % lack
    )


@case("校验：槽位缺 evidence 必须被拒（不许出现没有出处的判断）")
def _t_validate_slot_needs_evidence():
    for slot_name in ("subject", "granularity", "time_semantics", "data_scope"):
        obj = _full_obj()
        del obj[slot_name]["evidence"]
        res = rq.validate(obj)
        errs = res.get("errors") or []
        assert errs, "%s 槽位缺 evidence 竟然通过了校验——这会让无出处的判断进到SQL 生成" % slot_name
        assert any(slot_name in str(e.get("path", "")) for e in errs), (
            "报错路径应点名 %s，实际=%r" % (slot_name, [e.get("path") for e in errs])
        )


@case("校验：槽位缺 confidence 必须被拒（不确定的判断必须显式标出来）")
def _t_validate_slot_needs_confidence():
    for slot_name in ("subject", "granularity", "time_semantics", "data_scope"):
        obj = _full_obj()
        del obj[slot_name]["confidence"]
        res = rq.validate(obj)
        errs = res.get("errors") or []
        assert errs, "%s 槽位缺 confidence 竟然通过了校验" % slot_name


@case("校验：status 必须是中文枚举（待审核/已通过/已退回）")
def _t_validate_status_enum():
    for bad in ("draft", "DRAFT", "pending", ""):
        obj = _full_obj()
        obj["status"] = bad
        res = rq.validate(obj)
        errs = res.get("errors") or []
        assert errs, "非法 status %r 竟然通过了校验" % bad
    # 合法值必须放过
    for good in ("待审核", "已通过", "已退回"):
        obj = _full_obj()
        obj["status"] = good
        res = rq.validate(obj)
        assert not res.get("errors"), "合法 status %r 却被拒：%r" % (good, res["errors"])


def main():
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print("  [OK] %s" % name)
        except AssertionError as e:
            failed.append((name, str(e)))
            print("  [FAIL] %s\n         %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            failed.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  [ERROR] %s\n         %s: %s" % (name, type(e).__name__, e))
    total = len(CASES)
    print("\nrequirement_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())