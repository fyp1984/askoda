#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型配置层（gateway/llm_settings.py + llm.config 优先级）离线自证。

测什么 / 不测什么
-----------------
只测**离线可判定的逻辑**，全部不碰真实数据库、不发真实网络请求：
  - 供应商预设的完整性、按地址反推预设（精确匹配，不猜）；
  - 密钥掩码：短串全星、长串只露头尾、空串为空；
  - 环境变量解析与派生字段（configured / enabled / missing）；
  - **优先级语义**：库里有行 ⇒ 整行生效、连环境变量都不看；没有行 ⇒ 回落环境变量；
    读库失败 ⇒ 回落环境变量且带 `db_error`，**不抛异常**；`LLM_SETTINGS_SOURCE=env`
    ⇒ 一次库都不读（部署侧关掉界面改配置的口子，也让门禁与本机库状态解耦）；
  - 保存校验：缺地址 / 缺模型 / 开了开关没密钥 ⇒ 不落库、逐条给原因；
  - 密钥三态：留空=不改、clear_credential=清空、新值=覆盖；
  - **防泄漏**：`get_settings()` 的整个返回体里不得出现密钥原文（最强的断言方式）；
  - 连通性自测：配置不全不联网、配齐后按桩返回，`used_saved_credential` 语义正确；
  - 报错翻译：401/404/429/超时都给出「该改哪里」，且不带内部地址；
  - 缓存：同一 TTL 内只读一次库，写入后立即失效。

**不测**：真实模型的连通性（需网络与密钥）、真实 PG 的 DDL 落库（G0/G2 覆盖）。

用法：python3 tools/llm_settings_selftest.py   （退出码 0=全过，1=有失败）
"""
import contextlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "gateway"))

import llm  # noqa: E402
import llm_settings as S  # noqa: E402

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


_NO_ENV = dict(
    LLM_BASE_URL=None, LLM_API_KEY=None, LLM_MODEL=None, LLM_ENABLED=None,
    LLM_TEMPERATURE=None, LLM_TIMEOUT=None, LLM_MAX_REPAIR=None,
)
_ENV_FULL = dict(
    LLM_BASE_URL="https://env.example.com/v1/",
    LLM_API_KEY="env-secret-key-0001",
    LLM_MODEL="env-model",
    LLM_ENABLED=None,
    LLM_TEMPERATURE=None,
    LLM_TIMEOUT=None,
    LLM_MAX_REPAIR=None,
)


class FakeDB:
    """替身元数据库：行为足够真，才能测出「保存 → 生效」的真实语义。"""

    def __init__(self, row=None):
        self.row = dict(row) if row else None
        self.executed = []

    def query_one(self, sql, params=None):
        return dict(self.row) if self.row else None

    def query(self, sql, params=None):
        return []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INSERT INTO llm_settings" in sql:
            p = params
            self.row = {
                "provider": p[1], "base_url": p[2], "api_key": p[3], "model": p[4],
                "enabled": p[5], "temperature": p[6], "timeout": p[7],
                "max_repair": p[8], "updated_by": p[9], "updated_at": None,
            }
        elif "DELETE FROM llm_settings" in sql:
            self.row = None
        return 1

    @staticmethod
    def dumps(obj):
        return json.dumps(obj, ensure_ascii=False)


@contextlib.contextmanager
def fake_db(row=None):
    old = S.db
    S.db = FakeDB(row)
    S.invalidate()
    try:
        yield S.db
    finally:
        S.db = old
        S.invalidate()


@contextlib.contextmanager
def broken_db():
    """库不可用：`_row()` 直接抛异常（模拟 DSN 未配 / PG 未就绪）。"""

    class Broken(FakeDB):
        def query_one(self, sql, params=None):
            raise RuntimeError("connection refused")

    old = S.db
    S.db = Broken()
    S.invalidate()
    try:
        yield
    finally:
        S.db = old
        S.invalidate()


@contextlib.contextmanager
def patch_chat(fn):
    old = S._chat
    S._chat = fn
    try:
        yield
    finally:
        S._chat = old


@contextlib.contextmanager
def patch_models(fn):
    """替换第一段探活（模型清单）。默认桩 = 「地址通、模型在清单里」。

    真实探活会往被测地址发 GET；自检不许打网络，必须显式打桩。
    默认给「通」，让原有那些「测 chat 行为」的用例不必逐个改语义。
    """
    old = S._list_models
    S._list_models = fn
    try:
        yield
    finally:
        S._list_models = old


def _models_ok(cfg, timeout=None):
    return {"ok": True, "status": 200, "models": [cfg.get("model") or "m"]}


def _row(**kw):
    base = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-db-secret-9876",
        "model": "deepseek-chat",
        "enabled": True,
        "temperature": 0,
        "timeout": 60,
        "max_repair": 1,
        "updated_by": "R4-管理员",
        "updated_at": None,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 预设与掩码
# ---------------------------------------------------------------------------
@case("预设：每家都有 key/label/note，custom 允许空地址空模型")
def _t_presets():
    ps = S.providers()
    assert len(ps) >= 5, "预设过少：%d" % len(ps)
    keys = set()
    for p in ps:
        assert p["key"] and p["label"], p
        assert p["key"] not in keys, "预设 key 重复：%s" % p["key"]
        keys.add(p["key"])
        assert isinstance(p["models"], list), p
        if p["key"] != "custom":
            assert p["base_url"].startswith("https://"), p
            assert p["models"], "非自定义预设必须给示例模型：%s" % p["key"]
    assert "custom" in keys, "必须保留「自定义」出口，否则私有化部署无处填地址"


@case("预设：providers() 返回深拷贝，调用方改不动全局")
def _t_presets_copy():
    a = S.providers()
    a[0]["models"].append("注入的假模型")
    b = S.providers()
    assert "注入的假模型" not in b[0]["models"], "返回了内部列表引用"


@case("预设：按地址反推供应商只做精确匹配（含尾斜杠归一）")
def _t_guess_provider():
    assert S.guess_provider("https://api.deepseek.com/v1") == "deepseek"
    assert S.guess_provider("https://api.deepseek.com/v1/") == "deepseek"
    assert S.guess_provider("  https://api.deepseek.com/v1  ") == "deepseek"
    # 不做模糊匹配：猜错供应商比猜不出来更糟
    assert S.guess_provider("https://api.deepseek.com") == ""
    assert S.guess_provider("https://my-proxy.internal/v1") == ""
    assert S.guess_provider("") == ""
    assert S.provider_of("不存在的供应商") is None


@case("掩码：空为空、短串全星、长串只露头 3 尾 4")
def _t_mask():
    assert S.mask("") == ""
    assert S.mask(None) == ""
    assert S.mask("abc") == "***"
    assert S.mask("12345678") == "********", "8 位及以下应整串打星"
    m = S.mask("sk-abcdefghijklmn")
    assert m.startswith("sk-") and m.endswith("klmn"), m
    assert "abcdefghij" not in m, "中间段必须被吃掉：%s" % m


# ---------------------------------------------------------------------------
# 解析与派生
# ---------------------------------------------------------------------------
@case("环境变量：三项齐 ⇒ configured/enabled；缺一 ⇒ 两者皆假")
def _t_env_parse():
    with env(**_ENV_FULL):
        c = S._finalize(S._from_env())
        assert c["configured"] and c["enabled"], c
        assert c["base_url"] == "https://env.example.com/v1", "尾斜杠未归一：%s" % c["base_url"]
        assert c["provider"] == "", "环境变量地址不属任何预设时应为空"
    with env(**{**_ENV_FULL, "LLM_MODEL": None}):
        c = S._finalize(S._from_env())
        assert not c["configured"] and not c["enabled"], c
        assert c["missing"] == ["model"], c["missing"]


@case("环境变量：LLM_ENABLED=0 只关开关，不改「配置已齐」的事实")
def _t_env_flag_off():
    with env(**{**_ENV_FULL, "LLM_ENABLED": "0"}):
        c = S._finalize(S._from_env())
        assert c["configured"] is True, "配齐了就是配齐了，开关是另一回事"
        assert c["enabled"] is False, "开关关掉必须不生效"


@case("行解析：数值越界收敛到合法区间，不报错也不静默清零")
def _t_row_coerce():
    c = S._finalize(S._from_row(_row(temperature=9, timeout=9999, max_repair=7)))
    assert c["temperature"] == 2.0, c["temperature"]
    assert c["timeout"] == 600, c["timeout"]
    assert c["max_repair"] == 3, c["max_repair"]
    c2 = S._finalize(S._from_row(_row(temperature=-1, timeout=1, max_repair=-5)))
    assert c2["temperature"] == 0.0 and c2["timeout"] == 5 and c2["max_repair"] == 0, c2


# ---------------------------------------------------------------------------
# 优先级（本文件最关键的语义）
# ---------------------------------------------------------------------------
@case("优先级：库里有行 ⇒ 整行生效，环境变量完全不参与")
def _t_db_wins():
    with env(**_ENV_FULL), fake_db(_row()):
        eff = S.effective()
        assert eff["source"] == "db", eff["source"]
        assert eff["base_url"] == "https://api.deepseek.com/v1", eff["base_url"]
        assert eff["model"] == "deepseek-chat", "环境变量的模型名不该混进来：%s" % eff["model"]
        cfg = llm.config()
        assert cfg["source"] == "db" and cfg["model"] == "deepseek-chat", cfg


@case("优先级：LLM_SETTINGS_SOURCE=env ⇒ 库里即便有行也不读，只认环境变量")
def _t_env_authoritative():
    """这条开关的第二个用途就是本文件的对门用例：门禁不许依赖本机库里存了什么。

    断言要能证明「根本没读库」，而不只是「结果碰巧等于环境变量」——否则把
    `_row()` 换成返回 None 也能过，等于没测。
    """
    with env(**_ENV_FULL, LLM_SETTINGS_SOURCE="env"), fake_db(_row()) as fdb:
        reads = []
        _orig = fdb.query_one
        fdb.query_one = lambda sql, params=None: (reads.append(sql), _orig(sql, params))[1]
        eff = S.effective()
        assert eff["source"] == "env", eff["source"]
        assert eff.get("forced") == "env", "必须标出「本次是按开关走的」，否则排障时会误判成库坏了"
        assert eff["base_url"] == "https://env.example.com/v1", eff["base_url"]
        assert reads == [], "开关打开时一次库都不该读：%d 次" % len(reads)
        assert eff["db_error"] == "", "没读库就不该有读库错误：%r" % eff["db_error"]
        assert llm.config()["model"] == "env-model", llm.config()["model"]
        # 不写缓存：关掉开关后必须立刻能读回库里的行，不能被上一条 env 结果粘住
        assert S.effective()["source"] == "env"


@case("优先级：关掉开关（不设/设成 auto）⇒ 恢复「库优先」")
def _t_env_switch_off():
    for val in (None, "auto", ""):
        with env(**_ENV_FULL, LLM_SETTINGS_SOURCE=val), fake_db(_row()):
            eff = S.effective()
            assert eff["source"] == "db", "%r ⇒ %s" % (val, eff["source"])
            assert eff.get("forced") in (None, ""), "不该残留 forced：%r" % eff.get("forced")



@case("优先级：库里没行 ⇒ 回落环境变量")
def _t_env_fallback():
    with env(**_ENV_FULL), fake_db(None):
        eff = S.effective()
        assert eff["source"] == "env", eff["source"]
        assert eff["model"] == "env-model", eff["model"]
        assert llm.config()["source"] == "env"


@case("优先级：读库失败 ⇒ 回落环境变量 + 带 db_error，且不抛异常")
def _t_db_broken():
    with env(**_ENV_FULL), broken_db():
        eff = S.effective()  # 不许抛
        assert eff["source"] == "env" and eff["db_available"] is False, eff
        assert "connection refused" in (eff.get("db_error") or ""), eff.get("db_error")
        assert eff["enabled"] is True, "库挂了不该把可用的大模型一起停掉"
        cfg = llm.config()
        assert cfg["enabled"] is True, "llm.config 也不许被配置库拖死"


@case("优先级：库连不上且无环境变量 ⇒ 未配置，不报错")
def _t_nothing_configured():
    with env(**_NO_ENV), broken_db():
        eff = S.effective()
        assert eff["source"] == "env" and eff["enabled"] is False, eff
        assert set(eff["missing"]) == {"base_url", "api_key", "model"}, eff["missing"]


# ---------------------------------------------------------------------------
# 保存：校验
# ---------------------------------------------------------------------------
@case("保存校验：缺地址/缺模型/开开关无密钥 ⇒ 三条原因齐，且不落库")
def _t_save_errors():
    with fake_db(None):
        r = S.save_settings({"provider": "custom", "base_url": "", "model": "", "api_key": ""})
        assert r["ok"] is False, r
        assert len(r["errors"]) >= 2, r["errors"]
        joined = "；".join(r["errors"])
        assert "接口地址" in joined and "模型名" in joined, joined
        # `error` 必须同时给出：上游统一错误体认的是它，只给 errors 会变成 200 正常返回
        assert r.get("error"), "缺少单句 error，上游会把校验失败当成成功"
        assert S.db.executed == [], "校验不过却写了库：%s" % S.db.executed


@case("保存校验：地址必须是 http(s) 开头，否则拒绝")
def _t_save_bad_url():
    with fake_db(None):
        r = S.save_settings(
            {"provider": "custom", "base_url": "api.example.com/v1", "model": "m", "api_key": "k"}
        )
        assert r["ok"] is False and "http" in r["error"], r


@case("保存校验：http 公网地址给警告（不阻断）")
def _t_save_http_warn():
    with fake_db(None):
        r = S.save_settings(
            {"provider": "custom", "base_url": "http://llm.example.com/v1", "model": "m", "api_key": "k"}
        )
        assert r["ok"] is True, r
        assert any("明文" in w for w in r["warnings"]), r["warnings"]


@case("保存：选了预设但地址留空 ⇒ 自动用预设地址")
def _t_save_preset_default():
    with fake_db(None):
        r = S.save_settings({"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-x"})
        assert r["ok"] is True, r
        assert r["settings"]["base_url"] == "https://api.deepseek.com/v1", r["settings"]["base_url"]


# ---------------------------------------------------------------------------
# 保存：密钥三态
# ---------------------------------------------------------------------------
@case("密钥三态①：api_key 留空 ⇒ 保持库里原值（掩码不可能被回传覆盖）")
def _t_key_keep():
    with fake_db(_row(api_key="sk-original-keep-me")):
        r = S.save_settings({"model": "deepseek-reasoner", "api_key": ""})
        assert r["ok"] is True, r
        assert S.db.row["api_key"] == "sk-original-keep-me", "留空居然改掉了密钥"
        assert "api_key" not in r["changed"], r["changed"]
        assert r["settings"]["model"] == "deepseek-reasoner", r["settings"]["model"]


@case("密钥三态②：clear_credential ⇒ 清空，并如实变成「未配置」")
def _t_key_clear():
    with fake_db(_row()):
        r = S.save_settings({"clear_credential": True, "enabled": False})
        assert r["ok"] is True, r
        assert S.db.row["api_key"] == "", "没清掉"
        assert r["settings"]["has_credential"] is False, r["settings"]
        assert r["settings"]["effective"] is False


@case("密钥三态③：新值 ⇒ 覆盖，changed 里标出来")
def _t_key_replace():
    with fake_db(_row(api_key="sk-old")):
        r = S.save_settings({"api_key": "sk-new-value"})
        assert r["ok"] is True, r
        assert S.db.row["api_key"] == "sk-new-value", S.db.row["api_key"]
        assert "api_key" in r["changed"], r["changed"]


@case("审计：详情只记「换过密钥」，绝不出现密钥原文")
def _t_audit_no_secret():
    with fake_db(_row(api_key="sk-old")):
        S.save_settings({"api_key": "sk-brand-new-secret"})
        inserts = [p for sql, p in S.db.executed if "config_events" in sql]
        assert inserts, "保存没有写审计"
        blob = json.dumps(inserts, ensure_ascii=False)
        assert "sk-brand-new-secret" not in blob, "审计里出现了密钥原文：%s" % blob
        assert '"credential_changed": true' in blob or "credential_changed" in blob, blob


# ---------------------------------------------------------------------------
# 防泄漏（最强断言：整个返回体序列化后搜密钥）
# ---------------------------------------------------------------------------
@case("防泄漏：get_settings() 整个返回体不得包含密钥原文")
def _t_no_leak():
    secret = "sk-super-secret-do-not-leak-1234"
    with env(**_NO_ENV), fake_db(_row(api_key=secret)):
        view = S.get_settings()
        blob = json.dumps(view, ensure_ascii=False)
        assert secret not in blob, "出参泄露了密钥：%s" % blob
        assert view["has_credential"] is True
        assert view["credential_masked"].startswith("sk-"), view["credential_masked"]
        assert view["credential_masked"] != secret
        assert "api_key" not in view, "字段名带 api_key 会被上游按键脱敏，视图里不该有"


@case("视图：未配置时 missing 与 effective 一致，并给出环境变量侧状态")
def _t_view_unconfigured():
    with env(**_NO_ENV), fake_db(None):
        v = S.get_settings(include_presets=False, history=0)
        assert v["configured"] is False and v["effective"] is False, v
        assert set(v["missing"]) == {"base_url", "api_key", "model"}, v["missing"]
        assert v["env_configured"] is False
        assert "providers" not in v, "include_presets=False 仍下发了预设"


@case("视图：环境变量已配时，如实标出 env_configured")
def _t_view_env():
    with env(**_ENV_FULL), fake_db(None):
        v = S.get_settings(include_presets=False, history=0)
        assert v["source"] == "env" and v["env_configured"] is True, v
        assert v["env_model"] == "env-model", v["env_model"]


@case("状态行：三种状态各一句话，且不含地址")
def _t_status_line():
    with env(**_NO_ENV), fake_db(None):
        s = S.status_line()
        assert "未配置" in s and "http" not in s, s
    with env(**_ENV_FULL), fake_db(None):
        s = S.status_line()
        assert "已启用" in s and "env.example.com" not in s, s
        assert "环境变量" in s, s
    with env(**{**_ENV_FULL, "LLM_ENABLED": "0"}), fake_db(None):
        s = S.status_line()
        assert "关闭" in s, s


# ---------------------------------------------------------------------------
# 连通性自测
# ---------------------------------------------------------------------------
@case("测试连接：配置不全 ⇒ 直接拒绝，不发任何请求")
def _t_test_incomplete():
    calls = []

    def fake_chat(messages, **kw):
        calls.append(kw)
        return {"ok": True, "content": "可用"}

    with env(**_NO_ENV), fake_db(None), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({})
        assert r["ok"] is False, r
        assert calls == [], "配置不完整却发了请求：%s" % calls
        assert "不完整" in r["error"], r["error"]


@case("测试连接：配齐后按桩返回，且不把密钥回显出来")
def _t_test_ok():
    seen = {}

    def fake_chat(messages, **kw):
        seen.update(kw)
        return {"ok": True, "content": "可用", "model": "deepseek-chat", "latency_ms": 12}

    with env(**_NO_ENV), fake_db(_row(api_key="sk-live-secret")), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({})
        assert r["ok"] is True, r
        assert r["notice"] and "可以保存" in r["notice"], r["notice"]
        assert r["config"]["credential_masked"] != "sk-live-secret"
        blob = json.dumps(r, ensure_ascii=False)
        assert "sk-live-secret" not in blob, "测试结果里泄露了密钥"
        assert seen.get("timeout"), "必须给超时，否则测试会长时间挂住"
        assert seen.get("max_retries") == 0, "测试不该重试：等两倍时间还是同一个错"


@case("测试连接：草稿里的新密钥优先，并如实告知用的是新输入")
def _t_test_draft_key():
    seen = {}

    def fake_chat(messages, **kw):
        seen.update(kw.get("cfg") or {})
        return {"ok": True, "content": "可用", "latency_ms": 5}

    with env(**_NO_ENV), fake_db(_row(api_key="sk-saved")), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({"api_key": "sk-draft", "model": "deepseek-reasoner"})
        assert r["ok"] is True, r
        assert seen.get("api_key") == "sk-draft", "草稿密钥没被用上"
        assert r["used_saved_credential"] is False, "用了新密钥却说是旧密钥，会让用户误判"


@case("测试连接：草稿留密钥位 ⇒ 用已保存的，并明确标注")
def _t_test_saved_key():
    seen = {}

    def fake_chat(messages, **kw):
        seen.update(kw.get("cfg") or {})
        return {"ok": True, "content": "可用", "latency_ms": 5}

    with env(**_NO_ENV), fake_db(_row(api_key="sk-saved")), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({"api_key": ""})
        assert r["ok"] is True, r
        assert seen.get("api_key") == "sk-saved", seen.get("api_key")
        assert r["used_saved_credential"] is True, "用了已存密钥却没标注"


@case("报错翻译：401/404/429/超时都说清「改哪里」，且不带内部地址")
def _t_diagnose():
    cfg = {"base_url": "https://api.deepseek.com/v1", "model": "x", "api_key": "k"}
    cases = [
        ({"ok": False, "status": 401, "error": "HTTP 401"}, "鉴权", "API Key"),
        ({"ok": False, "status": 404, "error": "HTTP 404"}, "地址", "/v1"),
        ({"ok": False, "status": 429, "error": "HTTP 429"}, "限流", "重试"),
        ({"ok": False, "error": "URLError: timed out"}, "超时", "不通"),
        ({"ok": False, "error": "getaddrinfo failed"}, "域名", "DNS"),
    ]
    for res, kw1, kw2 in cases:
        msg = S._diagnose(res, cfg)
        assert kw1 in msg and kw2 in msg, "%s → %s" % (res, msg)
        assert "9000" not in msg and "wren" not in msg.lower(), "报错里出现了内部地址：%s" % msg


@case("误诊回归：探活已通时的超时，绝不能再说是「地址不通」")
def _t_diagnose_timeout_not_network():
    """2026-11-01 事故的断言：推理模型慢 23 秒被报成「地址不通或网络被拦」。

    这条断言的作用不是「文案好看」，是**防止把人带去查网络**。
    """
    cfg = {"base_url": "http://host.docker.internal:11434/v1", "model": "deepseek-r1:7b", "api_key": "ollama"}
    res = {"ok": False, "error": "TimeoutError: timed out"}
    msg = S._diagnose(res, cfg, {"reachable": True, "model_count": 7, "model_listed": True})
    assert "不通" not in msg and "网络被拦" not in msg, "探活已通却说地址不通：%s" % msg
    assert "慢" in msg, "没指出真正原因（模型慢）：%s" % msg
    assert "超时" in msg, "得告诉人调哪个参数：%s" % msg

    # 没探活 / 探活不通 ⇒ 仍然允许说「地址不通」，这是对的，不能为了改文案把真问题含糊掉
    plain = S._diagnose(res, cfg)
    assert "不通" in plain, "真·连不上时反而不敢说了：%s" % plain


@case("模型名不在清单：报「模型名不对」并给出可选数量，不去怪地址")
def _t_diagnose_model_missing():
    cfg = {"base_url": "http://host.docker.internal:11434/v1", "model": "deepseek-r9:99b", "api_key": "k"}
    msg = S._diagnose({"ok": False, "status": 404}, cfg, {"reachable": True, "model_count": 7, "model_listed": False})
    assert "模型名不对" in msg and "7" in msg, msg


@case("模型清单：地址归一化与模型名匹配")
def _t_models_url_and_match():
    assert llm.models_url("http://h:11434/v1") == "http://h:11434/v1/models"
    assert llm.models_url("http://h:11434/v1/") == "http://h:11434/v1/models"
    assert llm.models_url("http://h:11434/v1/chat/completions") == "http://h:11434/v1/models"
    assert llm.models_url("http://h:11434/v1/models") == "http://h:11434/v1/models"
    assert llm.models_url("") == ""

    pool = ["deepseek-r1:7b", "qwen2.5:1.5b"]
    assert llm.model_listed("deepseek-r1:7b", pool) is True
    assert llm.model_listed("qwen2.5", pool) is True, "省略 tag 应能对上"
    assert llm.model_listed("deepseek-r1:8b", pool) is False, "tag 不同就是不同模型，不能放行"
    assert llm.model_listed("qwen2.5:3b", pool) is False
    assert llm.model_listed("anything", []) is None, "清单为空时无从判断，不许硬说没有"


@case("测试连接失败：三段式——notice 给人话，error 留原始原因供排障")
def _t_test_fail_notice():
    def fake_chat(messages, **kw):
        return {"ok": False, "status": 401, "error": "HTTP 401: invalid api key"}

    with env(**_NO_ENV), fake_db(_row()), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({})
        assert r["ok"] is False, r
        # 用户话术：说清「该改哪里」，不含工具名与内部地址
        assert "鉴权" in (r.get("notice") or ""), r.get("notice")
        # 原始原因：保留（排障唯一线索），但不许夹带地址与密钥
        assert "HTTP 401" in (r.get("error") or ""), "原始原因被吞掉了，排障会瞎"
        blob = json.dumps(r, ensure_ascii=False)
        assert "sk-db-secret-9876" not in blob, "回吐了密钥"
        assert "api.deepseek.com" not in (r.get("notice") or ""), "用户话术里出现了地址"


@case("测试连接：探活不通 ⇒ 立即返回，不再等一次生成（否则多等一整个超时）")
def _t_test_probe_blocks_chat():
    calls = []

    def fake_chat(messages, **kw):
        calls.append(kw)
        return {"ok": False, "error": "TimeoutError: timed out"}

    def fake_models(cfg, timeout=None):
        return {"ok": False, "error": "URLError: Connection refused", "models": []}

    with env(**_NO_ENV), fake_db(_row()), patch_chat(fake_chat), patch_models(fake_models):
        r = S.test_connection({})
        assert r["ok"] is False, r
        assert calls == [], "地址都不通还去等生成，用户要多等一整个超时"
        assert r["probe"]["reachable"] is False, r
        assert "端口" in (r.get("notice") or "") or "连不上" in (r.get("notice") or ""), r.get("notice")


@case("测试连接：地址没有 /models 接口（404）时不许判死，要继续用生成验证")
def _t_test_models_404_not_fatal():
    """有些自建代理/网关只实现 chat/completions，不实现 /models。

    这一类必须放行到第二段：拿不到清单 ≠ 不能生成。
    否则「昨天还能测通」的配置，今天升级后变成「地址不对」。
    """
    calls = []

    def fake_chat(messages, **kw):
        calls.append(kw)
        return {"ok": True, "content": "可用", "latency_ms": 9}

    def fake_models(cfg, timeout=None):
        return {"ok": False, "status": 404, "error": "HTTP 404: not found", "models": []}

    with env(**_NO_ENV), fake_db(_row()), patch_chat(fake_chat), patch_models(fake_models):
        r = S.test_connection({})
        assert len(calls) == 1, "404 应当放行到生成验证，实际调用次数 %d" % len(calls)
        assert r["ok"] is True, r
        assert r["probe"]["conclusive"] is False, r["probe"]


@case("报错翻译：超时且探活无定论 ⇒ 说明「无法先确认可达性」，不假装确定")
def _t_diagnose_timeout_inconclusive():
    cfg = {"base_url": "https://proxy.example/v1", "model": "m", "api_key": "k"}
    msg = S._diagnose(
        {"ok": False, "error": "TimeoutError: timed out"},
        cfg,
        {"reachable": False, "conclusive": False},
    )
    assert "无法先单独确认可达性" in msg, msg


@case("测试连接：探活通、生成超时 ⇒ 结论是「模型慢」，不是「连接失败」")
def _t_test_slow_model():
    def fake_chat(messages, **kw):
        return {"ok": False, "error": "TimeoutError: timed out"}

    with env(**_NO_ENV), fake_db(_row(model="deepseek-r1:7b", timeout=90)), patch_chat(fake_chat), patch_models(_models_ok):
        r = S.test_connection({})
        assert r["ok"] is False, "没真出内容就不能算通过"
        assert r["probe"]["reachable"] is True and r["probe"]["model_listed"] is True, r["probe"]
        assert "慢" in (r.get("notice") or ""), r.get("notice")
        assert "不通" not in (r.get("notice") or ""), "又误诊成网络问题了：%s" % r.get("notice")


@case("测试连接：超时跟随配置，不再写死 20 秒")
def _t_test_timeout_follows_config():
    seen = {}

    def fake_chat(messages, **kw):
        seen["timeout"] = kw.get("timeout")
        return {"ok": True, "content": "可用", "latency_ms": 5}

    with env(**_NO_ENV), fake_db(_row(timeout=90)), patch_chat(fake_chat), patch_models(_models_ok):
        S.test_connection({})
        assert seen["timeout"] == 90, "页面上填 90，测试却按 %s 秒判死" % seen["timeout"]

    # 显式传 0 表示「没指定」⇒ 用配置值，不能被 _num 的下限夹成 3 秒
    with env(**_NO_ENV), fake_db(_row(timeout=45)), patch_chat(fake_chat), patch_models(_models_ok):
        S.test_connection({}, timeout=0)
        assert seen["timeout"] == 45, seen["timeout"]

    # 显式传值优先于配置
    with env(**_NO_ENV), fake_db(_row(timeout=45)), patch_chat(fake_chat), patch_models(_models_ok):
        S.test_connection({}, timeout=120)
        assert seen["timeout"] == 120, seen["timeout"]


# ---------------------------------------------------------------------------
# 写入即生效 / 缓存
# ---------------------------------------------------------------------------
@case("保存即生效：不用等缓存过期，写完立刻能读到新模型")
def _t_save_immediate():
    with fake_db(_row(model="deepseek-chat")):
        assert S.effective()["model"] == "deepseek-chat"
        r = S.save_settings({"model": "deepseek-reasoner"})
        assert r["ok"] is True, r
        assert S.effective()["model"] == "deepseek-reasoner", "保存后仍读到旧值（缓存没失效）"
        assert llm.config()["model"] == "deepseek-reasoner"


@case("缓存：TTL 内复用，不重复读库")
def _t_cache():
    hits = {"n": 0}

    class Counting(FakeDB):
        def query_one(self, sql, params=None):
            hits["n"] += 1
            return dict(self.row) if self.row else None

    old = S.db
    S.db = Counting(_row())
    S.invalidate()
    try:
        S.effective()
        S.effective()
        S.effective()
        assert hits["n"] == 1, "TTL 内重复读库 %d 次" % hits["n"]
        S.invalidate()
        S.effective()
        assert hits["n"] == 2, "invalidate 后没有重新读库"
    finally:
        S.db = old
        S.invalidate()


@case("恢复默认：删行 ⇒ 回落环境变量（不是留一条空配置）")
def _t_reset():
    with env(**_ENV_FULL), fake_db(_row()):
        assert S.effective()["source"] == "db"
        r = S.reset_settings(actor="R4")
        assert r["ok"] is True, r
        assert S.db.row is None, "reset 没删行"
        assert S.effective()["source"] == "env", "reset 后应回落环境变量"
        assert "环境变量" in r["notice"], r["notice"]


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
    print("\nllm_settings_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
