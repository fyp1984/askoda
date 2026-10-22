#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型原生 SQL 生成（gateway/llm.py + gateway/sqlgen_llm.py）离线自证。

测什么 / 不测什么
-----------------
只测**离线可判定的纯逻辑**：
  - LLM 配置门（未配置/被关闭 ⇒ 禁用）与 URL 规范化；
  - chat() 在禁用时不发网络请求、返回 disabled；
  - 模型响应解析（纯 JSON / 代码围栏 / 裸 SQL / garbage）；
  - 语义层上下文渲染（模型/列/中文名/口径/关系是否进 prompt）；
  - generate_sql 的编排：禁用即返回 disabled；复用假 chat + 校验桩验证
    「一次通过」「先失败后修复通过」两条路径（不依赖 sqlglot / 网络）。

**不测**：真实模型调用（需网络与密钥）、gates.review 的真实五层判定
（需 sqlglot，在容器/CI 内由其它门禁覆盖）。校验层在此用桩替换，只验证编排正确。

用法：python3 tools/llm_sqlgen_selftest.py   （退出码 0=全过，1=有失败）
"""
import contextlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "gateway"))

# registry 在 import 时读取 MDL_B_PATH；必须提前指向仓库内的真实 MDL。
_B_MDL = os.path.join(_REPO, "wren-docker-b", "workspace", "mdl.json")
if os.path.exists(_B_MDL):
    os.environ.setdefault("MDL_B_PATH", _B_MDL)

import llm  # noqa: E402
import sqlgen_llm  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_NO_LLM = dict(LLM_BASE_URL=None, LLM_API_KEY=None, LLM_MODEL=None, LLM_ENABLED=None)
_FAKE_LLM = dict(
    LLM_BASE_URL="https://example.invalid/v1",
    LLM_API_KEY="k",
    LLM_MODEL="fake-model",
    LLM_ENABLED=None,
)


# ---------------------------------------------------------------------------
# 配置门
# ---------------------------------------------------------------------------
@case("配置：三项缺失 ⇒ configured/enabled 均 False")
def _t_cfg_missing():
    with env(**_NO_LLM):
        c = llm.config()
        assert c["configured"] is False, "缺 base_url/key/model 不应视为已配置"
        assert c["enabled"] is False, "未配置不应启用"
        assert llm.is_enabled() is False


@case("配置：三项齐全 ⇒ 启用；LLM_ENABLED=0 ⇒ 强制禁用")
def _t_cfg_enabled_and_off():
    with env(**_FAKE_LLM):
        assert llm.config()["configured"] is True
        assert llm.is_enabled() is True, "三项齐全应启用"
    with env(**{**_FAKE_LLM, "LLM_ENABLED": "0"}):
        assert llm.is_enabled() is False, "LLM_ENABLED=0 必须强制禁用（即便配置齐全）"


@case("URL 规范化：根地址补 /chat/completions，已带则原样")
def _t_url():
    assert llm.completions_url("https://x/v1") == "https://x/v1/chat/completions"
    assert llm.completions_url("https://x/v1/") == "https://x/v1/chat/completions"
    assert llm.completions_url("https://x/v1/chat/completions") == "https://x/v1/chat/completions"
    assert llm.completions_url("") == ""


# ---------------------------------------------------------------------------
# chat() 禁用时不发网络
# ---------------------------------------------------------------------------
@case("chat：禁用 ⇒ ok=False 且 disabled=True（不触网）")
def _t_chat_disabled():
    with env(**_NO_LLM):
        r = llm.chat([{"role": "user", "content": "hi"}])
        assert r["ok"] is False
        assert r.get("disabled") is True
        assert "未启用" in (r.get("error") or "")


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
@case("解析：纯 JSON ⇒ 取 sql/explanation/confidence")
def _t_parse_json():
    txt = json.dumps({
        "sql": "SELECT a FROM t", "explanation": "e",
        "objects": [{"type": "model", "name": "t"}], "confidence": 0.8,
    }, ensure_ascii=False)
    r = sqlgen_llm.parse_generation_response(txt)
    assert r["sql"] == "SELECT a FROM t"
    assert r["parse_ok"] is True
    assert abs((r["confidence"] or 0) - 0.8) < 1e-9
    assert r["objects"] and r["objects"][0]["name"] == "t"


@case("解析：代码围栏包裹的 SQL ⇒ 去围栏取 SQL")
def _t_parse_fence():
    r = sqlgen_llm.parse_generation_response("```sql\nSELECT x FROM y\n```")
    assert r["sql"] == "SELECT x FROM y", r["sql"]


@case("解析：裸 SELECT ⇒ 直接当 SQL；garbage ⇒ 空串且 parse_ok=False")
def _t_parse_bare_and_garbage():
    assert sqlgen_llm.parse_generation_response("SELECT * FROM t")["sql"] == "SELECT * FROM t"
    g = sqlgen_llm.parse_generation_response("这不是 SQL，也没有 JSON")
    assert g["sql"] == ""
    assert g["parse_ok"] is False


@case("解析：JSON 里 sql 为空串 ⇒ 保留空串（模型表示无法满足）")
def _t_parse_empty_sql():
    r = sqlgen_llm.parse_generation_response('{"sql":"","explanation":"闭集内无法满足"}')
    assert r["sql"] == ""
    assert r["parse_ok"] is True
    assert "无法满足" in r["explanation"]


# ---------------------------------------------------------------------------
# 语义层上下文渲染
# ---------------------------------------------------------------------------
@case("上下文：渲染出模型/列/中文名/关系条件（真实 B 库 MDL）")
def _t_schema_ctx():
    import registry
    ctx = sqlgen_llm.build_schema_context(registry.get("B"))
    assert "dwd_order_di" in ctx, "应包含模型名"
    assert "pay_amount" in ctx, "应包含列名"
    assert "中文名" in ctx, "应带中文名"
    assert "dwd_order_di.member_id = dim_member.member_id" in ctx, "应包含已建模关联条件"
    assert "只允许 SELECT" in ctx or "只读" in ctx


@case("Prompt：需求文本与 JSON 约束进入 user 消息")
def _t_prompt():
    import registry
    msgs = sqlgen_llm.build_generation_messages(
        registry.get("B"), "查询2026年各会员等级的复购金额降序", structured=None
    )
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "查询2026年各会员等级的复购金额降序" in user
    assert "JSON" in user and "sql" in user


# ---------------------------------------------------------------------------
# generate_sql 编排（禁用 / 一次通过 / 修复通过）
# ---------------------------------------------------------------------------
@case("generate：禁用 ⇒ ok=False 且 disabled=True")
def _t_gen_disabled():
    with env(**_NO_LLM):
        r = sqlgen_llm.generate_sql(demand_id=None, dataset="B", nl="查销售额")
        assert r["ok"] is False
        assert r.get("disabled") is True


@case("generate：首轮即通过 ⇒ generator 走 llm，attempts=1")
def _t_gen_success():
    with env(**_FAKE_LLM):
        orig_chat = llm.chat
        orig_val = sqlgen_llm.validate_sql
        try:
            sqlgen_llm.llm.chat = lambda *a, **k: {
                "ok": True, "content": '{"sql":"SELECT 1 AS x","explanation":"ok"}',
                "latency_ms": 5,
            }
            sqlgen_llm.validate_sql = lambda sql, ds, req: (True, [], {"status": "通过"})
            r = sqlgen_llm.generate_sql(demand_id=None, dataset="B", nl="查个常量")
            assert r["ok"] is True, r.get("error")
            assert r["sql"] == "SELECT 1 AS x"
            assert len(r["attempts"]) == 1
        finally:
            sqlgen_llm.llm.chat = orig_chat
            sqlgen_llm.validate_sql = orig_val


@case("generate：首轮不过→修复后通过 ⇒ attempts=2")
def _t_gen_repair():
    with env(LLM_MAX_REPAIR="1", **_FAKE_LLM):
        orig_chat = llm.chat
        orig_val = sqlgen_llm.validate_sql
        state = {"n": 0}

        def fake_chat(*a, **k):
            state["n"] += 1
            return {"ok": True, "content": '{"sql":"SELECT bad FROM t","explanation":"try"}',
                    "latency_ms": 1}

        def fake_validate(sql, ds, req):
            # 第一次失败，第二次通过
            if state["n"] <= 1:
                return False, ["L-REQ 缺少排序要素"], {"status": "不通过"}
            return True, [], {"status": "通过"}

        try:
            sqlgen_llm.llm.chat = fake_chat
            sqlgen_llm.validate_sql = fake_validate
            r = sqlgen_llm.generate_sql(demand_id=None, dataset="B", nl="查排序")
            assert r["ok"] is True, r.get("error")
            assert len(r["attempts"]) == 2, "应经过两轮（首轮失败 + 修复）"
            assert any("修复" in n or "通过" in n for n in r["notes"])
        finally:
            sqlgen_llm.llm.chat = orig_chat
            sqlgen_llm.validate_sql = orig_val


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
    print("\nllm_sqlgen_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
