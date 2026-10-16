#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MDL 启动自检自证（离线，不需要网关、不需要数据库）。

为什么值得单独立一份
--------------------
2026-10-15 实测到的真实故障：容器内 `/workspace-a/mdl.json`、`/workspace-b/mdl.json`
偶发不可读（`PermissionError: [Errno 1] Operation not permitted`，**以 root 也一样**，
Docker 守护进程重启后恢复）。它之所以危险不是「会挂」，而是**挂了不报**：
`rules.evaluate_all()` 把异常吞成一条 `RULE_INTERNAL_ERROR` 提示后继续，
`sql_review` 照样返回「警告」——L2 静态规则整层失效而调用方毫不知情。

`gateway/mdl_check.py` 的职责就是把这种沉默变成显式拒绝启动。
本文件把它的判据钉成断言防回归，分两层：

- **判据层**（注入假 index_builder，不碰真实规则）：空路径 / 文件不存在 / 非法 JSON /
  空内容 / 路径是目录 / 索引构建抛异常 / 多数据集部分失败 / 渲染文案。
- **同源层**（用仓库里真实的 A、B 两份 MDL 走真实 `rules.build_mdl_index`）：
  验证「自检判据」与「L2 规则运行时实际读的那份文件」确实是同一份。

用法：
    python3 tools/mdl_check_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

import mdl_check  # noqa: E402

# 仓库里真实的两份 MDL（与容器内 MDL_A_PATH / MDL_B_PATH 同一份文件的仓库副本）
REAL_MDL = {
    "A": os.path.join(_ROOT, "wren-docker", "workspace", "mdl.json"),
    "B": os.path.join(_ROOT, "wren-docker-b", "workspace", "mdl.json"),
}

RESULTS = []


def case(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def _ok_builder(key, path):
    return {"models": {"m1": {}, "m2": {}}}


def _boom_builder(key, path):
    raise PermissionError(1, "Operation not permitted: %s" % path)


def _write(tmpdir, name, content):
    p = os.path.join(tmpdir, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


# ── 判据层 ────────────────────────────────────────────────────────────────
@case("空路径 → 失败并说明是环境变量未配置")
def t_empty_path(_tmp):
    r = mdl_check.selfcheck([("A", "")], index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert "环境变量" in r["failures"][0]["error"], r["failures"][0]
    return "命中「MDL 路径为空」"


@case("文件不存在 → 失败")
def t_missing(_tmp):
    r = mdl_check.selfcheck([("A", os.path.join(_tmp, "nope.json"))], index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert "不存在" in r["failures"][0]["error"], r["failures"][0]
    return "命中「文件不存在」"


@case("非法 JSON → 失败（不是合法 JSON）")
def t_bad_json(_tmp):
    p = _write(_tmp, "bad.json", "{ this is not json ")
    r = mdl_check.selfcheck([("A", p)], index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert "JSON" in r["failures"][0]["error"], r["failures"][0]
    return "命中「不可读或不是合法 JSON」"


@case("空对象 {} → 失败（结构异常）")
def t_empty_obj(_tmp):
    p = _write(_tmp, "empty.json", "{}")
    r = mdl_check.selfcheck([("A", p)], index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert "结构异常" in r["failures"][0]["error"], r["failures"][0]
    return "命中「内容为空或结构异常」"


@case("JSON 顶层是数组 → 失败")
def t_array(_tmp):
    p = _write(_tmp, "arr.json", "[1, 2, 3]")
    r = mdl_check.selfcheck([("A", p)], index_builder=_ok_builder)
    assert r["ok"] is False, r
    return "顶层非 dict → 失败"


@case("路径是目录 → 失败（IsADirectoryError 被转成结论）")
def t_dir(_tmp):
    d = os.path.join(_tmp, "adir")
    os.makedirs(d, exist_ok=True)
    r = mdl_check.selfcheck([("A", d)], index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert r["failures"][0]["error"], r
    return "异常被转成结论，未抛出"


@case("★ 文件可读但规则索引构建失败 → 失败（这正是静默降级的入口）")
def t_index_boom(_tmp):
    p = _write(_tmp, "ok.json", json.dumps({"models": [], "relationships": []}))
    r = mdl_check.selfcheck([("A", p)], index_builder=_boom_builder)
    assert r["ok"] is False, r
    err = r["failures"][0]["error"]
    assert "规则索引构建失败" in err and "PermissionError" in err, err
    return "PermissionError 被如实上报，未被吞"


@case("合法 MDL → 通过，且回报模型数")
def t_ok(_tmp):
    p = _write(_tmp, "ok.json", json.dumps({"models": [{"name": "t"}], "relationships": []}))
    r = mdl_check.selfcheck([("A", p)], index_builder=_ok_builder)
    assert r["ok"] is True, r
    assert r["items"][0]["models"] == 2, r["items"][0]
    return "models=2"


@case("多数据集部分失败 → 整体判红，且只报失败那个")
def t_partial(_tmp):
    good = _write(_tmp, "g.json", json.dumps({"models": []}))
    r = mdl_check.selfcheck([("A", good), ("B", os.path.join(_tmp, "gone.json"))],
                            index_builder=_ok_builder)
    assert r["ok"] is False, r
    assert r["checked"] == 2, r
    assert len(r["failures"]) == 1 and r["failures"][0]["key"] == "B", r["failures"]
    return "2 查 1 坏 → 判红且定位到 B"


@case("渲染文案：判红时必须写明「拒绝启动」与排查线索")
def t_render(_tmp):
    p = _write(_tmp, "ok.json", json.dumps({"models": []}))
    bad = mdl_check.selfcheck([("A", p), ("B", "/no/such.json")], index_builder=_ok_builder)
    txt = mdl_check.render(bad)
    for kw in ("拒绝启动", "MDL 自检失败", "数据集 B", "L2 静态规则"):
        assert kw in txt, (kw, txt)
    ok = mdl_check.selfcheck([("A", p)], index_builder=_ok_builder)
    assert "自检通过" in mdl_check.render(ok), mdl_check.render(ok)
    return "红绿两种文案均可用"


# ── 同源层：用仓库真实 MDL 走真实 rules.build_mdl_index ───────────────────
@case("★ 仓库真实 A/B 两份 MDL 走真实规则索引 → 全部通过")
def t_real_mdl(_tmp):
    import registry
    import rules

    missing = [k for k, p in REAL_MDL.items() if not os.path.exists(p)]
    assert not missing, "仓库缺少 MDL：%s" % missing

    # 把 registry 指向仓库副本（生产是镜像内 /app/mdl/*.json，同一份内容）
    saved = {k: ds.mdl_path for k, ds in registry.DATASETS.items()}
    try:
        for k, p in REAL_MDL.items():
            if k in registry.DATASETS:
                registry.DATASETS[k].mdl_path = p
        r = mdl_check.selfcheck()  # 不传 datasets → 走 registry 全量，且用真实 builder
        assert r["ok"] is True, r
        got = {i["key"]: i.get("models") for i in r["items"]}
        assert got.get("A") and got.get("B"), got
        # 反向确认：指向不存在的路径时，同一个自检必须判红
        for k in registry.DATASETS:
            registry.DATASETS[k].mdl_path = "/no/such/mdl.json"
        r2 = mdl_check.selfcheck()
        assert r2["ok"] is False, r2
    finally:
        for k, p in saved.items():
            registry.DATASETS[k].mdl_path = p
    return "真实 MDL：A=%s 模型 / B=%s 模型；坏路径如期判红" % (got.get("A"), got.get("B"))


def main():
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in RESULTS:
            try:
                note = fn(tmp)
                print("  [OK] %s%s" % (name, ("  → " + note) if note else ""))
            except AssertionError as e:
                failed += 1
                print("  [FAIL] %s\n         %s" % (name, e))
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("  [ERR] %s\n         %s: %s" % (name, type(e).__name__, e))
    total = len(RESULTS)
    print("=" * 60)
    print("MDL 自检自证：%s（%d/%d 通过）" % ("全部通过" if not failed else "存在失败", total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
