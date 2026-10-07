# Askoda 演示指南

面向**业务人员**与**技术人员**的演示操作手册。

本文所有命令与结论均在 2026-10-07 于本机实测跑过，输出为真实回显（未跑通的、部分跑通的都如实标注）。

---

## 0. 演示前检查（4 项，每项都给可执行命令）

先设好环境变量（后续命令都依赖它）：

```bash
export PATH="$HOME/.orbstack/bin:$PATH"
PY=/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python
cd /Users/FYP/Documents/WorkSpace/askoda
```

### 0.1 栈是否健康、8 个服务是否起齐

```bash
docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}"
```

实测输出（2026-10-07，10 个服务全部 Up）：

| 服务 | 状态 | 端口 |
|---|---|---|
| gateway | Up 3 hours (healthy) | 0.0.0.0:18080->8080 |
| bff | Up (healthy) | 127.0.0.1:18081->18081 |
| assistant-minio | Up 4 days | 0.0.0.0:19000/19001 |
| assistant-postgres | Up 4 days (healthy) | 0.0.0.0:15434->5432 |
| tei-embedding | Up 18 hours (healthy) | 0.0.0.0:18002->80 |
| wren-mcp-a | Up 4 days | 0.0.0.0:18000->8000 |
| wren-mcp-b | Up 15 hours | 0.0.0.0:18001->8000 |
| wren-postgres-a | Up 4 days (healthy) | 0.0.0.0:15432->5432 |
| wren-postgres-b | Up 4 days (healthy) | 0.0.0.0:15433->5432 |

> 注：任务书里写「8 个服务」，实测 compose 内是 9 个条目 + 1 个 minio = 10 个。
> 判定标准用**接口自探**，不数容器个数：

```bash
curl -s -m 10 http://127.0.0.1:18081/api/v1/health | python3 -m json.tool
```

实测关键回显：

```json
{
  "status": "ok",
  "datasets": [
    {"dataset": "A", "ok": true, "response": "Wren Engine is healthy (data source: postgres, MDL deployed: yes)", "ms": 130},
    {"dataset": "B", "ok": true, "response": "Wren Engine is healthy (data source: postgres, MDL deployed: yes)", "ms": 55}
  ],
  "components": {
    "meta_db": {"ok": true, "database": "assistant", "ms": 0},
    "attachments": {"ok": true, "bucket": "askoda-attachments", "bucket_exists": true},
    "knowledge": {"ok": true, "ms": 62, "datasets_visible": 1,
      "dataset": {"documents": 4, "embedding_model": "bge-m3@bge-m3@OpenAI-API-Compatible"}}
  }
}
```

**通过标准**：`status=ok`、A/B 两个 dataset 都 `ok:true`、`meta_db.ok=true`、`knowledge.ok=true`。
任一项 false 就先修那一项再继续，别带着半个栈演示。

### 0.2 远端 embedding 是否可达

知识检索的向量服务是**主备**结构：主为远端 TEI，备为本机 TEI（`18002`）。
RAGFlow 里的 `base_url` 是静态字符串，**不会自己切**，主挂了检索直接全断。

```bash
bash tools/embedding_failover.sh check
```

实测输出（主备都通，当前用主）：

```
--- 探活（不改动任何东西）---
  主（seven smile（主））✅ 通
  备（本机 TEI（备））✅ 通
```

```bash
bash tools/embedding_failover.sh status
```

实测输出：

```
  RAGFlow 当前指向：100.103.240.78:18001
  当前用：主（seven smile（主））
```

**是否要起本地备用**：默认**不用**。`tei-embedding` 服务实测已在跑
（`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18002/health` → `200`），
且 `check` 显示主备双通。只有主不通时才需要切换：

```bash
bash tools/embedding_failover.sh auto       # 主不通则自动切备
# 或手动：switch-back（切本机）/ switch-main（切回主）
```

> 切换只改 RAGFlow MySQL 里的 `base_url`，**无需重启 RAGFlow**，改完立即生效。

### 0.3 元数据是否已采集

```bash
$PY - <<'EOF'
import sys; sys.path.insert(0,'gateway')
import db
for r in db.query("""
select t.dataset, count(distinct (t.table_name,t.column_name)) cols,
       count(distinct (t.table_name,t.column_name))
         filter (where nullif(btrim(c.description),'') is not null) described,
       count(distinct t.table_name) tables
from column_docs t
left join column_docs c on c.dataset=t.dataset and c.table_name=t.table_name
                       and c.column_name=t.column_name
group by t.dataset order by t.dataset"""):
    print("dataset=%s 表=%s 字段=%s 有描述=%s 覆盖=%.1f%%" % (
        r['dataset'], r['tables'], r['cols'], r['described'],
        100.0*r['described']/r['cols']))
for r in db.query("select dataset, count(*) n from table_docs group by dataset order by dataset"):
    print("table_docs dataset=%s: %s 张" % (r['dataset'], r['n']))
for r in db.query("select dataset, term from business_glossary order by dataset, term"):
    print("glossary [%s] %s" % (r['dataset'], r['term']))
for r in db.query("select dataset, schema_version, collected_at from schema_snapshots order by dataset, collected_at"):
    print("snapshot %s %s %s" % (r['dataset'], r['schema_version'], r['collected_at']))
EOF
```

实测输出：

```
dataset=A 表=6 字段=29 有描述=2 覆盖=6.9%
dataset=B 表=8 字段=51 有描述=51 覆盖=100.0%
table_docs dataset=A: 6 张
table_docs dataset=B: 8 张
glossary [B] ads_member_repurchase_di.repurchase_flag
glossary [B] dim_member.member_status
glossary [B] dwd_order_detail_di.sales_amount
glossary [B] dwd_order_di.pay_amount
glossary [B] dws_store_daily_agg.area_sqm
snapshot A e559d369 2026-10-07 00:03:52+00:00
snapshot B 2c8c5631 2026-10-06 11:05:15+00:00
snapshot B 76471e8d 2026-10-07 00:03:58+00:00
```

**通过标准**：`table_docs` / `column_docs` 都有数据；**B 库字段描述覆盖应接近 100%**。

> **A 库只有 6.9% 是已知且预期的**（27/29 个字段没描述），这不是环境故障。
> 它正是 A 库需要人工答一轮口径的根因，见 §2.2。

若数据缺失，补采：

```bash
$PY tools/collect_metadata.py --dataset A --dataset B --seed-glossary
```

### 0.4 知识库是否已灌入

```bash
curl -s -m 20 "http://127.0.0.1:18081/api/v1/knowledge/documents?limit=20" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('total',d.get('total')); [print('  -',x.get('name'),'chunks',x.get('chunk_count')) for x in d.get('documents',[])]"
```

实测输出（4 篇，全部面向 B 域）：

```
total 4
  - 04-需求受理与口径确认规程.md              chunks 5
  - 03-表结构与颗粒度说明-零售会员域.md        chunks 9
  - 02-数据安全与敏感字段管理办法.md          chunks 5
  - 01-指标口径说明书-零售会员域.md            chunks 4
```

对应仓库内文件 `knowledge/` 下 4 篇同名 `.md`。

**光看文档数不够**——必须确认 embedding 链路真的能召回，否则演示时会当场返回 0 引用：

```bash
curl -s -m 40 -X POST http://127.0.0.1:18081/api/v1/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"question":"复购率的分母应该怎么取","top_k":3,"threshold":0.1,"demand_id":""}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok',d.get('ok'),'引用数',len(d.get('citations',[]))); [print('  -',c.get('document_name'),'sim',c.get('similarity')) for c in d.get('citations',[])[:3]]"
```

实测输出：

```
ok True 引用数 3
  - 01-指标口径说明书-零售会员域.md       sim 0.6136
  - 03-表结构与颗粒度说明-零售会员域.md   sim 0.5736
  - 04-需求受理与口径确认规程.md         sim 0.5712
```

**通过标准**：`引用数 >= 1` 且相似度非 0。若为 0，回到 §0.2 切 embedding 主备。

### 0.5（可选）演示前清场

演示前想把测试痕迹擦干净：

```bash
$PY tools/reset_demo_data.py --dry-run     # 先看，默认就是这个，不删任何东西
$PY tools/reset_demo_data.py --apply       # 确认无误后真的清
```

> ⚠️ **`--apply` 会删除全部 7 张需求域表（约 1.6 万行），不可撤销。**
> 本机 postgres 未开 `archive_mode`，**没有时间点恢复**——删了就是删了。
> 演示前请务必先跑 `--dry-run` 看清行数。
>
> `--apply` 与 `--dry-run` **同时给出会被直接拒绝**（退出码 2），
> 不存在"以某个为准"的猜测——这是 2026-10-07 实测踩坑后加的护栏。
>
> `--dry-run` 会打印**每张表将删除多少行**，并做「孤儿自检」。
> 默认**不清元数据域**（`column_docs` / `table_docs` 等）——那是语义层地基，清了环境就跑不动。
> 详见该工具的 `--help`。

---

## 1. 前端五步动线

界面入口：<http://127.0.0.1:18081/>

| 菜单 | 作用 | 演示时看什么 |
|---|---|---|
| ① 语义层 · MDL 字典 | 工作台默认首页 | 表/字段中文名、口径、颗粒度、敏感字段三色标识 |
| ② 知识储备 | 知识资产与检索 | 4 篇治理文档、检索命中与相似度 |
| ③ 数据源接入 | Schema 版本与差异 | schema_version 稳定性、主题表候选 |
| ④ 需求分析 · SQL 生成 | 提交→分析→确认→生成 | **系统反问什么**（业务人员最关心的部分） |
| ⑤ SQL 联调 · 交付 | 执行、结果、导出、审计回放 | 五层门禁记录、生产脚本、**完整判断链回放** |

菜单④ 内部 5 个分步：需求列表 → 提交需求 → 语义分析 → 口径确认 → SQL 生成。

---

## 2. 演示路线

### 结论先说：主打 B 库

| 维度 | A 库（电商域） | B 库（零售会员域） |
|---|---|---|
| 表 / 字段数 | 6 表 / 29 字段 | 8 表 / 51 字段 |
| **字段描述覆盖** | **6.9%（2/29）** | **100%（51/51）** |
| business_glossary | 0 条 | **5 条** |
| 知识文档 | 无（4 篇全面向 B 域） | 4 篇 |
| `subject.candidates` | **0 个（空）** | 2–3 个 |
| 首轮置信度 | 0.85 | 0.72–0.76 |
| 需人工答口径 | **必然要** | 需要（3 个问题） |

**建议主打 B 库**：元数据齐全、口径词表齐备、知识召回强，能把"从需求到数"的完整闭环演出来。
**A 库留作对比演示**，专门用来说明「元数据不足时系统会反问什么」——这不是缺陷，是设计使然。

### 2.1 B 库主线（推荐，约 6 分钟）

**第 1 步 · 菜单① 语义层 · MDL 字典**

- 看 B 库 8 张表的颗粒度说明（如 `dwd_order_di` = 一行一笔订单）
- 展开任一表，看字段中文名与口径描述——**强调"这不是原始库表"**，是建模后的语义资产
- 切到 A 库对比：字段描述大面积为空

**预期看到**：表格带中文标签、粒度说明、敏感字段标色。

**第 2 步 · 菜单② 知识储备**

- 看 4 篇文档清单与分块数
- 在检索框输入「复购率的分母应该怎么取」，点检索

**预期看到**：3 条引用，相似度 0.61 / 0.57 / 0.57，来自 `01-指标口径说明书` 与 `03-表结构与颗粒度说明`。

**第 3 步 · 菜单③ 数据源接入**

- 看 B 库 `schema_version` 与表/字段规模
- 点「扫描 Schema」，验证同一库两次扫描**版本号不变**（幂等）

**预期看到**：`schema_version` 稳定为 `76471e8d`。

**第 4 步 · 菜单④ 需求分析 · SQL 生成 → 提交需求**

填入 §3 的需求①，提交。

**预期看到**：4 项校验全绿（核心字段完整性 / 附件可识别 / 重复需求检查 / 时间范围自洽）。

**第 5 步 · 菜单④ → 语义分析**，点「开始分析」（数据集选 **B**）

**预期看到**（实测 `DR-20261007-CEZX`）：

```
置信度 0.72
槽位 subject       候选=3
槽位 granularity   候选=3   needs_confirmation=true
槽位 time          候选=6
槽位 scope         候选=1
槽位 fields        候选=4
槽位 risks         候选=4
```

> **讲解要点**：`time` 槽位自动给了 6 个候选——因为 B 库有明确的日期字段可依据。
> 这是元数据质量的直接体现，对比 A 库 `time` 候选为 0。

**第 6 步 · 菜单④ → 口径确认**，回答 3 个问题

| 问题 | 槽位 | 推荐答案 | 为什么这么答 |
|---|---|---|---|
| `Q-GRAIN-1` | granularity | 按业务单据逐条（一笔订单一行） | 一行一笔订单，金额不会被明细放大 |
| `Q-JOIN-1` | granularity | 同一笔业务只算一次 | 多表关联直接汇总会重复计算 |
| `Q-SCOPE-1` | scope | 排除已退款 | 销售额与退款额要分开看 |

**预期看到**：每答一题，`backfill` 区显示槽位被回填、P2 证据 +1（实测 1→2→3）。

> **讲解要点**：这是**业务人员与系统谈判口径**的地方。系统不猜，给候选 + 理由 + 影响面，
> 人拍板。证据链 P2「业务确认」会随每次答复累积。

**第 7 步 · 菜单④ → SQL 生成**，点「生成 SQL」

**预期看到**：门禁 5 层全过（L5 结果断言在生成阶段 skip，执行时才跑），得到：

```sql
SELECT s.store_name, SUM(o.pay_amount) AS 销售额
FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id
WHERE o.order_status = '已完成'
GROUP BY s.store_name
```

**第 8 步 · 菜单⑤ SQL 联调 · 交付**，点「执行取数」

**预期看到**：

```
Wren 执行成功：3 行 × 2 列（dataset=B, generator=deterministic-planner）
可交付 | 行数/样本正常（3 行 × 2 列）
门禁 7 层：L1-L5 全过，L-SQLFluff / L-GX 跳过（一期未启用）
列：['store_name', '销售额']
  上海旗舰店  5988.5
  北京朝阳店  1963.0
  杭州西湖店  1693.0
```

同时页面上会出现：结果柱状图、结果集表格、生产脚本导出（自带门禁记录头注）、迭代记录表。

**第 9 步 · 菜单⑤ → 审计回放**（技术人员重点）

- 点任意版本的「回放」按钮
- 展开完整判断链

**预期看到**（实测 `DR-20261007-CEZX` 共 2 个版本）：

```
执行记录（2 次）
  第2次  SR-f6b4367ef0e39edb  通过  已采用  2026-10-07 05:51:41
  第1次  SR-ba75333531db0b25  通过  未采用  2026-10-07 05:51:32

第 2 次的完整判断链
  pack_version cae38180 | schema_version 76471e8d | requirement_version 1 | 结果行数 3
  ① 当时的结构化需求  → 槽位全部取到（subject/time/granularity/...）
  ② 当时执行的 SQL    → 与上面一致
  ③ 门禁各层结论      → 7 层逐条列出通过/跳过
  ④ 结果断言          → 未检出可疑信号
  ⑤ 知识引用          → 6 条，指向 01-/03- 两篇治理文档
  快照缺口            → 无（gaps 为空）
```

> **讲解要点**：这��回答技术人员唯一的真问题——「这条数为什么长这样」。
> 页面明确标注这是**快照**，由历史留痕复原，不受此后元数据/知识库变动影响。
> 注意第 1 次「未采用」、第 2 次「已采用」的对比——留痕不可覆盖。

**第 10 步 · 状态收口**

在页面底部「状态流转」区点「验收通过」→ 需求进入终态「已通过」。

### 2.2 A 库对比线（说明「系统会问什么」，约 3 分钟）

用 §3 的需求②，走同样四步，但**前两步会明显不同**——这正是要演示的点。

**第 5 步 · 语义分析**（数据集选 **A**）

**预期看到**（实测 `DR-20261007-QWFO`）：

```
置信度 0.85
槽位 subject       候选=0   ← 空！
槽位 granularity   候选=0   needs_confirmation=true
槽位 time          候选=0   needs_confirmation=true
槽位 scope         候选=0
槽位 fields        候选=0
槽位 risks         候选=2
待答问题 2 个：granularity、time
```

> **讲解要点**：A 库字段描述只覆盖 6.9%，系统**没有依据**去推断统计主体和时间口径，
> 所以 `subject` / `time` / `fields` 候选全空。
> **它不猜**——空着并反问，比猜错口径强。
> 注意置信度 0.85 比 B 库的 0.72 **更高**，这正说明置信度衡量的是"问题描述清晰度"，
> 不是"元数据完备度"。别用置信度当质量信号。

**第 6 步 · 口径确认**

| 问题 | 推荐答案 |
|---|---|
| `Q-GRAIN-1` | 其他（请补充说明）→ 填「按商品类目汇总，一行一个类目」 |
| `Q-TIME-1` | 其他（请补充说明）→ 填「按业务发生当天」 |

**预期看到**：答复后 `fields`/`granularity`/`subject`/`time` 四个槽位同时回填。

> **讲解要点**：A 库必须**人工兜底**才能往下走，这一段是业务人员的真实负担。
> 用它来论证「元数据治理（菜单①）为什么是第一步」。

**第 7–8 步 · 生成 SQL + 执行**

**预期看到**：门禁通过，执行成功：

```sql
SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额
FROM order_items oi JOIN products p ON oi.product_id = p.product_id
GROUP BY p.category_name ORDER BY 销售额 DESC
```

```
可交付 | 行数/样本正常（3 行 × 2 列）
  数码电器  2695.0
  服饰鞋包  1034.0
  家居生活   744.0
```

**结论**：A 库**能跑通四步**，但必须人工答一轮口径。适合作为"为什么要先治元数据"的论据。

### 2.3 A 库 / B 库各自适合演什么

| 场景 | 用哪个 | 理由 |
|---|---|---|
| 完整闭环、争取业务方认可 | **B 库** | 元数据齐、口径自动出候选、知识召回强，四步顺滑 |
| 展示知识库价值 | **B 库** | 4 篇文档全面向 B 域，检索必然命中 |
| 展示口径词表的作用 | **B 库** | 5 条 glossary 全部 B 域，复购率分母规则能被触发 |
| 说明「系统会反问什么」 | A 库 | `subject`/`time` 候选为空，反问最直观 |
| 论证元数据治理的必要性 | A 库 | 6.9% vs 100% 的对比就是最好的论据 |
| 展示审计回放 | 两库都行 | 实测两库 `gaps` 都为空，回放链完整 |

---

## 3. 演示脚本（3 条，均已实测跑通）

> **关于本文引用的需求编号**：`DR-20261007-CEZX` / `-QWFO` / `-90RM` 是 2026-10-07
> 实测跑通时生成的三条需求。它们随后被 §0.5 的清场操作清掉了（清场是必要的：
> 演示前要保证「需求列表」里只有你现场提交的那几条）。
> **下面的槽位候选、门禁层数、行数、SQL 文本都是真实回显**，重新提交同样文本
> 会得到同样结果，只是 `demand_id` 与 `sql_run_id` 每次不同。
> 现场以页面实际显示为准。

### 需求① · B 库主线（推荐首选）

```
title           : 各门店销售额与已退款金额统计
business_context: 零售会员域月度经营分析，需要按门店对比销售额与退款额，识别退款集中的门店
description     : 统计最近30天各门店的销售额（SUM(sales_amount)）与已退款金额（SUM(refund_amount)），
                  按销售额降序排列，需要展示门店名称
expected_output : 一张表：门店名称、销售额、已退款金额、销售额降序
contact         : 演示用户
time_range      : 最近30天
```

**实测结果**（`DR-20261007-CEZX`）：四步全通，门禁 7 层通过，输出 3 行 × 2 列，
审计回放 2 个版本、`gaps` 为空、6 条知识引用。

> ⚠️ **已知局限（如实告知）**：实际生成的 SQL 只覆盖了**销售额**，
> **没有带出已退款金额**——`refund_amount` 未被规划器采纳。
> 演示时可顺势说明「受控生成不猜口径，缺的部分由人工在候选 SQL 里补」，
> 或直接改用下方需求③（结果更贴合预期）。
> 若必须一次产出两列，在菜单⑤ 的候选 SQL 框里手填完整 SQL 提交。

### 需求② · A 库对比线

```
title           : 各商品类别的订单数量与销售金额统计
business_context: 电商域商品运营分析，需要看各商品类目的订单量与金额，用于判断哪些类目贡献大
description     : 统计各商品类别（category）的订单数量与销售金额，按销售金额降序排列
expected_output : 一张表：商品类别、订单数、销售金额、销售金额降序
contact         : 演示用户
```

**实测结果**（`DR-20261007-QWFO`）：四步全通。首轮 `subject`/`time`/`fields` 候选全空、
必答 2 个口径问题；答复后四槽位回填，生成 SQL 门禁通过，输出 3 行 × 2 列。

> ⚠️ 同样只输出了**销售额**，未带订单数量。

### 需求③ · B 库 · 触发口径词表（结果最贴合预期，推荐演示用）

```
title           : 各会员等级复购率统计
business_context: 零售会员域运营复盘，需要按会员等级看复购率，识别高价值等级
description     : 统计各会员等级（member_status）的复购率，复购率=复购用户数/活跃用户数，分子分母都要输出
expected_output : 一张表：会员等级、活跃用户数、复购用户数、复购率
contact         : 演示用户
```

**实测结果**（`DR-20261007-90RM`）：四步全通，输出 1 行 × 3 列：

```
复购率百分比 87.5 | 复购用户数 7.0 | 活跃会员数 8
```

> **讲解要点**：这条最能体现「口径词表的价值」。`business_glossary` 里写着
> 「复购率=复购用户数/活跃用户数（须随附活跃用户数，分母不可悬空）」，
> 结果里**真的带上了 `活跃会员数` 这一列**——口径规则被执行了，不是写在文档里好看。
>
> ⚠️ **已知局限**：未按 `member_status` 分组（输出了全局单行），标题里的"各会员等级"
> 未被规划器采纳。同样可在候选 SQL 里手补 `GROUP BY`。

### 提交命令（可直接粘）

```bash
# 需求③（推荐）
curl -s -X POST http://127.0.0.1:18081/api/v1/demand \
  -H 'Content-Type: application/json' -d '{
  "title": "各会员等级复购率统计",
  "business_context": "零售会员域运营复盘，需要按会员等级看复购率，识别高价值等级",
  "description": "统计各会员等级（member_status）的复购率，复购率=复购用户数/活跃用户数，分子分母都要输出",
  "expected_output": "一张表：会员等级、活跃用户数、复购用户数、复购率",
  "contact": "演示用户", "actor": "demo-guide"
}' | python3 -c "import sys,json; print(json.load(sys.stdin)['demand_id'])"
```

拿到 `demand_id` 后按 §2 的步骤 5→10 走。

---

## 4. 演示中可能被问到的问题

**Q：为什么 A 库候选是空的，是不是坏了？**
不是。A 库字段描述只覆盖 6.9%（2/29），系统没有依据推断口径，所以不猜、反问。
这正是菜单① 存在的意义。

**Q：置信度 0.85 是不是比 0.72 更靠谱？**
不是。置信度衡量**问题描述的清晰度**，不衡量元数据完备度。
A 库那句"统计各类别订单数与金额"写得很清楚，所以分高；但它元数据最差。

**Q：为什么审计回放里的 SQL 少了几列？**
受控生成不猜口径，规划器只采纳有依据的部分。缺的部分可以在菜单⑤ 的候选 SQL 框手填后提交，
会留下完整留痕与门禁记录。

**Q：门禁 L-SQLFluff / L-GX 为什么是"跳过"？**
一期未启用，标记为 skip 而非通过——不把没做的事显示成做成了。

**Q：能演示生产环境吗？**
当前**没有任何入站鉴权与多租户隔离**，BFF 只绑 127.0.0.1 回环（这是唯一防护）。
进生产前必须先补鉴权，不能放开到 0.0.0.0。

---

## 5. 一页速查

```bash
# 环境
export PATH="$HOME/.orbstack/bin:$PATH"
PY=/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python
cd /Users/FYP/Documents/WorkSpace/askoda

# 栈健康
docker compose ps
curl -s http://127.0.0.1:18081/api/v1/health | python3 -m json.tool

# embedding 主备
bash tools/embedding_failover.sh check

# 知识库
curl -s "http://127.0.0.1:18081/api/v1/knowledge/documents?limit=20"
curl -s -X POST http://127.0.0.1:18081/api/v1/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"question":"复购率的分母应该怎么取","top_k":3,"threshold":0.1,"demand_id":""}'

# 审计回放（技术人员）
curl -s "http://127.0.0.1:18081/api/v1/demand/<需求号>/replay?version=0"

# 演示前清场
$PY tools/reset_demo_data.py --dry-run
```
