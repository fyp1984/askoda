#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 质量基线「测量工具本身」的自证（离线）。

为什么测量工具也要自证
----------------------
一个测量工具最危险的失败方式，不是报错，而是**安静地给出好看的数**：

  · 题集字段缺失（比如某题没写 must_have）⇒ 那题永远算"要素齐备"；
  · 匹配函数大小写/空白处理不一致 ⇒ 同一份 SQL 时对时错；
  · 某题在基线里是 true、这次根本没跑 ⇒ 逐题比对发现不了（"没跑"看起来像"没坏"）；
  · 回退判定写成 `>` 而不是 `<` ⇒ **永久绿**，回退全部漏掉。

这四种都不会抛异常，都会让"质量基线"变成第二个"门禁天天绿但什么都没查"。
所以这里用**人造样例**把评分器的行为钉死：该判过的过、该判不过的**必须判不过**。

⚠️ 本文件只用构造数据，不读真实题集、不依赖数据库
  —— 判据要能独立于被测量的对象运行，否则"题集改了所以判据也改了"就永远绿。

用法：
    python3 tools/sql_quality_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sql_quality_baseline as qb  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 匹配与归一化
# ---------------------------------------------------------------------------

@case("归一化：大小写与空白必须被抹平（否则同一份 SQL 时对时错）")
def _t_norm():
    a = qb._norm("SELECT  SUM(x)\n  AS 销售额")
    b = qb._norm("select sum(x) as 销售额")
    assert a == b, "归一化没抹平大小写/空白：%r vs %r" % (a, b)
    assert qb._norm(None) == "", "None 应归一成空串（不能抛异常）"


@case("归一化：中文与标点原样保留（抹过头会把不同列判成同一个）")
def _t_norm_cn():
    assert qb._norm("占比百分比") != qb._norm("销售额"), "两个不同别名被归一成了同一个"
    assert qb._norm("date_trunc('month', x)") == "date_trunc('month',x)"


# ---------------------------------------------------------------------------
# 单题评分：该过的过
# ---------------------------------------------------------------------------

def _fake_plan(sql="", intent="", reason=""):
    def _fn(ds, text):
        return {"sql": sql, "intent": intent or ("意图" if sql else "拒绝"),
                "reason": reason}
    return _fn


def _fake_review(status="通过", failed=None):
    layers = [{"layer": "L1", "ok": True, "skipped": False}]
    for name in (failed or []):
        layers.append({"layer": name, "ok": False, "skipped": False})
    # 一期未启用的层：ok=False 但 skipped=True —— 不能被算成失败层
    layers.append({"layer": "L-SQLFluff", "ok": False, "skipped": True})
    return lambda sql, dataset="B": {"status": status, "layers": layers}


@case("评分：要素齐 + 无陷阱 + 门禁过 ⇒ 达标")
def _t_pass():
    c = {"id": "X1", "dataset": "B", "text": "t", "kind": "sql",
         "must_have": ["pay_amount", "GROUP  BY"], "must_not_have": ["dw_insert_time"]}
    r = qb.evaluate_case(
        c, None, _fake_plan("SELECT SUM(pay_amount) FROM t GROUP BY x"),
        _fake_review())
    assert r["ok"] is True, r["why"]
    assert r["missing"] == [] and r["traps"] == [], r


# ---------------------------------------------------------------------------
# 单题评分：必须判不过的五种情形（这才是关键）
# ---------------------------------------------------------------------------

@case("评分：缺要素 ⇒ 不达标（「说了没做」必须被发现）")
def _t_missing():
    c = {"id": "X2", "dataset": "B", "text": "t", "kind": "sql",
         "must_have": ["pay_amount", "count("], "must_not_have": []}
    r = qb.evaluate_case(
        c, None, _fake_plan("SELECT SUM(pay_amount) FROM t"), _fake_review())
    assert r["ok"] is False, "缺 count( 却判达标 —— 要素判定失效"
    assert "count(" in r["missing"], r["missing"]


@case("评分：踩陷阱 ⇒ 不达标（装载列必须被抓）")
def _t_trap():
    c = {"id": "X3", "dataset": "B", "text": "t", "kind": "sql",
         "must_have": [], "must_not_have": ["dw_insert_time"]}
    r = qb.evaluate_case(
        c, None, _fake_plan("SELECT x FROM t WHERE dw_insert_time > '2026-01-01'"),
        _fake_review())
    assert r["ok"] is False, "用了 dw_insert_time 却判达标 —— 陷阱判定失效"
    assert r["traps"] == ["dw_insert_time"], r["traps"]


@case("评分：门禁不过 ⇒ 不达标，且失败层名要点出来")
def _t_gate_fail():
    c = {"id": "X4", "dataset": "B", "text": "t", "kind": "sql",
         "must_have": [], "must_not_have": []}
    r = qb.evaluate_case(
        c, None, _fake_plan("SELECT 1"), _fake_review("不通过", ["L2"]))
    assert r["ok"] is False, "门禁不通过却判达标"
    assert "L2" in r["gate_layers_failed"], r["gate_layers_failed"]
    assert "L-SQLFluff" not in r["gate_layers_failed"], (
        "把「一期未启用（skipped）」算成了失败层 ⇒ 报告会指向不存在的问题")


@case("评分：期望拒绝却生成了 SQL ⇒ 不达标（替业务方猜口径是最危险的）")
def _t_refuse_but_generated():
    c = {"id": "X5", "dataset": "B", "text": "t", "kind": "refuse",
         "refuse_reason": [["敏感"]]}
    r = qb.evaluate_case(c, None, _fake_plan("SELECT phone FROM m"), _fake_review())
    assert r["ok"] is False, "期望拒绝却生成了 SQL，仍被判达标"


@case("评分：拒绝理由对不上 ⇒ 不达标（拒得不明不白等于没拒）")
def _t_refuse_wrong_reason():
    c = {"id": "X6", "dataset": "B", "text": "t", "kind": "refuse",
         "refuse_reason": [["敏感"], ["手机号"]]}
    r = qb.evaluate_case(
        c, None, _fake_plan("", intent="未命中已建模意图", reason="口径未建模"),
        _fake_review())
    assert r["ok"] is False, "理由里既没有「敏感」也没有「手机号」，却判达标"


@case("评分：拒绝理由命中任一组即通过（不被措辞绑死）")
def _t_refuse_reason_ok():
    c = {"id": "X7", "dataset": "B", "text": "t", "kind": "refuse",
         "refuse_reason": [["敏感"], ["手机号"]]}
    r = qb.evaluate_case(
        c, None, _fake_plan("", intent="敏感字段拦截：手机号",
                            reason="会员手机号为敏感字段"),
        _fake_review())
    assert r["ok"] is True, r["why"]


@case("评分：规划器抛异常 ⇒ 不达标且原因可见（不静默当通过）")
def _t_plan_raises():
    def _boom(ds, text):
        raise ValueError("故意炸")
    c = {"id": "X8", "dataset": "B", "text": "t", "kind": "sql",
         "must_have": [], "must_not_have": []}
    r = qb.evaluate_case(c, None, _boom, _fake_review())
    assert r["ok"] is False, "规划器抛异常却判达标"
    assert "ValueError" in r["why"], r["why"]


# ---------------------------------------------------------------------------
# 汇总口径
# ---------------------------------------------------------------------------

def _recs():
    return [
        {"id": "a", "kind": "sql", "generated": True, "gate": "通过",
         "missing": [], "traps": [], "ok": True},
        {"id": "b", "kind": "sql", "generated": True, "gate": "通过",
         "missing": ["x"], "traps": [], "ok": False},
        {"id": "c", "kind": "sql", "generated": False, "gate": "",
         "missing": [], "traps": [], "ok": False},
        {"id": "d", "kind": "refuse", "generated": False, "gate": "",
         "missing": [], "traps": [], "ok": True},
    ]


@case("汇总：分母必须是「实际出数」而不是「全部题」（否则拒绝会拉低要素率）")
def _t_summary_denominator():
    m = qb.summarize(_recs())
    assert m["sql_cases"] == 3 and m["refuse_cases"] == 1, m
    assert m["generated"] == 2, m
    # ⚠️ 容差按 1e-3：summarize() 把比率四舍五入到 4 位小数（0.6667），
    #   用 1e-6 会把"四舍五入"误判成"算错了"。
    assert abs(m["generated_rate"] - 2.0 / 3.0) < 1e-3, m["generated_rate"]
    # 门禁/要素/陷阱的分母 = 真的出数的题（2 题），不是 3 题
    assert m["gate_pass_rate"] == 1.0, m["gate_pass_rate"]
    assert abs(m["element_rate"] - 0.5) < 1e-3, m["element_rate"]
    assert m["refuse_rate"] == 1.0, m["refuse_rate"]
    assert abs(m["overall_rate"] - 0.5) < 1e-3, m["overall_rate"]


@case("汇总：零题时率为 0 而不是崩（且调用方必须自己拦住空题集）")
def _t_summary_empty():
    m = qb.summarize([])
    assert m["total"] == 0, m
    for key, _label in qb.METRICS:
        assert m[key] == 0.0, "%s 在空集上应为 0.0，实际 %r" % (key, m[key])


# ---------------------------------------------------------------------------
# 回退判定（方向写反 = 永久绿）
# ---------------------------------------------------------------------------
def _full_metrics(**over):
    """造一份**指标齐全**的 metrics。

    ⚠️ 必须齐全：`compare()` 对「当前缺某个指标」的处理是**当作 0 = 回退**
    （缺指标说明这次根本没算出来，不能当没变化）。所以拿半份 metrics 去测，
    测的其实是"缺指标"这条分支，而不是想测的"分数涨跌"。
    """
    m = {k: 1.0 for k, _l in qb.METRICS}
    m.update({"total": 10, "sql_cases": 8, "refuse_cases": 2, "generated": 8})
    m.update(over)
    return m


@case("回退：分数下降必须判回退（把 `<` 写成 `>` 就永久绿）")
def _t_compare_drop():
    base = {"metrics": _full_metrics(overall_rate=0.9, element_rate=0.9)}
    cur = _full_metrics(overall_rate=0.8, element_rate=0.9)
    has_drop, drops = qb.compare(cur, base)
    assert has_drop is True, "指标下降却判为「无回退」"
    assert [d[0] for d in drops] == ["overall_rate"], drops


@case("回退：持平或上升不算回退（否则永远红）")
def _t_compare_ok():
    for cur_v in (0.9, 0.95, 1.0):
        has_drop, drops = qb.compare(_full_metrics(overall_rate=cur_v),
                                     {"metrics": _full_metrics(overall_rate=0.9)})
        assert has_drop is False, "当前 %r 不该判回退：%r" % (cur_v, drops)


@case("回退：基线里没有的指标不参与判定（新加指标不该一上线就红）")
def _t_compare_new_metric():
    # 基线侧**故意缺** trap_rate（模拟"这个指标是后来才加的"）⇒ 跳过，不该红
    base = {"metrics": {k: 1.0 for k, _l in qb.METRICS if k != "trap_rate"}}
    has_drop, drops = qb.compare(_full_metrics(trap_rate=1.0), base)
    assert has_drop is False, "基线里没有的指标被拿去比了：%r" % (drops,)


@case("回退：当前缺某个指标 ⇒ 判回退（没算出来不等于没变化）")
def _t_compare_missing_metric():
    cur = _full_metrics()
    cur.pop("trap_rate")
    has_drop, drops = qb.compare(cur, {"metrics": _full_metrics(trap_rate=1.0)})
    assert has_drop is True, "指标整个缺失却判为「无回退」—— 会让崩溃看起来像没变化"
    assert [d[0] for d in drops] == ["trap_rate"], drops


@case("回退：逐题比对必须抓到「基线里过、这次不过」（总分会被此长彼消掩盖）")
def _t_case_regression():
    base = {"cases": {"a": True, "b": True}}
    recs = [
        {"id": "a", "ok": True, "text": "", "why": ""},
        {"id": "b", "ok": False, "text": "", "why": "缺要素"},
    ]
    bad = qb.case_regressions(recs, base)
    assert [r["id"] for r in bad] == ["b"], bad


@case("回退：基线里是 false、这次仍 false 不算回退（已知差距不是新回退）")
def _t_known_gap_not_regression():
    base = {"cases": {"b": False}}
    bad = qb.case_regressions([{"id": "b", "ok": False, "text": "", "why": ""}], base)
    assert bad == [], bad


@case("回退：基线里是 false、这次 true 记为改善")
def _t_improvement():
    base = {"cases": {"b": False}}
    good = qb.new_passes([{"id": "b", "ok": True, "text": "", "why": ""}], base)
    assert [r["id"] for r in good] == ["b"], good


# ---------------------------------------------------------------------------
# 题集与基线文件的完整性
# ---------------------------------------------------------------------------

@case("题集：每题必须有 id / dataset / text / kind，且 id 唯一")
def _t_corpus_shape():
    with open(qb.CORPUS_PATH, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    cases = corpus.get("cases") or []
    assert cases, "题集为空 —— 空题集跑出来的是「0/0 = 通过」，那是假绿"
    ids = []
    for c in cases:
        for k in ("id", "dataset", "text", "kind"):
            assert c.get(k), "题 %r 缺字段 %s" % (c.get("id"), k)
        assert c["kind"] in ("sql", "refuse"), "题 %s 的 kind 非法：%r" % (c["id"], c["kind"])
        assert c["dataset"] in ("A", "B"), "题 %s 的 dataset 非法：%r" % (c["id"], c["dataset"])
        ids.append(c["id"])
    assert len(ids) == len(set(ids)), "id 有重复：%r" % ids


@case("题集：kind=sql 必须有 must_have（否则那题永远算「要素齐备」）")
def _t_corpus_must_have():
    with open(qb.CORPUS_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    bad = [c["id"] for c in cases if c["kind"] == "sql" and not (c.get("must_have") or [])]
    assert not bad, (
        "这些题没写 must_have ⇒ 它们的「要素齐备」是白送的：%r。\n"
        "        要么补上「正确 SQL 必须出现的东西」，要么把 kind 改成 refuse。" % bad)


@case("题集：kind=refuse 必须有 refuse_reason（否则拒得对不对没法判）")
def _t_corpus_refuse_reason():
    with open(qb.CORPUS_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    bad = [c["id"] for c in cases
           if c["kind"] == "refuse" and not (c.get("refuse_reason") or [])]
    assert not bad, "这些题没写 refuse_reason ⇒ 只要不出数就算过：%r" % bad


@case("题集：每题都要写 note（说清「为什么业务上该这样」）")
def _t_corpus_note():
    with open(qb.CORPUS_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    bad = [c["id"] for c in cases if not (c.get("note") or "").strip()]
    assert not bad, "这些题没写 note，将来没人知道期望值凭什么这么定：%r" % bad


@case("基线文件：存在、可解析、含全部题号（缺题号 ⇒ 那题的回退永远发现不了）")
def _t_baseline_shape():
    assert os.path.isfile(qb.BASELINE_PATH), (
        "基线文件不存在：%s\n"
        "        ⇒ 先跑 `python3 tools/sql_quality_baseline.py` 看分数，确认后 `--update` 写入。" %
        qb.BASELINE_PATH)
    with open(qb.BASELINE_PATH, "r", encoding="utf-8") as f:
        base = json.load(f)
    with open(qb.CORPUS_PATH, "r", encoding="utf-8") as f:
        ids = [c["id"] for c in json.load(f)["cases"]]
    missing = [i for i in ids if i not in (base.get("cases") or {})]
    assert not missing, "基线里缺这些题号（它们的回退将无法被发现）：%r" % missing
    extra = [i for i in (base.get("cases") or {}) if i not in ids]
    assert not extra, "基线里有题集里已不存在的题号（陈旧基线）：%r" % extra
    for key, _label in qb.METRICS:
        assert key in (base.get("metrics") or {}), "基线缺指标 %s" % key


@case("基线文件：known-false 的题必须是少数（大面积 false = 题集或实现出了大问题）")
def _t_baseline_known_gaps():
    with open(qb.BASELINE_PATH, "r", encoding="utf-8") as f:
        base = json.load(f)
    cases = base.get("cases") or {}
    if not cases:
        return
    bad = [i for i, ok in cases.items() if ok is False]
    ratio = float(len(bad)) / len(cases)
    assert ratio <= 0.2, (
        "基线里 %.0f%% 的题不过（%r）⇒ 不是「已知差距」的量级了。\n"
        "        要么是题集期望写高了，要么是实现退化了 —— 都得先查清再继续。" % (ratio * 100, bad))


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
    print("\nsql_quality_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
