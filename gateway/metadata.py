# -*- coding: utf-8 -*-
"""元数据字典（M2 · PRD §10.2 / 最终技术方案 §4.1）

两张来源合流
------------
| 来源 | 提供什么 | 采集方式 |
|---|---|---|
| **MDL 语义层** | 中文名、口径、颗粒度、关联关系（**AI 可见闭集**） | 直接读 `mdl.json`，`seed_from_mdl()` |
| **物理库结构** | 数据类型、可空性、真实字段全集 | SchemaCrawler 采集，`import_physical()` |

为什么要合流：MDL 里**没有**的字段，是项目刻意不对 AI 开放的（敏感字段未建模）。
只看 MDL 会以为"表就这些列"；只看物理库会把敏感列当成普通列。合流后才能回答
「这张表有哪些列、哪些列对 AI 可见、为什么不可见」。

按表查中文名与口径（M2 验收标准之一）
-------------------------------------
`lookup(table="dwd_order_di", dataset="B")` 返回表中文名、颗粒度、字段清单
（含中文名 / 口径 / 类型 / 是否对 AI 可见 / 敏感标记）与关联路径。
"""
import re

import db
import registry

# 口径分隔符：MDL description 里用「；口径：」把"中文名(单位)"与"计算口径"分开
CALIBER_MARKERS = ["；口径：", "; 口径：", "；口径:", "口径："]

# 括号类兜底：MDL 里也常见"中文名（备注）"这种非标准写法，括号内当 caliber 草稿
PAREN_MARKERS_L = [("（", "）"), ("(", ")")]

# 时间类型白名单（PostgreSQL + MDL 缩写）——这些类型的列才允许被当 time_field
TIME_TYPE_WHITELIST = {
    "date", "timestamp", "timestamptz", "timestamp without time zone",
    "timestamp with time zone", "time", "timetz", "time without time zone",
    "time with time zone", "interval",
    "DATE", "TIMESTAMP", "TIMESTAMPTZ", "TIME", "INTERVAL",
}

# 黑词：label/name 里只要出现这些且不是真正时间描述 → 直接排除 time_field 候选
TIME_FIELD_NAME_BLACKLIST = [
    # 主键/ID 类
    "_id", "_ID", "id", "ID", "编号", "编码", "代号", "主键", "序列",
    # 金额/数值类
    "元", "￥", "$", "金额", "价格", "成本", "费用", "收入", "利润",
    "销量", "数量", "件数", "份数", "比例", "率", "折扣",
    # 枚举/状态类
    "类型", "状态", "级别", "等级", "分类", "标签", "标志", "标识", "说明", "渠道", "来源",
    # 主体类（member_id/store_id 最容易被"月份"字样误伤）
    "会员", "门店", "商品", "订单", "用户", "客户", "店铺",
]
# 主体类黑词需要与"是否确实是时间列"组合判断，不全杀
TIME_FIELD_STRONG_BLACK = {"_id", "_ID", "编号", "编码", "代号", "主键"}

# 时间描述正词（辅助判断——有正词可以减弱**弱黑词**，不能单独用来放行列）
#
# 为什么一个单字都不留（P1-12 事故根因）
# -------------------------------------
# 早先这里放了单字「日 / 时 / 分 / 秒 / 周」，中文子串命中几乎没有门槛：
#   · "…便于免 JOIN 直接分析"      —— 含「分」→ 當作时间词
#   · "当日销售额 / 当日订单数"      —— 含「日」→ 金额列被放行
#   · "日期类型；枚举：工作日/周末/节假日" —— 含「周」→ 枚举列被放行
# 实测后果：真需求的 time_candidates_filtered = 0（硬判据一列都没拦住），
# A 库 products.category_name 这种纯英文思念也"通过"。所以正词一律 ≥2 字。
TIME_FIELD_POSITIVE_WORDS = {
    "日期", "时间", "月份", "月度", "季度", "年度", "年份", "年月",
    "星期", "会计期", "财年", "财月", "期间",
}

# 英文时间词走词边界正则，避免 "update_by" 里的 date 子串误命中。
# 不能用 \b —— "order_date" 里 `_` 也是词字符，\bdate 匹配不到；这里显式把
# 字母/数字/下划线都排除在边界之外，让 order_date / created_at 能正确识别。
_ASCII_TIME_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(date|datetime|dt|time|timestamp|month|year|period|quarter|day)"
    r"(?![A-Za-z0-9])", re.I
)


def split_label_caliber(desc):
    """把 MDL 的 description 拆成 (中文名, 口径)。

    为什么要做兜底切分
    ------------------
    MDL 实际写作时存在非标准格式：
      - "会员注册时间（按订单完成日期取最早一条）"——用中文括号当口径分隔
      - "订单月份 (统计维度：按月)"——用英文括号
    无「口径：」标记时，若检测到成对括号，括号内内容也算 caliber。
    这样 P-B 里把"月份"+"member_id"标签错混成时间列的几率会大幅下降，
    因为括号里若写着"会员维度唯一编码"，这些黑词会被正确识别到 caliber 里。
    """
    if not desc:
        return None, None
    text = desc.strip()
    # 优先走正式口径分隔符
    for mk in CALIBER_MARKERS:
        if mk in text:
            head, tail = text.split(mk, 1)
            return head.strip(), tail.strip()
    # 兜底：成对括号
    caliber_parts = []
    label = text
    for lp, rp in PAREN_MARKERS_L:
        if lp in label and rp in label:
            li = label.index(lp)
            ri = label.rindex(rp)
            if ri > li:
                caliber_parts.append(label[li + 1:ri].strip())
                label = (label[:li] + label[ri + 1:]).strip()
    if caliber_parts:
        return label, "；".join(p for p in caliber_parts if p)
    return label, None


def get_column_meta(dataset, table_name, column_name):
    """查 column_docs 返回 {data_type, is_primary_key, column_label}。

    纯查库函数；查不到就返回空字段，不抛错。
    """
    try:
        r = db.query_one(
            """
            SELECT data_type, is_primary_key, column_label
            FROM column_docs
            WHERE dataset=%s AND table_name=%s AND column_name=%s
            """,
            (dataset, table_name, column_name),
        )
        if r:
            return {
                "data_type": (r.get("data_type") or "").strip(),
                "is_primary_key": bool(r.get("is_primary_key")),
                "column_label": (r.get("column_label") or "").strip(),
            }
    except Exception:
        pass
    return {"data_type": "", "is_primary_key": False, "column_label": ""}


def _is_time_blacklisted_by_name(name_or_label, strong_only=False):
    s = str(name_or_label or "")
    for w in TIME_FIELD_STRONG_BLACK:
        if w in s:
            return True, "strong_black(%s)" % w
    if strong_only:
        return False, None
    # 主体类 + 非时间列：member_id / 门店名称 这种需要更小心
    any_black = False
    why = None
    for w in TIME_FIELD_NAME_BLACKLIST:
        if w in s:
            any_black = True
            why = "weak_black(%s)" % w
            break
    if any_black:
        # 如果同时有明确的日期/时间正词，弱黑词不杀——否则「会员注册月份」这种
        # 会因为含「会员」被误杀。但主键 ID 等强黑词已在上方杀完。
        for p in TIME_FIELD_POSITIVE_WORDS:
            if p in s:
                return False, None
        return True, why
    return False, None


def _has_positive_time_word(*texts):
    """是否出现明确的时间正词（中文 >=2 字词，或英文独立时间单词）。"""
    for t in texts:
        s = str(t or "")
        for p in TIME_FIELD_POSITIVE_WORDS:
            if p in s:
                return True
        if _ASCII_TIME_WORD_RE.search(s):
            return True
    return False


def is_eligible_time_field(locator, dataset="B", label_hint=None,
                           data_type_hint=None, is_pk_hint=None):
    """TIME_FIELD 硬判据入口：综合类型 + 语义 + 名字三层过滤。

    返回 (eligible, reason)；reason 用于 time_field_reason 的可解释性。
    """
    # locator = "table.column"
    if "." not in locator:
        return False, "locator 格式不符（需 table.column）：%s" % locator
    table, col = locator.rsplit(".", 1)

    meta = get_column_meta(dataset, table, col)
    data_type = (data_type_hint or meta.get("data_type") or "").strip()
    is_pk = bool(is_pk_hint if is_pk_hint is not None else meta.get("is_primary_key"))
    label = label_hint or meta.get("column_label") or col

    reasons = []

    # 1. 主键强杀——但只杀「非时间类型的主键」：stat_month/stat_date 这种时间分区列
    #    是复合主键之一，不该被杀。真正应该被杀的是 member_id 等 ID 型主键。
    #    判据：pk=True 且 (列名含强黑词 或 类型不在时间白名单)
    if is_pk:
        pk_nb, _ = _is_time_blacklisted_by_name(col, strong_only=True)
        norm_type = (data_type.split("(")[0].strip().lower()) if data_type else ""
        pk_type_ok = any(norm_type == wt.lower() for wt in TIME_TYPE_WHITELIST) if norm_type else False
        if pk_nb or not pk_type_ok:
            return False, "PK 排除：%s 是 ID 型或非时间型主键（col=%s type=%s）" % (
                locator, col, data_type or "未知"
            )
        else:
            reasons.append("复合主键时间列（pk=True 但属于时间分区列，不杀）")
    # 2. 名字强黑词（_id / 编号 / 编码 / 主键）——只看列名本身，不把 column_label
    #    里的"表主键之一""关联 xx 主键（ID）"这类人类描述也当黑词杀。
    nb, why_nb = _is_time_blacklisted_by_name(col, strong_only=True)
    if nb:
        return False, "名称黑词排除：%s @ %s" % (why_nb, locator)
    # 3. 类型白名单 —— 这是 P-B 最核心的硬门槛
    type_ok = False
    if data_type:
        # 规范化比较：去掉括号、空白
        norm = data_type.split("(")[0].strip().lower()
        for wt in TIME_TYPE_WHITELIST:
            if norm == wt.lower():
                type_ok = True
                reasons.append("type=%s" % data_type)
                break
    # 3. 类型白名单是**硬门槛**：不在白名单里直接排除。
    #    P1-12 修掉了原来「正词命中就暂放待复核」的弱通路——那条通路让
    #    "当日销售额"（numeric）、"日期类型"（varchar 枚举）这类列一路绿灯。
    if not type_ok:
        return False, "类型未命中时间白名单：data_type=%s, label=%s" % (
            data_type or "未知", label
        )
    # 4. 弱黑词最后一道（金额/枚举/类型 列即便时间格式也杀；明确时间正词可抵消）
    wb, why_wb = _is_time_blacklisted_by_name(label, strong_only=False)
    if wb and not _has_positive_time_word(col, label):
        return False, "弱语义黑词排除：%s @ %s（label=%s）" % (why_wb, locator, label)
    return True, "；".join(reasons) or "通过"


# ---------------------------------------------------------------------------
# 播种：MDL → 字典
# ---------------------------------------------------------------------------
def seed_from_mdl(dataset_key):
    """把 MDL 的语义（中文名/口径/颗粒度）写入字典。幂等：同键覆盖语义列，不动物理列。"""
    ds = registry.get(dataset_key)
    mdl = ds.mdl
    models = mdl.get("models", [])
    relationships = mdl.get("relationships", [])

    n_t, n_c = 0, 0
    for m in models:
        tname = m.get("name")
        cols = m.get("columns", [])
        # 表级：颗粒度由主键推定。MDL 的 primaryKey 既可能是字符串（单主键），
        # 也可能是数组（复合主键）——统一成列表，否则 "order_id" 会被逐字符拆开
        pk_raw = m.get("primaryKey")
        pk = [pk_raw] if isinstance(pk_raw, str) else list(pk_raw or [])
        grain = "一行 = 一条 %s 记录（主键 %s）" % (tname, "、".join(pk) if pk else "未声明")
        desc_parts = [
            c.get("description", "") for c in cols[:3] if c.get("description")
        ]
        db.execute(
            """
            INSERT INTO table_docs (dataset, table_name, table_label, domain, grain, description, source)
            VALUES (%s,%s,%s,%s,%s,%s,'mdl')
            ON CONFLICT (dataset, table_name) DO UPDATE SET
                table_label = COALESCE(EXCLUDED.table_label, table_docs.table_label),
                domain      = COALESCE(EXCLUDED.domain, table_docs.domain),
                grain       = EXCLUDED.grain,
                description = COALESCE(EXCLUDED.description, table_docs.description)
            """,
            (
                dataset_key,
                tname,
                ds.label + " · " + tname,
                ds.domain,
                grain,
                "字段摘要：" + "；".join(desc_parts)[:300],
            ),
        )
        n_t += 1
        for c in cols:
            label, caliber = split_label_caliber(c.get("description"))
            db.execute(
                """
                INSERT INTO column_docs
                    (dataset, table_name, column_name, column_label, data_type, nullable,
                     is_sensitive, is_primary_key, description, source)
                VALUES (%s,%s,%s,%s,%s,%s,false,%s,%s,'mdl')
                ON CONFLICT (dataset, table_name, column_name) DO UPDATE SET
                    column_label = EXCLUDED.column_label,
                    data_type    = COALESCE(EXCLUDED.data_type, column_docs.data_type),
                    is_primary_key = EXCLUDED.is_primary_key,
                    is_sensitive = false,
                    description  = EXCLUDED.description,
                    source       = CASE WHEN column_docs.source = 'schemacrawler'
                                        THEN 'mdl+schemacrawler' ELSE 'mdl' END
                """,
                (
                    dataset_key,
                    tname,
                    c.get("name"),
                    label,
                    c.get("type"),
                    True,  # MDL 未声明 nullable，占位为 True，物理采集会覆盖
                    bool(c.get("isPrimaryKey") or c.get("name") in pk),
                    c.get("description"),
                ),
            )
            n_c += 1

    # 关联关系写入表级 description 尾部，便于按表查关系
    rel_by_table = {}
    for r in relationships:
        cond = r.get("condition", "")
        for t in r.get("models", []):
            rel_by_table.setdefault(t, []).append(
                "%s(%s)" % (r.get("name"), r.get("joinType", ""))
            )
    for t, rels in rel_by_table.items():
        db.execute(
            "UPDATE table_docs SET description = COALESCE(description,'') || %s "
            "WHERE dataset=%s AND table_name=%s",
            (" ｜ 关联：" + "、".join(rels[:6]), dataset_key, t),
        )
    return {"dataset": dataset_key, "tables": n_t, "columns": n_c, "relationships": len(relationships)}


# ---------------------------------------------------------------------------
# 导入：SchemaCrawler → 字典
# ---------------------------------------------------------------------------
def import_physical(dataset_key, tables):
    """导入物理结构。

    tables 形如：
      [{"table_name": "...", "row_estimate": 123, "comment": "...",
        "columns": [{"name","type","nullable","default","comment","primary_key"}]}, ...]

    **关键判定**：物理存在但 MDL 未建模的列 → 标记 `is_sensitive = true`，
    并在 description 注明「未建模：对 AI 不可见」。这是项目「敏感字段不建模」
    约定的元数据侧体现，也是 §9.2 入口风控的字典依据。
    """
    ds = registry.get(dataset_key)
    visible = set()
    for m in ds.mdl.get("models", []):
        for c in m.get("columns", []):
            visible.add((m.get("name"), c.get("name")))

    n_t, n_c, n_hidden = 0, 0, 0
    for t in tables:
        tname = t["table_name"]
        db.execute(
            """
            INSERT INTO table_docs (dataset, table_name, row_estimate, collected_at, source)
            VALUES (%s,%s,%s,now(),'schemacrawler')
            ON CONFLICT (dataset, table_name) DO UPDATE SET
                row_estimate = COALESCE(EXCLUDED.row_estimate, table_docs.row_estimate),
                collected_at = now(),
                source = CASE WHEN table_docs.source LIKE '%%mdl%%'
                              THEN 'mdl+schemacrawler' ELSE 'schemacrawler' END
            """,
            (dataset_key, tname, t.get("row_estimate")),
        )
        n_t += 1
        for c in t.get("columns", []):
            cname = c.get("name")
            hidden = (tname, cname) not in visible
            note = c.get("comment")
            if hidden:
                n_hidden += 1
                note = (note + " " if note else "") + "【未建模：对 AI 不可见】"
            db.execute(
                """
                INSERT INTO column_docs
                    (dataset, table_name, column_name, data_type, nullable, is_sensitive,
                     is_primary_key, description, collected_at, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),'schemacrawler')
                ON CONFLICT (dataset, table_name, column_name) DO UPDATE SET
                    data_type    = EXCLUDED.data_type,
                    nullable     = EXCLUDED.nullable,
                    is_primary_key = EXCLUDED.is_primary_key,
                    is_sensitive = CASE WHEN column_docs.source LIKE '%%mdl%%'
                                        THEN column_docs.is_sensitive
                                        ELSE EXCLUDED.is_sensitive END,
                    description  = COALESCE(column_docs.description, EXCLUDED.description),
                    collected_at = now(),
                    source = CASE WHEN column_docs.source LIKE '%%mdl%%'
                                  THEN 'mdl+schemacrawler' ELSE 'schemacrawler' END
                """,
                (
                    dataset_key,
                    tname,
                    cname,
                    c.get("type"),
                    c.get("nullable"),
                    hidden,
                    bool(c.get("primary_key") or c.get("primaryKey")),
                    note,
                ),
            )
            n_c += 1
    return {
        "dataset": dataset_key,
        "tables": n_t,
        "columns": n_c,
        "not_modelled_hidden": n_hidden,
    }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def lookup(table=None, dataset=None, keyword=None, include_hidden=False):
    """按表或关键词查元数据字典。

    - `table` 给定时：返回该表的表级信息 + 字段清单（默认**只返回对 AI 可见的列**，
      `include_hidden=True` 才带出未建模列，且明确标注其对 AI 不可见）。
    - `keyword` 给定时：在表名/中文名/字段名/中文名/口径里模糊匹配。
    """
    params, where = [], []
    if dataset:
        params.append(dataset)
        where.append("dataset = %s")
    if table:
        params.append(table)
        where.append("table_name = %s")
    if keyword:
        params.append("%" + keyword + "%")
        where.append(
            "(table_name ILIKE %s OR COALESCE(table_label,'') ILIKE %s "
            "OR COALESCE(description,'') ILIKE %s)"
        )
        params.extend(["%" + keyword + "%"] * 2)

    sql = "SELECT * FROM table_docs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY dataset, table_name"
    tables = db.query(sql, params)

    out = []
    for t in tables:
        cwhere = ["dataset = %s", "table_name = %s"]
        cparams = [t["dataset"], t["table_name"]]
        if not include_hidden:
            cwhere.append("NOT (COALESCE(description,'') LIKE '%%未建模：对 AI 不可见%%')")
        cols = db.query(
            "SELECT column_name, column_label, data_type, nullable, is_sensitive, "
            "       is_primary_key, description, source "
            "FROM column_docs WHERE %s ORDER BY is_primary_key DESC, column_name"
            % " AND ".join(cwhere),
            cparams,
        )
        for c in cols:
            c["ai_visible"] = "未建模：对 AI 不可见" not in (c.get("description") or "")
        out.append(
            {
                "dataset": t["dataset"],
                "table_name": t["table_name"],
                "table_label": t["table_label"],
                "domain": t["domain"],
                "grain": t["grain"],
                "row_estimate": t["row_estimate"],
                "source": t["source"],
                "columns": cols,
                "column_count": len(cols),
                "hidden_column_count": sum(1 for c in cols if not c["ai_visible"]),
            }
        )
    return {"ok": True, "returned": len(out), "tables": out}


def glossary(term=None, keyword=None, dataset=None):
    """业务口径词表查询。"""
    where, params = [], []
    if term:
        where.append("term = %s")
        params.append(term)
    if keyword:
        where.append("(term ILIKE %s OR definition ILIKE %s)")
        params.extend(["%" + keyword + "%"] * 2)
    if dataset:
        where.append("(dataset = %s OR dataset IS NULL)")
        params.append(dataset)
    sql = "SELECT term, definition, formula, dataset, source FROM business_glossary"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY term"
    return {"ok": True, "terms": db.query(sql, params)}


def upsert_glossary(term, definition, formula=None, dataset=None, source="manual"):
    db.execute(
        """
        INSERT INTO business_glossary (term, definition, formula, dataset, source, updated_at)
        VALUES (%s,%s,%s,%s,%s,now())
        ON CONFLICT (term) DO UPDATE SET
            definition = EXCLUDED.definition,
            formula    = COALESCE(EXCLUDED.formula, business_glossary.formula),
            dataset    = COALESCE(EXCLUDED.dataset, business_glossary.dataset),
            source     = EXCLUDED.source,
            updated_at = now()
        """,
        (term, definition, formula, dataset, source),
    )
    return {"ok": True, "term": term}


def seed_glossary_from_mdl():
    """从 MDL 的字段口径里抽取含"口径："的条目，播种业务口径词表。"""
    n = 0
    for key in registry.DATASETS:
        ds = registry.get(key)
        for m in ds.mdl.get("models", []):
            for c in m.get("columns", []):
                _label, caliber = split_label_caliber(c.get("description"))
                if caliber and len(caliber) >= 6:
                    upsert_glossary(
                        term="%s.%s" % (m.get("name"), c.get("name")),
                        definition=caliber,
                        dataset=key,
                        source="mdl",
                    )
                    n += 1
    return {"seeded": n}
