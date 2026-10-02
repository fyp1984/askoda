#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元数据采集 CLI（M2）

采集实现见 `gateway/collect.py`（网关内 `metadata_collect` 工具走同一份逻辑，
避免两处实现漂移）。本 CLI 用于宿主机批量采集与排查。

用法
----
    python tools/collect_metadata.py --dataset B
    python tools/collect_metadata.py --dataset A --dataset B --seed-glossary
    # 镜像可用时切换 SchemaCrawler 后端（需本机 docker）
    python tools/collect_metadata.py --dataset B --engine schemacrawler
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))

import collect  # noqa: E402
import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="元数据采集（M2）")
    ap.add_argument("--dataset", action="append", required=True,
                    help="数据集键，可重复（A / B）")
    ap.add_argument("--engine", default="native", choices=["native", "schemacrawler"])
    ap.add_argument("--seed-glossary", action="store_true", help="顺便播种业务口径词表")
    ap.add_argument("--no-seed-mdl", action="store_true", help="跳过 MDL 语义播种")
    args = ap.parse_args()

    print("== 元数据采集 ==")
    ready = db.init_schema()
    print("元数据表：%s" % ", ".join(ready))

    rc = 0
    for ds_key in args.dataset:
        r = collect.collect(
            ds_key,
            engine=args.engine,
            seed_mdl=not args.no_seed_mdl,
            seed_glossary=args.seed_glossary,
        )
        ms = r.get("mdl_seed")
        if ms:
            print("[%s] MDL 播种：%d 表 / %d 字段 / %d 关系"
                  % (ds_key, ms["tables"], ms["columns"], ms["relationships"]))
        imp = r["import"]
        print("[%s] 物理采集（%s）：%d 表 / %d 字段（未建模、对 AI 不可见 %d 个）"
              % (ds_key, r["engine"], imp["tables"], imp["columns"], imp["not_modelled_hidden"]))
        con = r["consistency"]
        print("[%s] 一致性自检：MDL 可见字段 %d 个，物理缺失 %d 个 %s"
              % (ds_key, con["mdl_visible_fields"], len(con["missing_in_physical"]),
                 "✅" if con["ok"] else "⚠️ " + "、".join(con["missing_in_physical"][:8])))
        if not con["ok"]:
            rc = 2

        # 双后端时打印交叉校验：两条独立采集路径（information_schema vs SchemaCrawler）
        # 的结构必须一致，否则说明"元数据字典可信"这个前提不成立。
        cc = r.get("cross_check")
        if cc:
            print("[%s] 双路径交叉校验：native %d 列 / schemacrawler %d 列，主键差异 %d 个 %s"
                  % (ds_key, cc["native_columns"], cc["schemacrawler_columns"],
                     len(cc["primary_key_mismatch"]), "✅" if cc["consistent"] else "⚠️"))
            if cc["only_in_native"]:
                print("     仅 native 有：%s" % "、".join(cc["only_in_native"][:8]))
            if cc["only_in_schemacrawler"]:
                print("     仅 schemacrawler 有：%s" % "、".join(cc["only_in_schemacrawler"][:8]))
            if cc["primary_key_mismatch"]:
                print("     主键判定不一致：%s" % "、".join(cc["primary_key_mismatch"][:8]))
            print("     类型名补齐（取自 native）：%s 个" % r.get("types_filled_from_native"))
            if not cc["consistent"]:
                rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
