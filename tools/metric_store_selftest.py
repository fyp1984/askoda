#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径存取层（gateway/metric_store.py + metric_spec.py）离线自证。

测什么 / 不测什么
-----------------
只测**离线可判定的逻辑**，不连库、不连引擎：
  · 草稿规整：去空白、别名中英文逗号都能切、重复项只留一个；
  · 保存前校验：维度不在视图列、time_mode 乱写、缺别名、视图引用不存在的对象；
  · **部署失败不落库**（本次最要紧的一条：否则会出现「库里新口径、引擎还是旧的」）；
  · 部署成功才 UPDATE，且顺序是先部署后落库；
  · dry_run 只校验、不写库；
  · 种子孤儿：文件里写了、生效口径里没有，必须报出来；
  · 版本号自增与兜底。

**不测**：真实 deploy_manifest、真实 PostgreSQL（由 G2/G3 覆盖）。

用法：python3 tools/metric_store_selftest.py   （退出码 0=全过，1=有失败）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "gateway"))

import metric_spec  # noqa: E402
import metric_store  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


MDL = {
    "models": [
        {"name": "acct_loan", "columns": [{"name": "dblbal"}]},
        {"name": "business_duebill", "columns": [{"name": "putout_amt"}]},
    ]
}

SECTION = {
    "dataset": "C",
    "version": "v1",
    "views": [
        {"name": "mt_loan_snap",
         "statement": "SELECT a.dblbal, a.stat_month, a.product_name FROM acct_loan a",
         "columns": ["dblbal", "stat_month", "product_name"],
         "comment": "贷款快照"},
    ],
    "metrics": [
        {"key": "loan_bal", "label": "贷款余额", "aliases": ["余额"], "view": "mt_loan_snap",
         "expr": "SUM(dblbal)", "unit": "元", "fmt": "money", "time_mode": "snapshot",
         "time_col": "stat_month", "dims": ["product_name"]},
    ],
}


def _mdl_with_section(section=SECTION):
    mdl = json.loads(json.dumps(MDL))
    mdl["views"] = [{"name": v["name"], "statement": v["statement"]} for v in section["views"]]
    mdl["x_askoda_metrics"] = {
        "dataset": section["dataset"],
        "version": section["version"],
        "views": {v["name"]: {"columns": v["columns"], "comment": v.get("comment", "")}
                  for v in section["views"]},
        "metrics": section["metrics"],
    }
    return mdl


class FakeDB:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql.strip().split()[0].upper(), params))
        return 1


class FakeStore:
    def __init__(self, mdl=None):
        self.mdl = mdl if mdl is not None else _mdl_with_section()
        self.raw = {"dataset_key": "C", "wren_url": "http://engine:9000", "mdl_json": self.mdl,
                    "updated_by": "tester", "updated_at": None}

    def raw_row(self, key):
        return dict(self.raw) if key == "C" else None


class Patch:
    """把 metric_store 里的 db / dataset_store / _deploy 换成桩。"""

    def __init__(self, mdl=None, deploy=None):
        self.store = FakeStore(mdl)
        self.db = FakeDB()
        self.deploy = deploy or (lambda row, m: "ok")
        self.deployed = []

    def __enter__(self):
        self._db = metric_store.db
        self._ds = metric_store.dataset_store
        self._dp = metric_store._deploy
        metric_store.db = self.db
        metric_store.dataset_store = self.store

        def fake_deploy(row, m):
            self.deployed.append(m)
            out = self.deploy(row, m)
            return out

        metric_store._deploy = fake_deploy
        return self

    def __exit__(self, *a):
        metric_store.db = self._db
        metric_store.dataset_store = self._ds
        metric_store._deploy = self._dp
        return False

    def writes(self):
        return [c for c in self.db.calls if c[0] in ("UPDATE", "INSERT")]


# ---------------------------------------------------------------------------
# 规整与校验
# ---------------------------------------------------------------------------
@case("草稿规整：空白、中英文逗号、重复别名")
def _t_normalize():
    out = metric_spec.normalize({
        "metrics": [{
            "key": "  loan_bal  ", "label": " 贷款余额 ", "aliases": "余额，贷款余额、余额",
            "view": "mt_loan_snap", "expr": " SUM(dblbal) ", "time_mode": "snapshot",
            "time_col": "stat_month", "dims": ["product_name", "product_name"],
        }],
        "views": SECTION["views"],
    }, "C", SECTION)
    m = out["metrics"][0]
    assert m["key"] == "loan_bal" and m["label"] == "贷款余额", m
    assert m["aliases"] == ["余额", "贷款余额"], m["aliases"]
    assert m["dims"] == ["product_name"], m["dims"]
    assert m["expr"] == "SUM(dblbal)", m["expr"]
    assert m["fmt"] == "num", "没填 fmt 应给默认值"


@case("草稿规整：没有 key 的条目直接丢掉，不制造半条口径")
def _t_normalize_drop():
    out = metric_spec.normalize({"metrics": [{"label": "没 key"}, {"key": "k", "aliases": ["a"]}]}, "C")
    assert [m["key"] for m in out["metrics"]] == ["k"], out["metrics"]


@case("校验：维度不在视图列、time_mode 乱写、缺别名、视图引用不存在的对象")
def _t_static_check_catches():
    bad = json.loads(json.dumps(SECTION))
    bad["views"][0]["statement"] = "SELECT 1 FROM not_exists_table"
    bad["metrics"][0]["dims"] = ["no_such_col"]
    bad["metrics"][0]["time_mode"] = "whenever"
    bad["metrics"][0]["aliases"] = []
    bad["metrics"][0]["time_col"] = "no_such_time"
    problems = " | ".join(metric_spec.static_check(bad, {"models": MDL["models"]}))
    for kw in ("没有的对象", "维度", "time_mode", "aliases", "time_col"):
        assert kw in problems, "漏判：%s → %s" % (kw, problems)


@case("校验：相对「引擎已装载的那份 MDL」判，不拿别的库的模型顶替")
def _t_static_check_models():
    ok = metric_spec.static_check(SECTION, {"models": MDL["models"]})
    assert ok == [], ok
    # 把模型换成不含 acct_loan 的库 ⇒ 视图引用立刻判红（换库后口径不可能照搬）
    other = {"models": [{"name": "sales", "columns": []}]}
    assert metric_spec.static_check(SECTION, other), "换了库还判绿，说明没真去比对模型"


@case("种子孤儿：文件里有、生效口径里没有，必须报出来")
def _t_seed_orphans():
    seed = {"metrics": SECTION["metrics"] + [{"key": "npl_bal", "aliases": ["不良余额"]}]}
    assert metric_spec.seed_orphans(seed, SECTION) == ["npl_bal"]
    assert metric_spec.seed_orphans({"metrics": SECTION["metrics"]}, SECTION) == []


@case("版本号：v3 → v4；整数 1 → 2；认不出就用时间戳，绝不退化成空串")
def _t_version():
    assert metric_store._next_version("v3") == "v4"
    # 种子文件里 version 是数字 1，直接 .strip() 会崩成「校验未通过但没有原因」
    assert metric_store._next_version(1) == "2", metric_store._next_version(1)
    assert metric_store._next_version(None).startswith("v"), "None 也得给出版本号"
    v = metric_store._next_version("口径第一版")
    assert v.startswith("v") and len(v) > 10, v


# ---------------------------------------------------------------------------
# 保存链路
# ---------------------------------------------------------------------------
@case("保存：部署失败 ⇒ 一个字都不落库（否则库里新、引擎旧，最难查）")
def _t_save_deploy_fail():
    def boom(row, mdl):
        raise RuntimeError("engine down")

    with Patch(deploy=boom) as p:
        r = metric_store.save("C", SECTION, actor="tester")
        assert r["ok"] is False and r["code"] == "METRIC_DEPLOY_FAILED", r
        assert p.writes() == [], "引擎没装载却写了库：%s" % p.writes()
        assert "旧口径仍然可用" in (r.get("notice") or ""), r.get("notice")


@case("保存：dry_run 只校验，不部署不落库")
def _t_save_dry_run():
    with Patch() as p:
        r = metric_store.save("C", SECTION, actor="tester", dry_run=True)
        assert r["ok"] is True and r["dry_run"] is True, r
        assert p.deployed == [], "试校验不该部署引擎"
        assert p.writes() == [], "试校验不该写库"


@case("保存：校验不过 ⇒ 逐条原因返回，不部署不落库")
def _t_save_invalid():
    bad = json.loads(json.dumps(SECTION))
    bad["metrics"][0]["view"] = "no_such_view"
    with Patch() as p:
        r = metric_store.save("C", bad, actor="tester")
        assert r["ok"] is False and r["code"] == "METRIC_INVALID", r
        assert r["problems"], "没给原因，用户不知道改哪"
        assert p.deployed == [] and p.writes() == [], "校验没过还往下走了"


@case("保存：成功时先部署再落库，并把变更记进 config_events")
def _t_save_ok():
    draft = json.loads(json.dumps(SECTION))
    draft["metrics"][0]["aliases"] = ["余额", "贷款规模"]  # 真改一点，才该升版
    with Patch() as p:
        r = metric_store.save("C", draft, actor="tester")
        assert r["ok"] is True, r
        assert len(p.deployed) == 1, "没有部署到引擎"
        kinds = [c[0] for c in p.db.calls]
        assert kinds and kinds[0] == "UPDATE", "落库必须是第一步：%s" % kinds
        assert any(c[0] == "INSERT" for c in p.db.calls), "没记变更留痕"
        assert r["version"] == "v2", "口径改了版本号却没动：%s" % r["version"]


@case("保存：内容没变 ⇒ 不升版（版本号不能被每次点保存刷成噪音）")
def _t_save_unchanged():
    with Patch() as p:
        r = metric_store.save("C", SECTION, actor="tester")
        assert r["ok"] is True, r
        assert r["version"] == "v1", "内容没变却升版了：%s" % r["version"]
        assert "没有变化" in (r.get("notice") or ""), r.get("notice")


@case("保存：视图语句可以从草稿里省掉（沿用库里同名视图），不会把语句清空")
def _t_save_keeps_statement():
    draft = json.loads(json.dumps(SECTION))
    for v in draft["views"]:
        v.pop("statement", None)
    with Patch() as p:
        r = metric_store.save("C", draft, actor="tester")
        assert r["ok"] is True, r
        sent = p.deployed[0]
        stmt = (sent.get("views") or [{}])[0].get("statement")
        assert stmt and "acct_loan" in stmt, "视图语句被清空了：%r" % stmt


@case("保存：写不回引擎启动文件时必须出声（否则引擎重启静默回滚）")
def _t_save_persist_warning():
    old = os.environ.pop("ASKODA_ENGINE_WORKSPACE_C", None)
    try:
        draft = json.loads(json.dumps(SECTION))
        draft["metrics"][0]["label"] = "贷款余额（改）"
        with Patch() as p:
            r = metric_store.save("C", draft, actor="tester")
            assert r["ok"] is True, r
            assert r["persist_warning"], "没写回文件却不说，等于埋雷"
            assert "重启" in (r.get("notice") or ""), r.get("notice")
    finally:
        if old is not None:
            os.environ["ASKODA_ENGINE_WORKSPACE_C"] = old


@case("读取：未登记的数据集给明确原因，不返回空壳")
def _t_load_unknown():
    with Patch():
        try:
            metric_store.load("Z")
        except metric_store.MetricError as e:
            assert "未登记" in str(e), e
        else:
            raise AssertionError("未登记的数据集竟然读成功了")


@case("读取：返回的视图带语句，界面据此展示「这条口径基于什么」")
def _t_load_has_statement():
    with Patch():
        r = metric_store.load("C")
        assert r["ok"] is True, r
        assert r["views"][0].get("statement"), "视图语句丢了，界面看不到依据"
        assert r["metrics"][0]["key"] == "loan_bal"


def main():
    bad = 0
    for name, fn in CASES:
        try:
            fn()
            print("  [OK] %s" % name)
        except AssertionError as e:
            bad += 1
            print("  [FAIL] %s\n         %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("  [ERR ] %s\n         %s: %s" % (name, type(e).__name__, e))
    total = len(CASES)
    print("\nmetric_store_selftest: %d/%d 通过" % (total - bad, total))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
