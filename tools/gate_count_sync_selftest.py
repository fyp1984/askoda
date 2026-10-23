#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁项数「回填命令」的自证（纯字符串，不碰真实文件）。

为什么这个工具也要判据
----------------------
回填工具是**唯一会写文档**的脚本。它的失败方式比"报错"安静得多：

  · 正则匹配不上 ⇒ 如果实现是"找不到就跳过"，结果是一次**什么都没做**的
    "✅ 已回填"，而文档还是旧数字 ⇒ 下一次门禁照样红，人还得手工找；
  · 正则匹配太宽 ⇒ 顺手改了表格外的行（措辞、其他层），
    文档开始"自己变样"，再没人知道哪一版是人写的；
  · 把"没变"的行也重写 ⇒ 一行少个换行，diff 里冒出无意义变更；
  · 默认就写 ⇒ 回填从"人的决定"退化成"脚本的自动反应"，
    漂移会被抹平 —— 这正是 gate_count_check 坚持"只判定不写"的原因。

⚠️ 本文件只测**纯函数**（plan_readme / plan_contributing），
   绝不读写仓库里的真文档 —— 判据不能因为自己被跑就把文档改了。

用法：
    python3 tools/gate_count_sync_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import inspect
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gate_count_sync as gs  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


ACTUAL = {"G0": 86, "G1": 26, "G2": 6, "G3": 2, "TOTAL": 120, "DAILY": 112}

README_SAMPLE = """# 标题

- 门禁 **114 项全绿**：G0 静态 82 · G1 单元 25 · G2 独立复核 5 · G3 真实链路 2（日常只跑 G0+G1 共 107）
- 另一条无关的列表项，不该被碰
"""

CONTRIB_SAMPLE = """| 层 | 说明 | 项数 | 是否阻塞 |
|---|---|---|---|
| G0 | 静态自检（语法 + 依赖基线） | 82 | 否 |
| G1 | 单元自证（`*_selftest.py`） | 25 | 否 |
| G2 | 独立复核（`*_check.py`） | 5 | 否 |
| G3 | 真实链路端到端 | 2 | **是** |

表格后面的正文，不该被碰。
"""


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

@case("README：数字被换成实际值，措辞与结构不变")
def _t_readme_rewrite():
    new, old, line = gs.plan_readme(README_SAMPLE, ACTUAL)
    assert "**120 项全绿**" in line, line
    assert "G0 静态 86" in line and "G1 单元 26" in line, line
    assert "G2 独立复核 6" in line and "G3 真实链路 2" in line, line
    assert "共 112" in line, line
    assert new.count("门禁 **120 项全绿**") == 1, new
    # 无关的行必须原样保留
    assert "- 另一条无关的列表项，不该被碰" in new, new


@case("README：找不到那一行 ⇒ 报错，不静默跳过（静默 = 什么都没做还说成功）")
def _t_readme_missing():
    try:
        gs.plan_readme("# 没有任何门禁行\n", ACTUAL)
    except SystemExit as e:
        assert "找不到" in str(e), e
        return
    raise AssertionError("门禁行不存在时没有报错，而是静默返回了 —— 回填会变成空操作")


@case("README：幂等（同一份文本跑两次，第二次不再产生差异）")
def _t_readme_idempotent():
    once, _o, _l = gs.plan_readme(README_SAMPLE, ACTUAL)
    twice, _o2, l2 = gs.plan_readme(once, ACTUAL)
    assert once == twice, "第二次回填又改了内容，说明不是幂等"
    assert l2 in once, l2


# ---------------------------------------------------------------------------
# CONTRIBUTING
# ---------------------------------------------------------------------------

@case("表格：G0–G3 四行的数字都被换成实际值")
def _t_contrib_rewrite():
    new, changes = gs.plan_contributing(CONTRIB_SAMPLE, ACTUAL)
    assert len(changes) == 4, "应恰好处理 4 行，实际 %r" % [c[0] for c in changes]
    assert "| G0 | 静态自检（语法 + 依赖基线） | 86 | 否 |" in new, new
    assert "| G1 | 单元自证（`*_selftest.py`） | 26 | 否 |" in new, new
    assert "| G2 | 独立复核（`*_check.py`） | 6 | 否 |" in new, new


@case("表格：没变的那一行必须逐字节不变（否则 diff 里冒出假变更）")
def _t_contrib_noop_row():
    _new, changes = gs.plan_contributing(CONTRIB_SAMPLE, ACTUAL)
    g3 = [c for c in changes if c[0] == "G3"][0]
    assert g3[1] == g3[2], (
        "G3 的数字没变，重建后却不同：%r → %r\n"
        "        ⚠️ 常见成因：正则把行尾换行吞进了分组。" % (g3[1], g3[2]))


@case("表格：表外内容（正文、表头、分隔行）一个字都不动")
def _t_contrib_scope():
    new, _c = gs.plan_contributing(CONTRIB_SAMPLE, ACTUAL)
    assert "| 层 | 说明 | 项数 | 是否阻塞 |" in new, new
    assert "|---|---|---|---|" in new, new
    assert "表格后面的正文，不该被碰。" in new, new


@case("表格：缺行 ⇒ 报错并点出缺哪层（不猜、不新建行）")
def _t_contrib_missing():
    broken = CONTRIB_SAMPLE.replace("| G2 | 独立复核（`*_check.py`） | 5 | 否 |\n", "")
    try:
        gs.plan_contributing(broken, ACTUAL)
    except SystemExit as e:
        assert "G2" in str(e), "报错要点出缺的是哪一层：%s" % e
        return
    raise AssertionError("缺了 G2 行却没报错 —— 少了那层就再也不会被回填")


@case("表格：幂等")
def _t_contrib_idempotent():
    once, _c = gs.plan_contributing(CONTRIB_SAMPLE, ACTUAL)
    twice, changes = gs.plan_contributing(once, ACTUAL)
    assert once == twice, "第二次回填又改了内容"
    for layer, o, n in changes:
        assert o == n, "已是新值却仍判为变更：%s" % layer


# ---------------------------------------------------------------------------
# 安全性：默认不写
# ---------------------------------------------------------------------------

@case("默认不写：写文件必须由 --write 显式触发（回填是人的决定）")
def _t_write_requires_flag():
    src = inspect.getsource(gs.main)
    assert "--write" in src, "main() 里没看到 --write 开关"
    # 写动作必须在 write 分支内：简化判据——_write( 的调用点必须在 `if not write` 之后
    i_guard = src.find("if not write")
    i_write = src.find("_write(")
    assert i_guard >= 0, "没找到 `if not write` 这道闸"
    assert i_write > i_guard, (
        "_write() 出现在 `if not write` 闸门之前或不存在 —— "
        "预览模式可能会真的改文档")
    # 闸门后必须紧跟 return，否则会继续往下走去写
    tail = src[i_guard:i_guard + 400]
    assert "return 1" in tail, "预览分支没有 return，会继续往下执行到写文件"


@case("写入原子性：先算好两份新文本再落盘（不能改完一份另一份报错）")
def _t_plan_before_write():
    src = inspect.getsource(gs.main)
    i_plan_r = src.find("plan_readme")
    i_plan_c = src.find("plan_contributing")
    i_write = src.find("_write(")
    assert 0 <= i_plan_r < i_write and 0 <= i_plan_c < i_write, (
        "计划与写入的顺序不对：两份文本必须在任何一次 _write 之前都算完，"
        "否则第一份写完、第二份才报错，文档就停在半改状态")


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
    print("\ngate_count_sync_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
