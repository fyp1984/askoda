# -*- coding: utf-8 -*-
"""credit 库测试数据生成

为什么要单独写脚本而不是一堆 INSERT
------------------------------------
这批数据要支撑「SQL 生成 → 真实试跑 → 结果核对」的闭环，所以必须自带三条内部约束：

  1. **同一笔借据在各表的口径一致**：ACCT_LOAN.余额、BUSINESS_DUEBILL.余额、
     DWD/DWM 快照表.余额，都由「借据金额 − 截至快照日已还本金」这同一个算式产生。
     随机造数会在跨表对比时暴露「门店 9644 / 品类 10300」那种对不上的死结，
     而且那种错门禁查不出来——只能靠造数时就保证一致。
  2. **五级分类由逾期天数推导**，不是独立随机：0 天=正常、1–90=关注、91–180=次级、
     181–360=可疑、>360=损失。这样「不良率」类需求才有唯一正确答案。
  3. **可复现**：random seed 固定，重跑得到同一批数，基线才能逐题比对。

写法上按「列名取交集」写入：每行是一个 dict，只写库里真实存在的列。填错列名不会
让整批失败，而是被丢弃并打印出来——因为这类错只有在真跑一遍时才暴露。

用法
----
    python db/credit/02_seed.py --dsn postgresql://test:test@127.0.0.1:15432/credit \
        [--kb-xlsx 新信贷取数知识库_v0.4.xlsx] [--reset]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None

SEED = 20261101
SNAPSHOTS = [dt.date(2026, 9, 30), dt.date(2026, 6, 30)]
ASOF = dt.date(2026, 10, 31)

ORGS = [
    ("1000", "总行营业部", "1000", "总行", "上海"),
    ("1001", "上海分行", "1000", "分行", "上海"),
    ("1002", "杭州分行", "1000", "分行", "浙江"),
    ("1003", "南京分行", "1000", "分行", "江苏"),
    ("100101", "浦东支行", "1001", "支行", "上海"),
    ("100102", "徐汇支行", "1001", "支行", "上海"),
    ("100201", "西湖支行", "1002", "支行", "浙江"),
    ("100301", "鼓楼支行", "1003", "支行", "江苏"),
]
BRANCH_OF = {"100101": "1001", "100102": "1001", "100201": "1002", "100301": "1003"}
SUB_BRANCHES = ["100101", "100102", "100201", "100301"]
ORG_NAME = {o[0]: o[1] for o in ORGS}

# 业务品种：1140 开头为个人（知识库 06_SQL案例库：对公 = NOT LIKE '1140%'，个人 = LIKE '1140%'）
PRODUCTS = [
    ("1101", "对公流动资金贷款", "00"),
    ("1102", "对公项目贷款", "00"),
    ("1103", "对公固定资产贷款", "00"),
    ("1120", "对公贸易融资", "00"),
    ("1140", "个人住房按揭贷款", "10"),
    ("1141", "个人消费贷款", "10"),
    ("1142", "个人经营性贷款", "10"),
    ("2100", "同业拆借", "20"),
]
PROD_NAME = {p[0]: p[1] for p in PRODUCTS}
GRT_MODES = [("10", "信用"), ("20", "保证"), ("30", "抵押"), ("40", "质押")]
GRT_NAME = dict(GRT_MODES)


def five_level(od_days: int, finished: bool) -> str:
    """按逾期天数推导五级分类字母码"""
    if finished or od_days <= 0:
        return "N"
    if od_days <= 90:
        return "A"
    if od_days <= 180:
        return "S"
    if od_days <= 360:
        return "D"
    return "L"


def add_months(d: dt.date, n: int) -> dt.date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.date(y, m, min(d.day, days[m - 1]))


def months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def code_rows_from_kb(path: str) -> list[tuple[str, str, str, str]]:
    try:
        import openpyxl
    except Exception:
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["04_码值知识库"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for r in rows[1:]:
        d = dict(zip(hdr, r))
        if not d.get("码值"):
            continue
        out.append((str(d.get("码值集名称") or "").strip(),
                    str(d.get("码值") or "").strip(),
                    str(d.get("中文含义") or "").strip(),
                    str(d.get("所属字段") or "").strip()))
    return out


# ---------------------------------------------------------------- 造数

def build_rows(kb_xlsx: str | None) -> dict[str, list[dict]]:
    rnd = random.Random(SEED)
    T: dict[str, list[dict]] = {}

    # ---- 维度
    T["public.org_info"] = [
        {"orgid": o, "orgname": n, "mainframeorgid": m, "orglevel": lv, "belongarea": ar}
        for o, n, m, lv, ar in ORGS
    ]
    users = [{"userid": "U%03d" % i, "username": "客户经理%02d" % i} for i in range(1, 21)]
    T["public.user_info"] = users
    T["public.business_type"] = [{"typeno": p, "typename": n, "isinuse": "1"} for p, n, _ in PRODUCTS]

    # ---- 码值（知识库 04_码值知识库 + 内置兜底）
    code_lib: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for setname, item, name, col in (code_rows_from_kb(kb_xlsx) if kb_xlsx else []):
        if not setname or (setname, item) in seen:
            continue
        seen.add((setname, item))
        code_lib.append({"codeno": setname, "itemno": item, "itemname": name,
                         "colname": col or None, "coleditsource": None})
    for setname, items in [
        ("CUSTOMERTYPE", [("00", "对公客户"), ("10", "个人客户"), ("20", "同业客户")]),
        ("FIVELVLCGYCD_JG", [("N", "正常"), ("A", "关注"), ("S", "次级"), ("D", "可疑"), ("L", "损失")]),
        ("LOANSTS", [("1", "正常"), ("2", "逾期"), ("3", "结清"), ("4", "核销")]),
        ("BUSINESSCURRENCY", [("CNY", "人民币"), ("USD", "美元"), ("HKD", "港币")]),
        ("CERTTYPE", [("01", "组织机构代码"), ("10", "居民身份证"), ("20", "营业执照")]),
    ]:
        for item, name in items:
            if (setname, item) in seen:
                continue
            seen.add((setname, item))
            code_lib.append({"codeno": setname, "itemno": item, "itemname": name,
                             "colname": None, "coleditsource": None})
    T["public.code_library"] = code_lib

    # ---- 客户
    customers: list[dict] = []
    plan = ["00"] * 60 + ["10"] * 55 + ["20"] * 5
    for i, ctype in enumerate(plan, start=1):
        cid = "C%06d" % i
        org = rnd.choice(SUB_BRANCHES)
        if ctype == "00":
            name = "%s%s有限公司" % (rnd.choice(["华东", "申达", "远洋", "恒信", "汇通", "天成"]),
                                    rnd.choice(["贸易", "实业", "科技", "物流", "建设"]))
            cert_type, cert_id = "01", "%018d" % rnd.randrange(10 ** 17, 10 ** 18)
            scale = rnd.choice(["大型", "中型", "小型", "微型"])
        elif ctype == "10":
            name = "%s%s" % (rnd.choice(["张", "王", "李", "刘", "陈", "杨"]),
                             rnd.choice(["建国", "丽华", "伟", "敏", "强", "静"]))
            cert_type, cert_id = "10", "3101%014d" % rnd.randrange(10 ** 13, 10 ** 14)
            scale = "个人"
        else:
            name = "%s银行同业部" % rnd.choice(["浦发", "华夏", "兴业"])
            cert_type, cert_id = "20", "%018d" % rnd.randrange(10 ** 17, 10 ** 18)
            scale = "同业"
        customers.append({
            "customerid": cid, "customername": name, "customertype": ctype,
            "certtype": cert_type, "certid": cert_id, "inputorgid": org,
            "inputuserid": rnd.choice(users)["userid"],
            "inputdate": dt.date(2023, 1, 1) + dt.timedelta(days=rnd.randrange(0, 900)),
            "mfcustomerid": "MFC%06d" % i, "status": "1", "channel": rnd.choice(["网银", "柜面", "客户经理"]),
            "nationcode": "CHN", "forbidstatus": "0", "mfcustomername": name,
            "isbadcustomer": "N", "riskalarmlevel": "低", "custst": "1",
            "mainframeorgid": BRANCH_OF.get(org, org), "customerscale": scale,
        })
    T["public.customer_info"] = customers
    cust_map = {c["customerid"]: c for c in customers}

    # ---- 财报（对公客户逐年）
    fs = []
    for i, c in enumerate(customers):
        if c["customertype"] != "00":
            continue
        for y in (2024, 2025):
            fs.append({"customerid": c["customerid"], "recordno": "FS%06d%d" % (i, y),
                       "reportdate": dt.date(y, 12, 31), "reportscope": "年报"})
    T["public.customer_fsrecord"] = fs

    # ---- 合同 / 借据 / 账务 / 计划 / 日志 / 分类 / 流程
    contracts, duebills, loans, sched, paylog, classify, tasks = [], [], [], [], [], [], []
    seq = 0
    for ci, cust in enumerate(customers):
        cid, cname, ctype = cust["customerid"], cust["customername"], cust["customertype"]
        org = cust["inputorgid"]
        uid = cust["inputuserid"]
        n_contract = 1 if ctype != "00" else rnd.choice([1, 1, 2])
        for k in range(n_contract):
            prod = rnd.choice([p for p in PRODUCTS if p[2] == ctype])[0]
            cntr_no = "HT%04d%03d" % (ci + 1, k + 1)
            base = rnd.uniform(50, 8000) if ctype == "00" else rnd.uniform(20, 500)
            cntr_amt = round(base * 10000, 2)
            sign = dt.date(2023, 6, 1) + dt.timedelta(days=rnd.randrange(0, 800))
            grt = rnd.choice(GRT_MODES)[0]
            contracts.append({
                "serialno": cntr_no, "artificialno": cntr_no + "-R", "customerid": cid,
                "businesstype": prod, "businesssum": cntr_amt, "vouchtype": grt,
                "status": "1", "manageuserid": uid, "manageorgid": org,
                "accountorgid": BRANCH_OF.get(org, org),
            })
            n_dbl = rnd.choice([1, 1, 2, 3])
            for j in range(n_dbl):
                seq += 1
                dbl_no = "JJ%06d" % seq
                amt = round(cntr_amt / n_dbl * rnd.uniform(0.8, 1.0), 2)
                term = rnd.choice([12, 24, 36, 60, 120])
                rate = round(rnd.uniform(3.45, 6.50), 4)
                putout = sign + dt.timedelta(days=30 * (j + 1) + rnd.randrange(0, 20))
                maturity = add_months(putout, term)
                finished = maturity <= ASOF
                od_days = 0
                if not finished and rnd.random() < 0.08:
                    od_days = rnd.choice([15, 45, 75, 120, 200, 400])
                lv = five_level(od_days, finished)
                sts = "3" if finished else ("2" if od_days else "1")
                done_terms = max(0, min(term, months_between(putout, ASOF)))
                bal = 0.0 if finished else round(max(amt - amt / term * done_terms, 0), 2)
                od_bal = bal if od_days > 90 else 0.0
                od_int = round(bal * rate / 100 / 12 * max(1, od_days // 30), 2) if od_days else 0.0

                duebills.append({
                    "serialno": dbl_no, "relativeserialno2": cntr_no, "mfcustomerid": cust["mfcustomerid"],
                    "customerid": cid, "businesstype": prod, "businessstatus": sts,
                    "businesscurrency": "CNY", "businesssum": amt, "balance": bal,
                    "normalbalance": round(bal - od_bal, 2), "overduebalance": od_bal,
                    "putoutdate": putout, "maturity": maturity, "actualmaturity": maturity,
                    "finishdate": maturity if finished else None,
                })
                loans.append({
                    "serialno": "DK%06d" % seq, "accountno": "ACC%012d" % (622200000000 + seq),
                    "contractserialno": cntr_no, "customerid": cid, "customername": cname,
                    "businesstype": prod, "productid": prod, "currency": "CNY",
                    "businesssum": amt, "putoutdate": putout, "maturitydate": maturity,
                    "originalmaturitydate": maturity, "operateorgid": org,
                    "accountingorgid": BRANCH_OF.get(org, org), "loanstatus": sts,
                    "finishdate": maturity if finished else None, "businessdate": ASOF,
                    "overduedays": od_days, "classifyresult": lv,
                    "putoutserialno": "CZ%06d" % seq, "duebillserialno": dbl_no,
                    "normalbalance": round(bal - od_bal, 2), "odintebalance": od_int,
                    "overduebalance": od_bal, "compdintebalance": 0, "fineintebalance": 0,
                })
                paid_terms = term if finished else done_terms
                for p in range(1, term + 1):
                    pay_date = add_months(putout, p)
                    pp = round(amt / term, 2)
                    sched.append({
                        "serialno": "HD%08d%03d" % (seq, p), "parentserialno": dbl_no,
                        "objecttype": "DUEBILL", "objectno": dbl_no,
                        "relativeobjecttype": "DUEBILL", "relativeobjectno": dbl_no,
                        "periodno": p, "paydate": pay_date, "pstype": "1",
                        "payitemcode": "PRINCIPAL", "payprincipalamt": pp,
                        "actualpayprincipalamt": pp if p <= paid_terms else 0,
                    })
                    if p == paid_terms and paid_terms > 0:
                        paylog.append({"relativeobjectno": dbl_no, "actualpaydate": pay_date,
                                       "actualpayprincipalamt": pp,
                                       "actualpayinterestamt": round(pp * rate / 100 / 12, 2)})
                classify.append({"objectno": dbl_no, "classifyresult": "N"})
                if lv != "N":
                    classify.append({"objectno": dbl_no, "classifyresult": lv})
                tasks.append({"objectno": dbl_no, "objecttype": "DUEBILL",
                              "phasename": rnd.choice(["受理", "审查", "审批", "放款"]),
                              "userid": uid, "begintime": dt.datetime.combine(
                                  putout - dt.timedelta(days=rnd.randrange(3, 20)), dt.time(9, 30)),
                              "phaseaction": "提交"})

    T["public.business_contract"] = contracts
    T["public.business_duebill"] = duebills
    T["public.acct_loan"] = loans
    T["public.acct_payment_schedule"] = sched
    T["public.acct_payment_log"] = paylog
    T["public.classify_record"] = classify
    T["public.flow_task"] = tasks

    # ---- 担保
    gcontracts, ginfos, grels = [], [], []
    for gi in range(1, 81):
        gid = "GC%05d" % gi
        cust = customers[(gi * 3) % len(customers)]
        amt = round(rnd.uniform(100, 5000) * 10000, 2)
        gcontracts.append({"serialno": gid, "guarantorid": cust["customerid"],
                           "guarantorname": cust["customername"], "contractstatus": "1",
                           "guarantysum": amt})
        for k in range(rnd.choice([1, 1, 2])):
            pid = "G%05d%02d" % (gi, k + 1)
            ginfos.append({"guarantyid": pid, "guarantystatus": rnd.choice(["1", "2"]),
                           "evalnetvalue": round(amt / 2, 2), "guarantysum": round(amt / 2, 2)})
            grels.append({"objecttype": "GUARANTY_CONTRACT", "objectno": gid,
                          "guarantyid": pid, "contractno": gid})
    T["public.guaranty_contract"] = gcontracts
    T["public.guaranty_info"] = ginfos
    T["public.guaranty_relative"] = grels

    T["public.samecustomer_relative"] = [
        {"rltvtypeid": "R%02d" % i, "certenddate": dt.date(2027, 12, 31)} for i in range(1, 21)
    ]

    # ---- 快照表（同一算式派生，保证与账务/借据表对得上）
    dwd_rows, dwm_rows = [], []
    cntr_map = {c["serialno"]: c for c in contracts}
    for snap in SNAPSHOTS:
        for d in duebills:
            putout, maturity = d["putoutdate"], d["maturity"]
            if putout > snap:
                continue
            term = max(1, months_between(putout, maturity))
            done = max(0, min(term, months_between(putout, snap)))
            snap_bal = round(max(d["businesssum"] - d["businesssum"] / term * done, 0), 2)
            od_days_snap = max(0, (snap - maturity).days) if snap > maturity else 0
            lv = five_level(od_days_snap, False)
            cust = cust_map[d["customerid"]]
            ctype = cust["customertype"]
            grt = cntr_map[d["relativeserialno2"]]["vouchtype"]
            dwd_rows.append({
                "dp_load_dt": snap, "syscgy": "101", "dblno": d["serialno"],
                "dblamt": d["businesssum"], "dblbal": snap_bal, "prdno": d["businesstype"],
                "loansts": d["businessstatus"], "fivelvlcgycd_jg": lv,
                "custnm": cust["customername"], "certno": cust["certid"],
                "bnkincustno": cust["mfcustomerid"], "wrtoffpnp": 0,
                "wrtoffdt": None, "oper": cust["inputuserid"], "cntrno": d["relativeserialno2"],
            })
            dwm_rows.append({
                "dblno": d["serialno"], "cntrno": d["relativeserialno2"],
                "cntrseqno": d["relativeserialno2"] + "-S", "custno": cust["customerid"],
                "bnkincustno": cust["mfcustomerid"], "custnm": cust["customername"],
                "certtyp": cust["certtype"], "certno": cust["certid"],
                "custtypbcgy": {"00": "对公", "10": "个人", "20": "同业"}[ctype],
                "custtypcgy": {"00": "企业", "10": "自然人", "20": "金融机构"}[ctype],
                "prdno": d["businesstype"], "prdnm": PROD_NAME[d["businesstype"]], "cur": "CNY",
                "acgorgno": cust["inputorgid"], "acgorgnm": ORG_NAME.get(cust["inputorgid"], ""),
                "blgbrchno": BRANCH_OF.get(cust["inputorgid"], cust["inputorgid"]),
                "blgbrchnm": ORG_NAME.get(BRANCH_OF.get(cust["inputorgid"], ""), ""),
                "mngorgno": cust["inputorgid"], "mngorgnm": ORG_NAME.get(cust["inputorgid"], ""),
                "setlacctno": "ACC" + d["serialno"], "loanacctno": "ACC" + d["serialno"],
                "loancgylv1": "贷款", "loancgylv2": "信贷", "loancgylv3": PROD_NAME[d["businesstype"]],
                "loancgylv4": PROD_NAME[d["businesstype"]],
                "grtmode": grt, "grtform": GRT_NAME.get(grt, ""),
                "fivelvlcgy": lv, "fivelvlcgycd_jg": lv,
                "yrbgnfivelvlcgy": "N", "mobgnfivelvlcgy": lv,
                "operorgnm": ORG_NAME.get(cust["inputorgid"], ""),
                "bsnsts": d["businessstatus"], "dblsts": d["businessstatus"],
                "valtdt": putout, "expdt": maturity, "actexpdt": maturity, "cnlacctdt": None,
                "loanrate": None, "cntramt": cntr_map[d["relativeserialno2"]]["businesssum"],
                "dblamt": d["businesssum"], "dblbal": snap_bal, "dblbalcnvtcny": snap_bal,
                "oduebal": 0, "oduedys": od_days_snap, "pnpoduedys": od_days_snap,
                "intoduedys": 0, "maxoduedys": od_days_snap,
                "onshtdbtintbal": round(snap_bal * 0.02, 2), "offshtdbtintbal": 0,
                "pnpoduepnyint": 0, "intoduepnyint": 0, "entrbaddt": None,
                "twllvlcgycd": lv, "datadt": snap, "dp_load_dt": snap,
                "loantyp": "自营贷款", "loanuse": "经营周转" if ctype == "00" else "消费",
            })
    T["dwd.dwd_lan_dbl_fcti"] = dwd_rows
    T["dwm.rpt_loan_dtl"] = dwm_rows

    # ---- 押品/担保的贴源层与报表（与上面同一批担保数据派生，不另造）
    snap0 = SNAPSHOTS[0]
    own_map = {g["serialno"]: g for g in gcontracts}
    T["public.odm_cms_cms_collateraltype_info"] = [
        {"data_dt": snap0, "coltypeid": "CT%02d" % i, "guarantytype": code,
         "guarantytypename": name, "fathertypecd": "00", "isleafnodes": "1",
         "status": "1", "validdate": dt.date(2020, 1, 1), "invaliddate": dt.date(2040, 12, 31)}
        for i, (code, name) in enumerate(
            [("30", "房产抵押"), ("40", "存单质押"), ("41", "股权质押"), ("20", "保证担保"),
             ("31", "土地抵押"), ("42", "应收账款质押"), ("10", "信用"), ("43", "设备抵押")], start=1)
    ]
    odm_gc, odm_gi, odm_gr = [], [], []
    for g in gcontracts:
        odm_gc.append({
            "data_dt": snap0, "serialno": g["serialno"], "contractno": g["serialno"],
            "contracttype": "1", "guarantytype": "20", "contractstatus": g["contractstatus"],
            "signdate": dt.date(2024, 3, 1), "begindate": dt.date(2024, 3, 1),
            "enddate": dt.date(2027, 3, 1), "guarantorid": g["guarantorid"],
            "guarantorname": g["guarantorname"], "creditorgid": "100101",
            "creditorgname": ORG_NAME["100101"], "guarantycurrency": "CNY",
            "guarantyvalue": g["guarantysum"], "guarantysum": g["guarantysum"],
            "ownguarantysum": g["guarantysum"], "dp_load_dt": snap0,
        })
    for i, gi in enumerate(ginfos):
        gc = gcontracts[i % len(gcontracts)]
        odm_gi.append({
            "data_dt": snap0, "guarantyid": gi["guarantyid"], "guarantytype": "30",
            "guarantystatus": gi["guarantystatus"], "ownerid": gc["guarantorid"],
            "ownername": gc["guarantorname"], "ownertype": "00",
            "guarantysum": gi["guarantysum"], "evalnetvalue": gi["evalnetvalue"],
            "guarantyvalue": gi["evalnetvalue"], "confirmvalue": gi["evalnetvalue"],
            "guarantyamount": gi["guarantysum"], "guarantycurrency": "CNY",
            "evaldate": dt.date(2026, 6, 30), "evalorgname": "中估联",
            "begindate": dt.date(2024, 3, 1), "enddate": dt.date(2027, 3, 1),
            "pledgestatus": "1", "dp_load_dt": snap0,
        })
        odm_gr.append({
            "data_dt": snap0, "objecttype": "GUARANTY_CONTRACT", "objectno": gi["guarantyid"],
            "contractno": gi["guarantyid"], "guarantyid": gi["guarantyid"],
            "status": "1", "relationstatus": "1", "guarantysum": gi["guarantysum"],
            "payorder": i + 1, "dp_load_dt": snap0,
        })
    T["public.odm_cms_guaranty_contract"] = odm_gc
    T["public.odm_cms_guaranty_info"] = odm_gi
    T["public.odm_cms_guaranty_relative"] = odm_gr

    # 押品报表：有担保方式的借据才进这张表（信用贷款无押品）
    rpt = []
    for snap in SNAPSHOTS:
        for d in duebills:
            c = cntr_map[d["relativeserialno2"]]
            if c["vouchtype"] == "10":
                continue
            if d["putoutdate"] > snap:
                continue
            term = max(1, months_between(d["putoutdate"], d["maturity"]))
            done = max(0, min(term, months_between(d["putoutdate"], snap)))
            bal = round(max(d["businesssum"] - d["businesssum"] / term * done, 0), 2)
            cust = cust_map[d["customerid"]]
            # 用借据序号取押品，不用 hash()——str 的 hash 每进程不同，会破坏可复现性
            gg = ginfos[int(d["serialno"][2:]) % len(ginfos)]
            rpt.append({
                "datadt": snap, "orgno": cust["inputorgid"],
                "uporgno": cust["mainframeorgid"],
                "uporgnm": ORG_NAME.get(cust["mainframeorgid"], ""),
                "dblno": d["serialno"], "cntrno": d["relativeserialno2"], "cur": "CNY",
                "curnm": "人民币", "custno": cust["customerid"], "custnm": cust["customername"],
                "custtyp": cust["customertype"], "prdno": d["businesstype"],
                "prdnm": PROD_NAME[d["businesstype"]], "loantyp": "自营贷款",
                "loanterm": term, "fivelvlcgycd": five_level(0, d["maturity"] <= snap),
                "grtmode": c["vouchtype"], "grtytyp": "30",
                "grtyno": gg["guarantyid"], "grtynm": "押品-%s" % gg["guarantyid"],
                "grtyestn": gg["evalnetvalue"], "grtysts": gg["guarantystatus"],
                "dblamt": d["businesssum"], "dblbal": bal, "valtdt": d["putoutdate"],
                "expdt": d["maturity"], "loansts": d["businessstatus"],
                "dblamtcnvtcny": d["businesssum"], "dp_load_dt": snap,
            })
    T["public.rpt_mrtgplg_dtl"] = rpt

    return T


# ---------------------------------------------------------------- 写入

def load_columns(conn) -> tuple[dict[str, set[str]], dict[tuple[str, str], int]]:
    out: dict[str, set[str]] = {}
    lens: dict[tuple[str, str], int] = {}
    with conn.cursor() as c:
        c.execute("""SELECT table_schema||'.'||table_name, column_name,
                            coalesce(character_maximum_length, 0)
                       FROM information_schema.columns
                      WHERE table_schema IN ('public','dwd','dwm')""")
        for t, col, ln in c.fetchall():
            out.setdefault(t, set()).add(col)
            if ln:
                lens[(t, col)] = int(ln)
    return out, lens


def write(dsn: str, rows: dict[str, list[dict]], reset: bool) -> tuple[dict, dict, dict]:
    conn = psycopg.connect(dsn, autocommit=False)
    stats, dropped, clipped = {}, {}, {}
    try:
        cols, lens = load_columns(conn)
        with conn.cursor() as c:
            if reset:
                c.execute("TRUNCATE " + ", ".join(sorted(rows)))
            for table, items in rows.items():
                if not items:
                    stats[table] = 0
                    continue
                known = cols.get(table, set())
                bad = [k for k in items[0] if k not in known]
                if bad:
                    dropped[table] = bad
                    items = [{k: v for k, v in it.items() if k in known} for it in items]
                # 按列真实长度截断：知识库里的取值可能比建表长度长（如码值集名）
                for it in items:
                    for k, v in it.items():
                        lim = lens.get((table, k))
                        if lim and isinstance(v, str) and len(v) > lim:
                            it[k] = v[:lim]
                            clipped[(table, k)] = clipped.get((table, k), 0) + 1
                keys = [k for k in items[0]]
                sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                    table, ",".join(keys), ",".join(["%s"] * len(keys)))
                try:
                    c.executemany(sql, [tuple(it.get(k) for k in keys) for it in items])
                except Exception as exc:
                    print("❌ 写入 %s 失败：%s" % (table, str(exc).strip().splitlines()[0]))
                    raise
                stats[table] = len(items)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats, dropped, clipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 credit 库测试数据")
    ap.add_argument("--dsn", default=os.environ.get("CREDIT_DSN", ""))
    ap.add_argument("--kb-xlsx", default="", help="可选：知识库 xlsx，用于导入 04_码值知识库")
    ap.add_argument("--reset", action="store_true", help="先清空本脚本负责的表")
    args = ap.parse_args(argv)

    if not args.dsn:
        print("缺少 --dsn（或环境变量 CREDIT_DSN）")
        return 2
    if psycopg is None:
        print("psycopg 不可用")
        return 2

    rows = build_rows(args.kb_xlsx or None)
    stats, dropped, clipped = write(args.dsn, rows, args.reset)
    print("写入完成：")
    for k in sorted(stats):
        print("  %-34s %6d 行" % (k, stats[k]))
    if clipped:
        print("\nℹ️ 以下列按建表长度截断（知识库取值更长）：")
        for (t, col), n in sorted(clipped.items()):
            print("  %s.%s → 截断 %d 行" % (t, col, n))
    if dropped:
        print("\n⚠️ 以下列名在库里不存在，已丢弃（说明脚本与建表不一致，需修）：")
        for t, ks in dropped.items():
            print("  %s → %s" % (t, ", ".join(ks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
