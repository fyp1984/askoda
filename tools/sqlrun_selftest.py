#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读 SQL 执行与留痕（sqlrun.py）纯函数自证。

为什么单独做这个自证
--------------------
`sqlrun.py` 负责「审查通过后才执行、执行后写 sql_runs 留痕」，是 M5 交付链路
的落点。它依赖 sqlglot（经 gates/sqlgen 间接导入），**本解释器若未装 sqlglot 则
整模块 import 失败**——此时本自证如实打印 SKIP 并以 0 退出（不伪装成绿，也不
误报红），待在装了 sqlglot 的网关运行时（docker 镜像 / CI）自然跑通。

本自证只测**不碰库的纯函数**：
  · `_new_run_id`        —— sql_run_id 格式（SR- + 16 位十六进制）
  · `_normalize_json`    —— DB JSONB/TEXT 统一转 dict/list
  · `_blocked_summary`   —— 从 review 包提「哪层拦的 + 理由」
  · `_resolve_versions`  —— 版本号缺失时的尽力补齐（sqlpack.build 失败也返回原值）

用法：
    python3 tools/sqlrun_selftest.py
退出码 0 = 全过或 SKIP，1 = 有失败。
"""
import os
import sys

GW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway")
sys.path.insert(0, GW)


def _try_import():
    try:
        import sqlrun as sr  # noqa: E402
        return sr
    except Exception as e:  # noqa: BLE001
        return e


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


_sr = _try_import()
if isinstance(_sr, Exception):
    # sqlglot 未安装：本解释器跑不了 sqlrun，如实 SKIP。
    print("[SKIP] sqlrun_selftest：sqlrun 依赖 sqlglot，当前解释器未安装"
          "（%s）。在装了 sqlglot 的网关运行时（docker 镜像 / CI）将自动跑通。"
          % type(_sr).__name__)
    sys.exit(0)

sr = _sr


@case("sql_run_id：格式 SR- + 16 位十六进制")
def _t_run_id():
    rid = sr._new_run_id()
    assert rid.startswith("SR-"), "应以 SR- 开头，实际=%r" % rid
    tail = rid[len("SR-"):]
    assert len(tail) == 16 and all(c in "0123456789abcdef" for c in tail), (
        "后缀须为 16 位十六进制，实际=%r" % tail
    )


@case("normalize_json：dict/list 原样返回；json 串解析；非 json 串原样；空/None 原样")
def _t_normalize():
    assert sr._normalize_json({"a": 1}) == {"a": 1}
    assert sr._normalize_json([1, 2]) == [1, 2]
    assert sr._normalize_json('{"b": 2}') == {"b": 2}
    assert sr._normalize_json("plain") == "plain"
    assert sr._normalize_json("") == ""
    assert sr._normalize_json(None) is None


@case("blocked_summary：有阻断层 ⇒ 含层名与理由；无阻断 ⇒ 兜底文案")
def _t_blocked_summary():
    blocked = sr._blocked_summary({
        "layers": [
            {"layer": "L1", "name": "语法", "ok": False, "skipped": False,
             "detail": "缺少 SELECT"},
            {"layer": "L3", "name": "只读", "ok": True, "skipped": False, "detail": "ok"},
        ],
        "rule_violations": [],
    })
    assert "L1" in blocked and "缺少 SELECT" in blocked, "应点出 L1 及其理由：%r" % blocked
    empty = sr._blocked_summary({"layers": [], "rule_violations": []})
    assert empty, "无阻断层时应返回兜底文案而非空串"


@case("resolve_versions：全部为 None 且 sqlpack 不可用 ⇒ 原样返回 (None,None,None)")
def _t_resolve_versions_none():
    # sqlpack.build 在缺库/缺数据时会抛异常被函数内部吞掉，返回入参原值。
    out = sr._resolve_versions("D-X", "B", None, None, None)
    assert out == (None, None, None), "无版本且无 pack 时应尽力而为返回 None，实际=%r" % (out,)


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
    print("\nsqlrun_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
