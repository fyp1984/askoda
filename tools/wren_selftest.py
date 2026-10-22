#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wren MCP 客户端（wren.py）纯函数 / 契约自证（离线，不连网络）。

为什么单独做这个自证
--------------------
`wren.py` 是只读执行链路里"打语义层"的客户端：URL 归一化、超时解析、
异常层级、读工具白名单，都是**不依赖网络**就能钉死的契约。它们一旦改错：
  · endpoint 拼错 ⇒ 所有 dry_run / query 静默连不到端；
  · 异常层级错 ⇒ 超时与普通错误无法区分，重试策略失效；
  · READ_TOOLS 漏登记 ⇒ 写操作被当成读操作放行（只读门禁形同虚设）。
这些都是"改了不报错、运行时才爆"的坑，用单元自证封死。

用法：
    python3 tools/wren_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

GW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway")
sys.path.insert(0, GW)
import wren as wr  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 端点 URL 归一化
# ---------------------------------------------------------------------------

@case("endpoint：base 自动拼 /mcp，且去掉尾部斜杠")
def _t_endpoint():
    assert wr.WrenClient("http://host:9000").endpoint == "http://host:9000/mcp"
    assert wr.WrenClient("http://host:9000/").endpoint == "http://host:9000/mcp"


@case("endpoint：带路径前缀的 base 也正确拼接")
def _t_endpoint_path():
    c = wr.WrenClient("http://host:9000/foo")
    assert c.endpoint == "http://host:9000/foo/mcp", "实际=%r" % c.endpoint


# ---------------------------------------------------------------------------
# 异常层级（超时必须是 WrenError，WrenError 必须是 RuntimeError）
# ---------------------------------------------------------------------------

@case("异常层级：WrenTimeout ⊂ WrenError ⊂ RuntimeError")
def _t_exc_hierarchy():
    assert issubclass(wr.WrenTimeout, wr.WrenError), "超时须是 WrenError 子类"
    assert issubclass(wr.WrenError, RuntimeError), "WrenError 须是 RuntimeError 子类"


# ---------------------------------------------------------------------------
# 超时解析（显式传入优先；否则读 WREN_<NAME>_TIMEOUT / WREN_TIMEOUT；都无=60）
# ---------------------------------------------------------------------------

@case("超时：显式传入优先，且转成 float")
def _t_timeout_explicit():
    assert wr.WrenClient("http://h:1", timeout=5).timeout == 5.0
    assert wr.WrenClient("http://h:1", timeout=0.2).timeout == 0.2


@case("超时：无环境且无传入 ⇒ 默认 60.0")
def _t_timeout_default():
    saved = {}
    for k in ("WREN_WREN_TIMEOUT", "WREN_TIMEOUT"):
        saved[k] = os.environ.pop(k, None)
    try:
        assert wr.WrenClient("http://h:1").timeout == 60.0, "默认超时应为 60"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@case("超时：WREN_WREN_TIMEOUT 环境变量生效")
def _t_timeout_env():
    saved = os.environ.get("WREN_WREN_TIMEOUT")
    os.environ["WREN_WREN_TIMEOUT"] = "12"
    try:
        assert wr.WrenClient("http://h:1").timeout == 12.0, "环境变量超时应生效"
    finally:
        if saved is None:
            os.environ.pop("WREN_WREN_TIMEOUT", None)
        else:
            os.environ["WREN_WREN_TIMEOUT"] = saved


# ---------------------------------------------------------------------------
# 只读工具白名单（漏登记读工具 = 只读门禁失效）
# ---------------------------------------------------------------------------

@case("读工具白名单：含 dry_run / query / health_check 等关键项")
def _t_read_tools():
    assert isinstance(wr.READ_TOOLS, (set, frozenset)), "READ_TOOLS 应是集合"
    for t in ("dry_run", "query", "health_check", "get_manifest", "get_relationships"):
        assert t in wr.READ_TOOLS, "读工具 %s 必须登记进 READ_TOOLS" % t


@case("可观测字段：last_attempts 初始化为 0")
def _t_last_attempts():
    assert wr.WrenClient("http://h:1").last_attempts == 0


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
    print("\nwren_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
