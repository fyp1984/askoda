#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红线守卫 · 门禁 G2 收编入口（把 `tools/redline_guard.py` 挂进统一门禁）

为什么需要这个文件
------------------
`tools/redline_guard.py` 是**六条红线的机器判定体**，但它**不在任何强制执行路径上**：
  · `tools/gate_all.py` 全文出现 `redline_guard` **0** 次；
  · G1 收 `tools/*_selftest.py`、G2 收 `tools/*_check.py`、G3 收 `tools/*_verify.py`
    —— `redline_guard.py` 三个后缀**都不匹配** ⇒ 不被任何一层发现；
  · 它只被 `tools/sense.py` / `tools/coord.py` 调用，而这两个脚本也不在门禁发现口径里；
  · 仓库无 CI（无 `.github` / `.gitlab-ci.yml` / `.pre-commit-config.yaml` / `Makefile` /
    git hooks）。

⇒ 六条红线整体处于「**靠人记得手动跑**」的状态。实测过的后果：
   `git rm gateway/app.py` ⇒ 48 个已注册 MCP 工具全部消失，而 R2 判 CLEAR；
   G0 `py_compile` 拦不住（少一个文件 ≠ 语法错）、requirements 拦不住、R3–R6 全不相关
   ⇒ **整个仓库没有任何一层兜得住**。

本文件做的事
------------
**调用** `redline_guard` 的判据函数，把结果转成 G2 的判定格式（exit 0 / 非 0）。

⚠️ 为什么文件名必须以 `_check.py` 结尾：
   G2 用 `glob("tools/*_check.py")` 自动发现脚本。改名成别的后缀会**掉出 G2 门禁**，
   本文件存在的全部意义就没了。

⚠️ 判据全部**离线可跑**，不需要容器 / 网关 / DB：
   R1 读改动文件清单；R2 读 `gateway/app.py` 两版源码并解析 AST；
   R3–R6 读 diff 正文。⇒ 符合 G2「独立复核」定位，不引入环境依赖。

判定基准
--------
判「**当前工作树相对 HEAD**」的改动——与 `redline_guard.py` 的默认模式一致，
也是交付前自检时人实际会跑的那一次。

用法：
    python3 tools/redline_guard_check.py
退出码：0 = 六条红线全未触；1 = 命中任一条。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import redline_guard as rg  # noqa: E402


def main():
    print("=" * 74)
    print("红线守卫 · 门禁收编入口（独立于执行者，命中即 BLOCKED）")
    print("=" * 74)
    print("  仓库根：%s" % ROOT)
    print("  判定基准：当前工作树相对 HEAD 的改动")
    print()

    # 改动文件清单 + diff 正文（与 redline_guard 默认模式同口径）
    files, err = rg._diff_files(base=None, staged=False)
    if err and not files:
        print("FATAL：无法获取改动文件清单（%s）" % err)
        return 2
    diff_text = rg._diff_body(base=None, staged=False)

    # ⚠️ 判据清单与分派**全部复用** redline_guard 的 `run_all_checks()`，
    #    不在这里另抄一份。旧写法在本文件里自己按 `co_argcount` 猜怎么调，
    #    等于把「R2 的 base/staged 传不进去」那个 bug 复制了一份到门禁里。
    results = rg.run_all_checks(diff_text, files, base=None, staged=False)

    print("  改动文件：%d 个" % len(files))
    print("-" * 74)
    for r in results:
        mark = "🔴 命中" if r["hit"] else "⚪ 未触"
        print("  %s  %s  %s" % (mark, r["id"], r["name"]))
        for d in r["detail"]:
            print("        · %s" % d)
    print("-" * 74)

    any_hit = any(r["hit"] for r in results)
    if any_hit:
        print("总判定：🔴 BLOCKED —— 命中红线，须出《红线阻断报告》后停止后续动作。")
        for r in results:
            if r["hit"]:
                print("  · %s %s：%s" % (r["id"], r["name"], r["reason"]))
        return 1
    print("总判定：✅ CLEAR —— 六条红线全未触。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
