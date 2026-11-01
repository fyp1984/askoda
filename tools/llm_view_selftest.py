#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BFF「模型端点放行」规则自证（bff/llm_view.py）。

为什么要单独测这一层
--------------------
它是全项目**唯一一处**在 `sanitize()` 之后把值补回去的代码——也就是唯一可能
削弱 R2 出参脱敏的地方。这种地方不该靠「看一眼觉得对」，必须有断言盯着：
  · 该放行的三处（顶层 / config / settings / providers）确实放行了；
  · 除这几处之外，脱敏结果**一个字节都不许被改动**；
  · 不合规形态（不是 http(s)、超长、带 userinfo、空 authority）一律不放。

`sanitize` 用**桩**替换：按实测行为建模（`*url*` 键换占位符；`*_endpoint`/`*_path`
键直接删）。这样本自测不需要 fastapi，也不依赖受保护模块，可离线重放。

用法：python3 tools/llm_view_selftest.py   （退出码 0=全过，1=有失败）
"""
import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "bff"))

import llm_view  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


PLACEHOLDER = "[地址已脱敏]"
_DROP_KEYS = ("wren_endpoint", "mdl_path", "data_source", "dsn", "api_key")


def stub_sanitize(payload):
    """按实测行为建模的脱敏桩：键名含 url ⇒ 值换占位符；命中删除清单 ⇒ 整键删掉。

    实测细节（2026-10-22 真实链路）：**空串不替换**——`custom` 预设的空地址回来
    仍是 `''`。桩与真实行为一致，用例才有意义。
    """
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k in _DROP_KEYS:
                continue
            if isinstance(v, str) and v and "url" in k.lower():
                out[k] = PLACEHOLDER
            else:
                out[k] = stub_sanitize(v)
        return out
    if isinstance(payload, list):
        return [stub_sanitize(v) for v in payload]
    return payload


PRESETS = [
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat"]},
    {"key": "custom", "label": "自定义", "base_url": "", "models": []},
]


def _raw(**kw):
    base = {
        "ok": True,
        "source": "db",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "has_credential": True,
        "credential_masked": "sk-…1234",
        "providers": copy.deepcopy(PRESETS),
        "recent_changes": [{"action": "save", "actor": "R4"}],
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# safe_endpoint
# ---------------------------------------------------------------------------
@case("放行：正常的 http(s) 端点原样返回，尾斜杠保留")
def _t_safe_ok():
    for u in (
        "https://api.deepseek.com/v1",
        "http://10.0.0.5:8000/v1",
        "https://llm.internal.example/compatible-mode/v1/",
    ):
        assert llm_view.safe_endpoint(u) == u, u


@case("拒绝：非 http(s)、超长、带 userinfo、无 authority、非字符串")
def _t_safe_reject():
    bad = [
        None,
        "",
        "   ",
        "api.deepseek.com/v1",           # 缺协议
        "ftp://api.example.com/v1",      # 协议不对
        "https://",                       # 无 authority
        "https://user:pass@host/v1",      # 带账号口令
        "https://api.example.com/" + "a" * 400,  # 超长
        123,
        {"base_url": "https://x/v1"},
    ]
    for v in bad:
        assert llm_view.safe_endpoint(v) == "", "应拒绝：%r" % (v,)


# ---------------------------------------------------------------------------
# apply：只补该补的
# ---------------------------------------------------------------------------
@case("补回：顶层 base_url 与 providers[].base_url 都被补回")
def _t_apply_top():
    raw = _raw()
    clean = stub_sanitize(raw)
    assert clean["base_url"] == PLACEHOLDER, "桩没生效，用例前提不成立"
    out = llm_view.apply(raw, clean)
    assert out["base_url"] == "https://api.deepseek.com/v1", out["base_url"]
    assert out["providers"][0]["base_url"] == "https://api.deepseek.com/v1"
    assert out["providers"][1]["base_url"] == "", "custom 的空地址不该被填成别的"


@case("补回：config / settings 两层嵌套都生效（保存返回体是这一形状）")
def _t_apply_nested():
    raw = _raw(
        config={"base_url": "http://10.0.0.5:8000/v1", "model": "m"},
        settings={"base_url": "https://api.deepseek.com/v1", "model": "m", "providers": copy.deepcopy(PRESETS)},
    )
    out = llm_view.apply(raw, stub_sanitize(raw))
    assert out["config"]["base_url"] == "http://10.0.0.5:8000/v1", out["config"]
    assert out["settings"]["base_url"] == "https://api.deepseek.com/v1", out["settings"]
    assert out["settings"]["providers"][0]["base_url"] == "https://api.deepseek.com/v1"


@case("不越界：除 base_url 之外的任何字段都不许被改动")
def _t_no_side_effect():
    raw = _raw(url_note="https://should-be-masked.example/v1", other={"endpoint_url": "https://x/v1"})
    clean = stub_sanitize(raw)
    before = copy.deepcopy(clean)
    out = llm_view.apply(raw, clean)
    for k in before:
        if k == "base_url":
            continue
        if k == "providers":
            # providers 里除了 base_url，其余字段必须原样
            for i, p in enumerate(before["providers"]):
                for kk, vv in p.items():
                    if kk == "base_url":
                        continue
                    assert out["providers"][i][kk] == vv, (i, kk)
            continue
        assert out[k] == before[k], "字段 %s 被改动了：%r → %r" % (k, before[k], out[k])
    # 名字里带 url 的其它字段仍保持脱敏
    assert out["url_note"] == PLACEHOLDER, out["url_note"]
    assert out["other"]["endpoint_url"] == PLACEHOLDER, out["other"]


@case("不越界：被删除的敏感键不会因为本步被复活")
def _t_no_resurrect():
    raw = _raw(wren_endpoint="http://wren-b:9000/mcp", api_key="sk-should-never-appear")
    out = llm_view.apply(raw, stub_sanitize(raw))
    assert "wren_endpoint" not in out, "敏感键被复活了"
    assert "api_key" not in out, "密钥键被复活了"
    import json

    blob = json.dumps(out, ensure_ascii=False)
    assert "sk-should-never-appear" not in blob
    assert "wren-b" not in blob and "9000" not in blob


@case("容错：raw/clean 不是 dict、缺字段、providers 长度不一致都不抛")
def _t_tolerant():
    assert llm_view.apply(None, None) is None
    assert llm_view.apply({}, None) is None
    assert llm_view.apply(None, {}) == {}
    assert llm_view.apply({"settings": None}, {"settings": None}) == {"settings": None}
    # providers 两个列表长度不同：只按较短的那一侧补，不许越界
    raw = _raw(providers=copy.deepcopy(PRESETS))
    clean = stub_sanitize(raw)
    clean["providers"] = clean["providers"][:1]
    out = llm_view.apply(raw, clean)
    assert len(out["providers"]) == 1
    assert out["providers"][0]["base_url"] == "https://api.deepseek.com/v1"


@case("兼容：view(raw, sanitizer) 便捷入口等价于 apply(raw, sanitize(raw))")
def _t_view_entry():
    raw = _raw()
    a = llm_view.view(raw, stub_sanitize)
    b = llm_view.apply(raw, stub_sanitize(raw))
    assert a == b


@case("常量：端点长度上限是个合理值，且被 safe_endpoint 真正使用")
def _t_limit():
    assert 100 <= llm_view.ENDPOINT_MAX <= 1000, llm_view.ENDPOINT_MAX
    base = "https://a.example/v1"
    ok = base + "?" + "x" * (llm_view.ENDPOINT_MAX - len(base) - 1)
    assert len(ok) == llm_view.ENDPOINT_MAX
    assert llm_view.safe_endpoint(ok) == ok, "恰好等于上限应放行"
    assert llm_view.safe_endpoint(ok + "x") == "", "超过上限应拒绝"


@case("占位符不残留：即便 sanitize 把空串也换成占位符，界面也拿到空串")
def _t_placeholder_never_leaks():
    def aggressive(payload):  # 假设未来 sanitize 收紧了：空串也换占位符
        out = stub_sanitize(payload)
        for p in out.get("providers") or []:
            if p.get("base_url") == "":
                p["base_url"] = PLACEHOLDER
        if out.get("base_url") == "":
            out["base_url"] = PLACEHOLDER
        return out

    raw = _raw(base_url="", providers=[{"key": "custom", "base_url": "", "models": []}])
    out = llm_view.apply(raw, aggressive(raw))
    assert out["base_url"] == "", out["base_url"]
    assert out["providers"][0]["base_url"] == "", out["providers"][0]["base_url"]


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
    print("\nllm_view_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
