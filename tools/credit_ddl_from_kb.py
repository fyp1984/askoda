# -*- coding: utf-8 -*-
"""从《新信贷取数知识库》工作簿生成 PostgreSQL 建表脚本

为什么要有这个脚本
------------------
知识库里的字段定义有 583 行、分布在 25 张表，而且有三类脏数据必须机器处理：
  ① 3 张表存在重复字段行（同一字段登记两遍，第二遍多填了外键信息）
  ② 178 个字段「数据类型」列是空的（恰好覆盖 BUSINESS_DUEBILL / ACCT_LOAN 等核心表）
  ③ 个别行的列整体错位（REPORTDATE 的「类型」写到了「业务含义」列）
手工抄一遍必然出错，而抄错一个字段名就是一次静默失效——MDL 里写了库里没有的列，
L2 静态规则会跳过该列且不报错。所以一律从工作簿生成。

处理原则
--------
- 重复字段：保留信息量最大的那一行（有类型 > 有外键 > 先出现）
- 空类型：按命名惯例推断，推断结果在脚本末尾单独汇总，便于人工复核
- 表名含点（DWD.xxx / DWM.xxx）：落到同名 schema，保留源系统分层
- 不建物理外键：源系统是 Oracle 贴源层，关系表里有拼写错误与「条件写在字段格里」的行，
  照抄会建出错误的约束；改为写进列注释（FK→表.字段）
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import re
import sys

try:
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None


# ---------------------------------------------------------------- 类型映射

_DECLARED = re.compile(r"^(VARCHAR2?|CHAR)\s*\(\s*(\d+)\s*\)$", re.I)
_DEC_NUM = re.compile(r"^(NUMBER|DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)$", re.I)
_SIMPLE = {
    "STRING": "text",
    "DATE": "date",
    "DATETIME": "timestamp",
    "TIMESTAMP": "timestamp",
    "INTEGER": "integer",
    "INT": "integer",
    "BIGINT": "bigint",
}

# 同一张业务表在知识库里被拆成两条表名登记的情况（02 的行数太多，作者分两段写）
# RPT_LOAN_DTL(56 列) 与 DWM.RPT_LOAN_DTL(75 列) 中文名都是「贷款明细报表宽表」，
# 无公共键、列不重叠 ⇒ 是同一张宽表的两批列，合并为 131 列。
# 不合并会得到一张没有借据号的孤表，任何 SQL 都连不上。
MERGE_INTO = {
    "rpt_loan_dtl": ("dwm", "rpt_loan_dtl"),
}

# 知识库里登记不足、但业务规则要求存在的列（补列并在注释里标明来源规则）
EXTRA_COLUMNS = {
    "public.code_library": [
        ("colname", "varchar(64)", "码值对应的字段名（05_业务规则库 RULE_018 的标准查法要求）"),
        ("coleditsource", "varchar(400)", "码值编辑来源/可选值串（同上）"),
    ],
}

# 复合主键：02 只标了单列 PK，但业务粒度是联合键，按 01_表知识库 的粒度定义补齐
COMPOSITE_PK = {
    "public.customer_fsrecord": ["recordno", "reportdate"],
    "public.code_library": ["codeno", "itemno"],
    # 事实表粒度是「借据 × 快照日」。01 只写了业务键 SYSCGY+DBLNO，照抄会让两个快照日
    # 的同一条借据撞主键，装载历史数据直接失败，故把 dp_load_dt 提进主键。
    "dwd.dwd_lan_dbl_fcti": ["dp_load_dt", "syscgy", "dblno"],
    "dwm.rpt_loan_dtl": ["dblno", "dp_load_dt"],
}

# 源系统把日期存成 VARCHAR2(10)（Oracle 贴源习惯）。建成 date 是为了让
# 「按放款月份汇总」「逾期天数计算」这类需求能直接写，不必层层 to_date。
_AS_DATE = {"DP_LOAD_DT", "DATA_DT", "DATADT"}

# 命名兜底规则的顺序很关键：先日期，再金额（BALANCE 含 BAL 但也含 ANCE，
# 不会误判为日期），最后编号/文本
_DATE_SUFFIX = ("DATE", "DT", "TIME", "DATE_TIME")
_AMOUNT_HINT = ("AMT", "AMOUNT", "SUM", "BAL", "BALANCE", "VALUE", "PRICE", "PROPORTION")
# 本金/欠息/罚息在信贷库是金额列，但命名里没有 AMT/BAL，只能按后缀认
_AMOUNT_SUFFIX = ("PNP", "PYINT", "DBTINT", "INTBAL")
_RATE_HINT = ("RATE",)
_COUNT_SUFFIX = ("DAYS", "CNT", "COUNT", "NUM")
_TEXT_HINT = ("ID", "NO", "CODE", "CD", "CGY", "TYP", "TYPE", "STS", "STATUS",
              "NM", "NAME", "FLG", "FLAG", "DESC")


def infer_type(col: str) -> tuple[str, str]:
    """按命名推断类型，返回 (pg_type, 依据说明)"""
    c = col.upper()
    if _looks_date(col):
        return "date", "命名日期"
    if c.endswith(_COUNT_SUFFIX):
        return "integer", "命名计数"
    for h in _RATE_HINT:
        if h in c:
            return "numeric(20,6)", "命名利率"
    for h in _AMOUNT_HINT:
        if h in c:
            return "numeric(20,2)", "命名金额"
    for suf in _AMOUNT_SUFFIX:
        if c.endswith(suf):
            return "numeric(20,2)", "命名本金/罚息"
    for h in _TEXT_HINT:
        if c.endswith(h):
            return "varchar(64)", "命名编号/文本"
    return "varchar(200)", "命名兜底"


def _looks_date(col: str) -> bool:
    c = col.upper()
    return c.endswith(_DATE_SUFFIX) or c in _AS_DATE or "MATURITY" in c


def map_type(raw: str, col: str) -> tuple[str, str]:
    """原始类型 → (pg_type, 来源)。来源用于统计「哪些是推断出来的」"""
    t = (raw or "").strip()
    as_date = _looks_date(col)
    if t:
        m = _DECLARED.match(t)
        if m:
            n = int(m.group(2))
            if as_date and n <= 10:
                return "date", "声明为字符但按日期存"
            return "varchar(%s)" % m.group(2), "声明"
        m = _DEC_NUM.match(t)
        if m:
            p, s_ = m.group(2), m.group(3)
            return ("numeric(%s,%s)" % (p, s_)) if s_ else ("numeric(%s)" % p), "声明"
        if t.upper() in _SIMPLE:
            if as_date:
                return "date", "声明为字符但按日期存"
            return _SIMPLE[t.upper()], "声明"
        # 声明了但不像类型（例如错位写进来的 'Y'），继续走推断
    return infer_type(col)


# ---------------------------------------------------------------- 读取工作簿

def load_sheet(wb, name: str) -> list[dict]:
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for r in rows[1:]:
        if all(c is None or str(c).strip() == "" for c in r):
            continue
        out.append(dict(zip(hdr, r)))
    return out


def s(v) -> str:
    return "" if v is None else str(v).strip()


def split_table(raw: str) -> tuple[str, str]:
    """DWD.DWD_LAN_DBL_FCTI → ('dwd', 'dwd_lan_dbl_fcti')"""
    raw = s(raw)
    if "." in raw:
        schema, table = raw.split(".", 1)
        return schema.lower(), table.lower()
    return "public", raw.lower()


def parse_key(raw: str, cols: set[str]) -> list[str]:
    """把 01 的「主键字段」解析成列名清单；解析不出来就返回空"""
    raw = s(raw)
    if not raw or "见 02" in raw or "见02" in raw:
        return []
    cands = re.split(r"[+＋、,，/]|（|）|\(|\)", raw)
    out = []
    for c in cands:
        c = re.sub(r"[（(].*", "", c).strip().upper()
        if not c or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", c):
            continue
        if c.lower() not in cols:
            return []          # 只要有一个对不上就整体放弃，避免建出假主键
        out.append(c.lower())
    return out


# ---------------------------------------------------------------- 生成

def build(xlsx: str) -> tuple[str, dict]:
    if openpyxl is None:
        raise SystemExit("openpyxl 不可用")

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    tables_meta = {s(r.get("表英文名")): r for r in load_sheet(wb, "01_表知识库")}
    fields = load_sheet(wb, "02_字段知识库")
    rels = load_sheet(wb, "03_表关联知识库")

    # ---- 按表聚字段，去重（保留信息量最大的行）
    by_table: dict[str, dict[str, dict]] = {}
    for r in fields:
        raw_t = s(r.get("表英文名"))
        col = s(r.get("字段英文名"))
        if not raw_t or not col:
            continue
        schema, table = split_table(raw_t)
        if table in MERGE_INTO:
            schema, table = MERGE_INTO[table]
        key = "%s.%s" % (schema, table)
        score = (1 if s(r.get("数据类型")) else 0) + (1 if s(r.get("外键参照表")) else 0)
        bucket = by_table.setdefault(key, {})
        prev = bucket.get(col.upper())
        if prev is None or score > prev["_score"]:
            bucket[col.upper()] = dict(r, _score=score, _schema=schema, _table=table, _raw_t=raw_t)

    # ---- 关系：建成 「表.字段 → 表.字段」的注释索引
    # 注意：03_表关联知识库 的「左/右关联字段」既有纯字段名，也有全限定名
    # （CUSTOMER_INFO.CUSTOMERID），必须统一剥到最后一个点之后，否则一条都匹配不上。
    def _col_of(raw: str) -> str:
        return (raw or "").strip().split(".")[-1].strip()

    rel_list: list[tuple[str, str, str, str, str]] = []
    for r in rels:
        lt, rt = s(r.get("左表英文名")), s(r.get("右表英文名"))
        lf, rf = _col_of(s(r.get("左关联字段"))), _col_of(s(r.get("右关联字段")))
        if not (lt and rt and lf and rf):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", lf) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rf):
            continue
        ls, ltab = split_table(lt)
        rs, rtab = split_table(rt)
        if ltab in MERGE_INTO:
            ls, ltab = MERGE_INTO[ltab]
        if rtab in MERGE_INTO:
            rs, rtab = MERGE_INTO[rtab]
        rel_list.append(("%s.%s" % (ls, ltab), lf.lower(),
                         "%s.%s" % (rs, rtab), rf.lower(), s(r.get("基数"))))

    # 列注释只挂一条（同一字段可能被多条关系引用，注释里写不下全部）
    fk_note: dict[tuple[str, str], tuple[str, str, str]] = {}
    for lkey, lcol, rkey, rcol, card in rel_list:
        fk_note.setdefault((lkey, lcol.upper()), (rkey, rcol, card))

    created_keys = set(by_table)
    colset = {k: {c.lower() for c in v} for k, v in by_table.items()}
    out_of_scope: list[str] = []
    stats_skip: list[str] = []
    rel_rows: list[tuple[str, str, str, str, str]] = []
    for lkey, lcol, rkey, rcol, card in rel_list:
        if lkey not in created_keys or rkey not in created_keys:
            continue
        # 知识库里有拼写错误（如 BUSINESS_CONTRACT.SERIANO），列不存在就不建关系——
        # 建一条指向不存在列的关系，等于给 SQL 生成埋一个必然失败的 JOIN。
        if lcol.lower() not in colset.get(lkey, set()) or rcol not in colset.get(rkey, set()):
            stats_skip.append("%s.%s = %s.%s（列名对不上，跳过）" % (lkey, lcol, rkey, rcol))
            continue
        # 03_表关联知识库 的「基数」是从左表看右表的：一对多 = 左边一个、右边多个，
        # 所以 MDL 里要以右表为多端（MANY_TO_ONE 指向前者=一端）。
        if "一对多" in card:
            rel_rows.append((rkey, rcol, lkey, lcol.lower(), "MANY_TO_ONE"))
        elif "一对一" in card:
            rel_rows.append((lkey, lcol.lower(), rkey, rcol, "ONE_TO_ONE"))
        else:
            rel_rows.append((lkey, lcol.lower(), rkey, rcol, "MANY_TO_ONE"))

    stats = {"tables": 0, "columns": 0, "inferred": [], "pk": {}, "no_pk": [], "skipped": []}
    lines: list[str] = []
    schemas: set[str] = set()

    for key in sorted(by_table):
        cols = by_table[key]
        schema = next(iter(cols.values()))["_schema"]
        table = next(iter(cols.values()))["_table"]
        raw_t = next(iter(cols.values()))["_raw_t"]
        meta = tables_meta.get(raw_t) or tables_meta.get(table.upper()) or {}
        schemas.add(schema)

        col_names = {c.lower() for c in cols}
        # 数仓宽表按 01 的粒度补快照日（02 未列该列，但粒度定义要求它）
        extra_cols: list[str] = []
        if schema in ("dwd", "dwm") and "dp_load_dt" not in col_names:
            extra_cols.append("dp_load_dt")

        defs = []
        for upper, r in cols.items():
            cname = s(r.get("字段英文名")).lower()
            pg, src = map_type(s(r.get("数据类型")), cname)
            if src != "声明":
                stats["inferred"].append("%s.%s(%s→%s)" % (table, cname, s(r.get("数据类型")) or "空", pg))
            desc_bits = []
            cn = s(r.get("字段中文名"))
            if cn:
                desc_bits.append(cn)
            meaning = s(r.get("字段业务含义"))
            if meaning and meaning != "需要补充" and len(meaning) < 80 and meaning != cn:
                desc_bits.append(meaning)
            note = fk_note.get((key, upper))
            if note:
                target_key, target_col = note[0], note[1]
                if target_key in created_keys:
                    desc_bits.append("FK→%s.%s" % (target_key, target_col))
                else:
                    out_of_scope.append("%s.%s → %s.%s（源系统关系，本库未建该表）"
                                        % (key, cname, target_key, target_col))
            if s(r.get("是否码值")) == "Y":
                setname = s(r.get("码值集名称"))
                desc_bits.append("码值" + ("：%s" % setname if setname else ""))
            notnull = s(r.get("是否可空")).upper() == "N"
            defs.append((cname, pg, notnull, "；".join(desc_bits)))

        for c in extra_cols:
            defs.append((c, "date", True, "数据日期（快照日）；按 01_表知识库 的粒度补入，02_字段知识库未列"))

        # 知识库登记不足、业务规则要求存在的列
        for cname, pg, desc in EXTRA_COLUMNS.get(key, []):
            if cname not in {d[0] for d in defs}:
                defs.append((cname, pg, False, desc + "；按知识库规则补入"))

        # ---- 主键
        pk = [s(r.get("字段英文名")).lower() for r in cols.values() if s(r.get("是否主键")).upper() == "PK"]
        pk = sorted(set(pk))
        if not pk:
            pk = parse_key(s(meta.get("主键字段")), col_names | set(extra_cols))
        if key in COMPOSITE_PK:
            pk = COMPOSITE_PK[key]
        if pk:
            have = {d[0] for d in defs}
            missing = [c for c in pk if c not in have]
            if missing:
                stats.setdefault("pk_conflict", []).append("%s 缺列 %s，放弃主键" % (key, missing))
                pk = []
        if not pk:
            stats["no_pk"].append(key)

        stats["tables"] += 1
        stats["columns"] += len(defs)
        if pk:
            stats["pk"][key] = pk

        title = s(meta.get("表中文名")) or table
        grain = s(meta.get("数据粒度"))
        lines.append("-- %s %s" % (key, title))
        lines.append("CREATE TABLE %s.%s (" % (schema, table))
        body = []
        pk_set = set(pk)
        for cname, pg, notnull, desc in defs:
            nn = " NOT NULL" if (notnull or cname in pk_set) else ""
            body.append("  %-24s %-16s%s" % (cname, pg, nn))
        stmt = ",\n".join(body)
        if pk:
            stmt += ",\n  CONSTRAINT %s PRIMARY KEY (%s)" % ("pk_" + table[:40], ", ".join(pk))
        lines.append(stmt)
        lines.append(");")

        tdesc = title
        if grain:
            tdesc += "；粒度：%s" % grain.replace("\n", " ")
        other = s(meta.get("已知坑点/风险"))
        if other:
            tdesc += "；坑点：%s" % other.replace("\n", " ")[:200]
        lines.append("COMMENT ON TABLE %s.%s IS %s;" % (schema, table, sql_literal(tdesc)))
        for cname, _pg, _nn, desc in defs:
            if desc:
                lines.append("COMMENT ON COLUMN %s.%s.%s IS %s;" % (schema, table, cname, sql_literal(desc[:300])))
        lines.append("")

    # ---- 组装文件
    head = [
        "-- " + "=" * 76,
        "-- 信贷业务库 credit · 建表脚本",
        "-- 来源：新信贷取数知识库_v0.4.xlsx → 01_表知识库 / 02_字段知识库 / 03_表关联知识库",
        "-- 生成：tools/credit_ddl_from_kb.py（勿手改，改知识库后重新生成）",
        "-- 生成时间：%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "--",
        "-- 口径说明：",
        "--   1) 知识库里有字段级定义的表共 25 张；RPT_LOAN_DTL 与 DWM.RPT_LOAN_DTL 是同一张",
        "--      宽表的两批列（中文名相同、列不重叠、无公共键），已合并为 dwm.rpt_loan_dtl，",
        "--      故实际建 %d 张。" % stats["tables"],
        "--   2) 01_表知识库另列的 100 张表没有字段定义，本脚本不建——建一张没有列的空表只会",
        "--      让「表不存在」变成「列不存在」，更难查。清单见 db/credit/README.md。",
        "--   2) 不建物理外键，关系写进列注释（FK→表.字段）。源系统为 Oracle 贴源层，",
        "--      关系表存在拼写错误与「条件写进字段格」的行，照抄会建出错误约束。",
        "--   3) 178 个字段的类型是按命名推断的，逐条列在文件末尾，需业务复核。",
        "--   4) DWD./DWM. 前缀落到同名 schema，保留源系统分层。",
        "-- " + "=" * 76,
        "",
        "SET client_min_messages = warning;",
        "",
    ]
    schema_ddl = ["CREATE SCHEMA IF NOT EXISTS %s;" % sc for sc in sorted(schemas) if sc != "public"]
    tail = []
    if out_of_scope:
        tail += ["", "-- " + "=" * 76,
                 "-- 源系统关系，但对方表不在本批建的 %d 张表内（不做约束，仅备查）" % stats["tables"],
                 "-- " + "=" * 76]
        for item in sorted(set(out_of_scope)):
            tail.append("--   " + item)
    tail += ["", "-- " + "=" * 76, "-- 推断类型清单（%d 条），需业务复核" % len(stats["inferred"]), "-- " + "=" * 76]
    for item in stats["inferred"]:
        tail.append("--   " + item)

    sql = "\n".join(head + schema_ddl + [""] + lines + tail) + "\n"
    stats["relations"] = rel_rows
    stats["relations_skipped"] = stats_skip
    return sql, stats


def sql_literal(text: str) -> str:
    return "'" + text.replace("\0", "").replace("'", "''") + "'"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 credit 库建表脚本")
    ap.add_argument("--xlsx", required=True, help="新信贷取数知识库 xlsx 路径")
    ap.add_argument("--out", required=True, help="输出 .sql 路径")
    ap.add_argument("--relations-out", default="", help="输出关系 CSV（喂给 tools/mdl_from_pg.py --relations-csv）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sql, stats = build(args.xlsx)
    print("表 %d 张 / 字段 %d 个" % (stats["tables"], stats["columns"]))
    print("有主键：%d 张；无主键：%d 张 → %s" % (len(stats["pk"]), len(stats["no_pk"]), ", ".join(stats["no_pk"])))
    print("类型靠推断的字段：%d 个" % len(stats["inferred"]))
    if args.dry_run:
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(sql)
    print("已写入 %s（%d 行）" % (args.out, len(sql.splitlines())))
    if args.relations_out:
        rows = stats.get("relations") or []
        with open(args.relations_out, "w", encoding="utf-8") as f:
            f.write("left_table,left_column,right_table,right_column,join_type\n")
            for r in rows:
                f.write(",".join(r) + "\n")
        print("已写入 %s（%d 条关系）" % (args.relations_out, len(rows)))
    if stats.get("relations_skipped"):
        print("跳过 %d 条关系：" % len(stats["relations_skipped"]))
        for item in stats["relations_skipped"]:
            print("  - " + item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
