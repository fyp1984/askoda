# credit · 信贷业务测试库

从《新信贷取数知识库_v0.4.xlsx》生成，作为 askoda 的第三个数据集（引擎代号 `C`），
供「SQL 生成 → 真实试跑 → 结果核对」闭环使用。

## 目录里有什么

| 文件 | 是什么 | 怎么来的 |
|---|---|---|
| `01_schema.sql` | 建库脚本（24 张表 / 564 列，含中文注释与主键） | `tools/credit_ddl_from_kb.py` 从工作簿生成 |
| `02_seed.py` | 测试数据生成（可复现，固定随机种子） | 手写，按业务口径建模 |
| `03_relations.csv` | 表间关系 13 条（喂给 MDL） | 同上生成器导出 |
| `README.md` | 本文件 | — |

## 三步重建

```bash
# 1. 建库（库名 credit，宿主 15432，账号 test/test）
docker exec ekos-postgres psql -U test -d postgres \
  -c "DROP DATABASE IF EXISTS credit WITH (FORCE);" \
  -c "CREATE DATABASE credit OWNER test ENCODING 'UTF8';"
docker exec -i ekos-postgres psql -U test -d credit -v ON_ERROR_STOP=1 -f - < db/credit/01_schema.sql

# 2. 造数
python db/credit/02_seed.py --dsn postgresql://test:test@127.0.0.1:15432/credit \
  --kb-xlsx <知识库 xlsx 路径> --reset

# 3. 生成语义层并让 C 引擎装载
python tools/mdl_from_pg.py --dsn postgresql://test:test@127.0.0.1:15432/credit \
  --catalog credit --schemas public,dwd,dwm \
  --relations-csv db/credit/03_relations.csv \
  --out wren-docker-c/workspace/mdl.json
docker-compose restart wren-mcp-c
```

建完在页面「数据源 · 登记数据源」里填：编码 `C`、引擎 `C`、库名 `credit`、用户/口令 `test`，
MDL 留空则沿用引擎侧已装载的那份。

## 建了什么（24 张）

按源系统分层放在三个 schema：

- `public`（21 张，贴源/业务表）：`customer_info`、`customer_fsrecord`、`business_contract`、
  `business_duebill`、`acct_loan`、`acct_payment_schedule`、`acct_payment_log`、
  `classify_record`、`flow_task`、`guaranty_contract`、`guaranty_info`、`guaranty_relative`、
  `org_info`、`user_info`、`business_type`、`code_library`、`samecustomer_relative`、
  `rpt_mrtgplg_dtl`（押品报表）、`odm_cms_guaranty_info`、`odm_cms_guaranty_contract`、
  `odm_cms_guaranty_relative`、`odm_cms_cms_collateraltype_info`
- `dwd`（1 张）：`dwd_lan_dbl_fcti` 贷款借据事实表，主键 `(dp_load_dt, syscgy, dblno)`
- `dwm`（1 张）：`rpt_loan_dtl` 贷款明细报表宽表，132 列，主键 `(dblno, dp_load_dt)`

## 五条必须知道的口径（否则会拿错数）

1. **`RPT_LOAN_DTL` 与 `DWM.RPT_LOAN_DTL` 是同一张宽表的两批列**，知识库里分成两段登记
   （56 列 + 75 列，列不重叠、无公共键）。这里合并成 `dwm.rpt_loan_dtl` 一张。
   不合并会得到一张没有借据号的孤表，任何 SQL 都连不上。
2. **`acct_loan.serialno` 是贷款账号，不是借据号**；借据号在 `duebillserialno`。
   `acct_loan` 与 `business_duebill` 是**一对一**（一笔借据一个核算账户），不是一对多。
3. **五级分类以余额口径推导**：0 天=正常、1–90=关注、91–180=次级、181–360=可疑、>360=损失。
   `classify_record` 有轨迹记录，`acct_loan.classifyresult` 是当前值，两者本库一致；
   但真实系统里两表可能不一致（知识库 Q1 待确认），所以测试时别默认二者永远相等。
4. **不良 = 次级 + 可疑 + 损失**（字母码 `S`/`D`/`L`），不是"逾期>90天"的等价物。
5. **数仓表必须带 `dp_load_dt`**：`dwd`/`dwm` 每张表都有两个快照日（2026-06-30、2026-09-30），
   不带快照日条件会把两个日期加在一起，金额翻倍且不报错。

## 三类刻意的偏离（都有原因）

| 偏离 | 原因 |
|---|---|
| **106 张表没建** | 知识库 `01_表知识库` 列了 125 张表，但只有 25 张有字段级定义。建一张没有列的空表只会把「表不存在」变成「列不存在」，更难查。清单见文末。 |
| **日期列建成 `date`** | 源系统把日期存成 `VARCHAR2(10)`（Oracle 贴源习惯）。照抄的话，「按放款月份汇总」得层层 `to_date`，写错一个格式就是错数。 |
| **不建物理外键** | 源系统是贴源层，`03_表关联知识库` 里有拼写错误（如 `BUSINESS_CONTRACT.SERIANO`）。照抄会建出错误约束。关系写进列注释，并导出 13 条进 MDL 供 JOIN 使用。 |

另有 205 个字段的类型是按命名推断的（知识库里「数据类型」空着，且集中在
`business_duebill` / `acct_loan` 这些核心表上），逐条列在 `01_schema.sql` 文件末尾，**需业务复核**。

## 造了多少数

客户 120（对公 60 / 个人 55 / 同业 5）· 合同 133 · 借据 233 · 账务 233 · 还款计划 12036 ·
还款流水 233 · 五级分类记录 248 · 担保合同 80 · 押品 108 · 码值 894（来自 `04_码值知识库`）·
数仓快照 466×2（两个快照日）· 押品报表 364。

金额合计 **27.47 亿元**，借据 233 笔，其中逾期 15 笔、不良（次级+可疑+损失）13 笔。

一致性约束（造数时就保证，不是事后校验）：
`acct_loan`、`business_duebill`、`dwd`、`dwm` 四个地方的**金额合计完全相同**；
余额差异只来自快照日不同（9/30 比 10/31 少还一期本金）。

## 未建表清单（106 张，缺字段定义）

| 表英文名 | 表中文名 | 主题域 |
|---|---|---|
| CUSTOMER_TEL | 客户联系电话信息列表 | 客户管理 |
| ENT_IPO | 企业发行股票信息 | 客户管理 |
| CUSTOMER_ADDRESS | 客户联系地址表 | 客户管理 |
| CUSTOMER_CERT | 客户证件信息表 | 客户管理 |
| CUSTOMER_RELATIVE | 客户关联信息（除同业） | 客户管理 |
| ENT_INFO | 企业客户基本信息 | 客户管理 |
| GROUP_INFO | 集团客户基本信息表 | 客户管理 |
| INDCAREER_INFO | 个人经营/职业信息表 | 客户管理 |
| IND_INFO | 个人客户基本信息 | 客户管理 |
| IND_OPERATE_INFO | 个体经营信息 | 客户管理 |
| IND_FAMILY | 家庭及主要成员情况 | 客户管理 |
| IND_EDUCATION | 个人学业履历 | 客户管理 |
| IND_RESUME | 个人工作履历列表 | 客户管理 |
| GROUP_MEMBER_RELATIVE | 集团客户当前成员 | 客户管理 |
| CUSTOMER_MEMO | 客户涉诉信息列表/客户大事记列表 | 客户管理 |
| REPORT_RECORD | 财务报表记录 | 客户管理 |
| REPORT_DATA | 财务报表数据 | 客户管理 |
| CUSTOMER_CRM_CORE | 主机客户对照表 | 客户管理 |
| ACCT_PUTOUT | 贷款-放款信息 | 贷后/账务 |
| BUSINESS_WASTEBOOK | 业务流水账 | 授信管理 |
| CONTRACT_RELATIVE | 合同关联表 | 合同管理 |
| GUARANTY_AUDIT | 抵质押物入库/出库信息 | 担保管理 |
| BUSINESS_EXTENSION | 业务展期记录 | 授信管理 |
| BILL_INFO | 票据信息 | 授信管理 |
| RECOVERY_APPLY | 呆账核销表 | 贷后/账务 |
| ERATE_INFO | 汇率表 | 金融市场 |
| CODE_CATALOG | 代码类别表 | 系统参数 |
| PROJECT_INFO | 项目基本信息 | 授信管理 |
| PROJECT_PROGRESS | 项目进展情况 | 授信管理 |
| PROJECT_RELATIVE | 项目关联对象 | 授信管理 |
| CL_DIVIDE | 额度切分表 | 额度管理 |
| ACCT_RATE_SEGMENT | 贷款-利率区段表 | 贷后/账务 |
| ACCT_RPT_SEGMENT | 贷款-组合还款区段表 | 贷后/账务 |
| HOUSE_INFO | 房产信息 | 客户管理 |
| ACCT_TRANS_PAYMENT | 贷款-还款交易单据信息 | 贷后/账务 |
| BUILDING_DETAIL | 楼盘-项下楼号信息 | 客户管理 |
| BUILDING_INFO | 楼盘信息 | 客户管理 |
| CUSTOMER_ANARECORD | 客户分析记录 | 客户管理 |
| CUSTOMER_BELONG | 客户所属列表 | 客户管理 |
| CUSTOMER_BOND | 客户拥有债券信息 | 客户管理 |
| CUSTOMER_CORE | 主机客户信息表 | 客户管理 |
| CUSTOMER_IMASSET | 客户无形资产信息 | 客户管理 |
| CUSTOMER_OTHER_EVALUATE | 同业客户外部评级 | 客户管理 |
| CUSTOMER_REALTY | 客户房产资产信息 | 客户管理 |
| CUSTOMER_STOCK | 客户拥有股票信息 | 客户管理 |
| CUSTOMER_TAXPAYING | 客户纳税信息 | 客户管理 |
| CUSTOMER_VEHICLE | 客户车辆资产信息 | 客户管理 |
| ENT_AUTH | 企业资质与认证信息 | 客户管理 |
| ENT_FIXEDASSETS | 企业固定资产信息 | 客户管理 |
| GROUP_EVENT | 集团客户事件 | 客户管理 |
| GUARANTY_INFO_DEFEND | 保证人信息维护表 | 客户管理 |
| IND_BI | 个人商业保险信息 | 客户管理 |
| IND_OASSET | 个人/对公其它资产信息 | 客户管理 |
| IND_RELATIVEOPERATE_INFO | 非法人企业主关联方 | 客户管理 |
| IND_ODEBT | 个人其它负债信息 | 客户管理 |
| IND_SI | 个人社会保险情况 | 客户管理 |
| CUSTOMER_ACCOUNT | 客户账户表 | 客户管理 |
| ACCT_BUSINESS_ACCOUNT | 业务账号信息 | 客户管理 |
| ACCT_GENERAL_LEDGER | 总帐信息 | 客户管理 |
| ACCT_SUBLEDGER_DETAIL | 帐务-会计分录表 | 客户管理 |
| ACCT_SUBSIDIARY_LEDGER | 帐务-分户帐表 | 客户管理 |
| ACCT_TRANSACTION | 帐务-交易信息 | 客户管理 |
| ENT_BONDISSUE | 企业发行债券信息 | 客户管理 |
| BUSINESS_PROVIDER | 其它提供贷款人(授信组织方式节点详情信息 | 授信管理 |
| RISK_SIGNAL_RELATIVE | 预警信号信息 | 风险预警 |
| ACCT_CODE_LIBRARY | 核算配置代码表 | 贷后/账务 |
| CUSTOMER_SPECIAL | 特定客户名单 | 客户管理 |
| ABS_ASSET | 项目资产信息 | 客户管理 |
| CUSTOMER_PARTNER | 合作方客户 | 客户管理 |
| EVALUATE_RECORD | 评估记录表 | 客户管理 |
| IND_SALARY | 个人工资表 | 客户管理 |
| T_FACT_LOAN_M | 报表中间表 | 贷后/账务 |
| LC_INFO | 信用证信息 | 授信管理 |
| GUARANTEE_INFO | 相关保函信息 | 授信管理 |
| BUSINESS_APPLY | 业务申请信息 | 授信管理 |
| BUSINESS_APPROVE | 业务批准信息 | 授信管理 |
| APPLY_RELATIVE | 申请关联表 | 授信管理 |
| APPROVE_RELATIVE | 批准关联表 | 授信管理 |
| CMS_COLLATERALTYPE_INFO | 押品类型目录信息 | 贷后/账务 |
| CMS_COLLATERALDONO_INFO | 押品显示模板版本信息等配置表 | 贷后/账务 |
| RATE_INFO | 利率表 | 贷后/账务 |
| CL_OCCUPY | 额度占用关系表 | 贷后/账务 |
| CONTRACT_INFO | 贸易合同信息 | 担保管理 |
| RISK_SIGNAL_OBJECT | 风险预警排查申请表 | 贷后/账务 |
| ODS_D_CUST | 纳税人信息表 | 客户管理 |
| WPD_PUTOUT | 旺铺贷支用申请 | 客户管理 |
| OPERATE_DEPOSIT | 个人商户经营流水表 | 客户管理 |
| SME_CUSTRELA | 个体经营户信息关联表 | 客户管理 |
| DUN_INFO | 催收登记表 | 贷后/账务 |
| BUSINESS_APPLICANT | 业务其他申请人 | 客户管理 |
| DWD.DWD_LAN_DBL_DIMI | 贷款借据维表 | 贷款借据 |
| DWD.DWD_LAN_DBL_SBJ | 贷款借据科目表 | 贷款账务 |
| DWD.DWD_LAN_DBL_WRTOFF_FCTI | 贷款核销事实表 | 核销催收 |
| DWD.DWD_LOAN_WRTOFF_AF_COLL_SEQ_FCT | 核销后收回事实表 | 核销催收 |
| DWD.DWD_LAN_PRD_DIMI | 贷款产品维表 | 产品信息 |
| DWD.DWD_PUB_ORG_DIMI | 公共机构维表 | 机构信息 |
| DWD.DWD_CST_IDV_DIMI | 个人客户维表 | 客户信息 |
| DWD.DWD_DEP_ACCT_DIMI | 存款账户维表 | 存款账户 |
| DWM.RPT_LOAN_DTL_MULSYS | 多系统贷款明细宽表 | 贷款借据 |
| DWM.RPT_LOAN_WRTOFF_DEP_TMP1 | 核销客户存款余额临时表 | 核销催收 |
| DWM.RPT_CODE | 码值表（数仓） | 码值参数 |
| DWM.RPT_CODE_MAPPING | 码值映射表 | 码值参数 |
| DWM.RPT_EXRT | 汇率表（数仓） | 利率 |
| DWM.RPT_MRTGPLG_DTL | 押品明细报表 | 担保物 |
| DWM.RPT_RTL_BDLN_STAT_TBL | 零售贷款统计表 | 贷款借据 |
| ODS_TABLES.LOAN_SUS_LIST | 可疑贷款清单（临时表） | 核销催收 |
