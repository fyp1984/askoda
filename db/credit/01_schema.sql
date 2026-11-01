-- ============================================================================
-- 信贷业务库 credit · 建表脚本
-- 来源：新信贷取数知识库_v0.4.xlsx → 01_表知识库 / 02_字段知识库 / 03_表关联知识库
-- 生成：tools/credit_ddl_from_kb.py（勿手改，改知识库后重新生成）
-- 生成时间：2026-11-01 10:55
--
-- 口径说明：
--   1) 知识库里有字段级定义的表共 25 张；RPT_LOAN_DTL 与 DWM.RPT_LOAN_DTL 是同一张
--      宽表的两批列（中文名相同、列不重叠、无公共键），已合并为 dwm.rpt_loan_dtl，
--      故实际建 24 张。
--   2) 01_表知识库另列的 100 张表没有字段定义，本脚本不建——建一张没有列的空表只会
--      让「表不存在」变成「列不存在」，更难查。清单见 db/credit/README.md。
--   2) 不建物理外键，关系写进列注释（FK→表.字段）。源系统为 Oracle 贴源层，
--      关系表存在拼写错误与「条件写进字段格」的行，照抄会建出错误约束。
--   3) 178 个字段的类型是按命名推断的，逐条列在文件末尾，需业务复核。
--   4) DWD./DWM. 前缀落到同名 schema，保留源系统分层。
-- ============================================================================

SET client_min_messages = warning;

CREATE SCHEMA IF NOT EXISTS dwd;
CREATE SCHEMA IF NOT EXISTS dwm;

-- dwd.dwd_lan_dbl_fcti 贷款借据事实表
CREATE TABLE dwd.dwd_lan_dbl_fcti (
  dp_load_dt               date             NOT NULL,
  syscgy                   varchar(64)      NOT NULL,
  dblno                    varchar(64)      NOT NULL,
  dblamt                   numeric(20,2)   ,
  dblbal                   numeric(20,2)   ,
  prdno                    varchar(64)     ,
  loansts                  varchar(64)     ,
  fivelvlcgycd_jg          varchar(200)    ,
  custnm                   varchar(64)     ,
  certno                   varchar(64)     ,
  bnkincustno              varchar(64)     ,
  wrtoffpnp                numeric(20,2)   ,
  wrtoffdt                 date            ,
  oper                     varchar(200)    ,
  cntrno                   varchar(64)     ,
  CONSTRAINT pk_dwd_lan_dbl_fcti PRIMARY KEY (dp_load_dt, syscgy, dblno)
);
COMMENT ON TABLE dwd.dwd_lan_dbl_fcti IS '贷款借据事实表；粒度：一行一条借据在某个快照日（DP_LOAD_DT）的事实；坑点：★ 数仓表必须带快照日条件';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.dp_load_dt IS '数据日期；快照日期（分区字段）';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.syscgy IS '系统类别；业务系统类别码（101=信贷，304=助贷，101 之外的信用卡另处理）；码值：SYSCGY';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.dblno IS '借据号；数仓口径借据主键';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.dblamt IS '借据金额；借据发放金额';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.dblbal IS '借据余额；借据当前余额';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.prdno IS '产品编号';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.loansts IS '贷款状态；贷款状态码；码值：LOANSTS';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.fivelvlcgycd_jg IS '五级分类代码；五级分类字母码；码值：FIVELVLCGYCD_JG';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.custnm IS '客户名称；借款人名称';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.certno IS '证件号码；借款人证件号码';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.bnkincustno IS '行内客户号';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.wrtoffpnp IS '核销本金；核销本金金额';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.wrtoffdt IS '核销日期';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.oper IS '经办人；经办人工号';
COMMENT ON COLUMN dwd.dwd_lan_dbl_fcti.cntrno IS '合同编号；所属合同编号';

-- dwm.rpt_loan_dtl 贷款明细报表宽表
CREATE TABLE dwm.rpt_loan_dtl (
  dblno                    varchar(64)      NOT NULL,
  cntrno                   varchar(64)     ,
  cntrseqno                varchar(64)     ,
  custno                   varchar(64)     ,
  bnkincustno              varchar(64)     ,
  custnm                   varchar(64)     ,
  certtyp                  varchar(64)     ,
  certno                   varchar(64)     ,
  custtypbcgy              varchar(64)     ,
  custtypcgy               varchar(64)     ,
  prdno                    varchar(64)     ,
  prdnm                    varchar(64)     ,
  cur                      varchar(200)    ,
  acgorgno                 varchar(64)     ,
  acgorgnm                 varchar(64)     ,
  blgbrchno                varchar(64)     ,
  blgbrchnm                varchar(64)     ,
  mngorgno                 varchar(64)     ,
  mngorgnm                 varchar(64)     ,
  orgjuriscd               varchar(64)     ,
  orgjurisnm               varchar(64)     ,
  greencrdtidx             varchar(200)    ,
  greenproportion          numeric(20,2)   ,
  setlacctno               varchar(64)     ,
  loanacctno               varchar(64)     ,
  loancgylv1               varchar(200)    ,
  loancgylv2               varchar(200)    ,
  loancgylv3               varchar(200)    ,
  loancgylv4               varchar(200)    ,
  hsmrtgflg                varchar(64)     ,
  hpntyp                   varchar(64)     ,
  loandect                 varchar(200)    ,
  loandectlv1              varchar(200)    ,
  loandectlv2              varchar(200)    ,
  loandectlv3              varchar(200)    ,
  loandectlv4              varchar(200)    ,
  loantyp                  varchar(64)     ,
  loanuse                  varchar(200)    ,
  loanterm                 varchar(200)    ,
  loantermcgy              varchar(64)     ,
  grtmode                  varchar(200)    ,
  grtform                  varchar(200)    ,
  gnrnmgrp                 varchar(200)    ,
  grtynmgrp                varchar(200)    ,
  addgrtmode               varchar(200)    ,
  rpymodecd                varchar(64)     ,
  rpymode                  varchar(200)    ,
  fivelvlcgy               varchar(64)     ,
  fivelvlcgycd_jg          varchar(200)    ,
  yrbgnfivelvlcgy          varchar(64)     ,
  mobgnfivelvlcgy          varchar(64)     ,
  operorgnm                varchar(64)     ,
  bsnsts                   varchar(64)     ,
  dblsts                   varchar(64)     ,
  valtdt                   date            ,
  expdt                    date            ,
  actexpdt                 date            ,
  cnlacctdt                date            ,
  loanwrtoffmemocd         varchar(64)     ,
  loanrate                 numeric(20,6)   ,
  cntramt                  numeric(20,2)   ,
  dblamt                   numeric(20,2)   ,
  dblbal                   numeric(20,2)   ,
  dblbalcnvtcny            numeric(20,2)   ,
  oduebal                  numeric(20,2)   ,
  oduedys                  varchar(200)    ,
  pnpoduedys               varchar(200)    ,
  intoduedys               varchar(200)    ,
  maxoduedys               varchar(200)    ,
  onshtdbtintbal           numeric(20,2)   ,
  offshtdbtintbal          numeric(20,2)   ,
  pnpoduepnyint            varchar(200)    ,
  intoduepnyint            varchar(200)    ,
  entrbaddt                date            ,
  twllvlcgycd              varchar(64)     ,
  datadt                   date            ,
  distributionkey          text            ,
  custtypscgy              text            ,
  oldbsnkind               text            ,
  curcd                    text            ,
  rgstaddrblgcntycd        text            ,
  rgstaddrblgcnty          text            ,
  operaddrblgcntycd        text            ,
  operaddrblgcnty          text            ,
  hsaddr                   text            ,
  hshldrgstaddr            text            ,
  pecoprtloanflg           text            ,
  tholflg                  text            ,
  indscgy                  text            ,
  indscgylv1               text            ,
  indscgylv2               text            ,
  indscgylv3               text            ,
  indscgylv4               text            ,
  entpscl                  text            ,
  holdtyp                  text            ,
  farmflgdesc              text            ,
  offsiteloanflg           text            ,
  peprofcoprtloanflg       text            ,
  agtrloanflg              text            ,
  agtrloantyp              text            ,
  agtrloantyplv1           text            ,
  agtrloantyplv2           text            ,
  agtrloantyplv3           text            ,
  agtrloantyplv4           text            ,
  inofftabflgdbl           text            ,
  inofftabflgcntr          text            ,
  loantermcgycd            text            ,
  csnrnm                   text            ,
  csnrattr                 text            ,
  csnrindscgy              text            ,
  opertlrno                text            ,
  operorgno                text            ,
  lgopnbnk                 text            ,
  lnafmngpsnno             text            ,
  lnafmngpsnnm             text            ,
  billtyp                  text            ,
  billno                   text            ,
  billsts                  text            ,
  guarpct                  numeric(10,6)   ,
  guarcur                  text            ,
  guaramt                  numeric(38,6)   ,
  yraccmdblbal             numeric(38,6)   ,
  yrbgndblbal              numeric(38,6)   ,
  accmdbtintbal            numeric(38,6)   ,
  drctsbsdintadj           numeric(38,6)   ,
  actocpscl                numeric(38,6)   ,
  bsnlcnsno                text            ,
  bsnlcnsno2               text            ,
  mngorgblgbrch            text            ,
  focusedindustry          text            ,
  city                     text            ,
  dp_load_dt               date             NOT NULL,
  CONSTRAINT pk_rpt_loan_dtl PRIMARY KEY (dblno, dp_load_dt)
);
COMMENT ON TABLE dwm.rpt_loan_dtl IS '贷款明细报表宽表；粒度：一行一条借据（多系统统一口径）；坑点：★ 数仓表必须带快照日条件；★ 数仓表必须带快照日条件；★ **本表无 SYSCGY 字段**（SYSCGY 仅存在于 DWD 层借据事实表/维表与贴源系统表），禁止套用 DWD 联合主键写法 → 会报 Invalid column reference（2026-09-11 实证）';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dblno IS '借据编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.cntrno IS '合同编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.cntrseqno IS '合同流水号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.custno IS '客户编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.bnkincustno IS '行内客户号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.custnm IS '客户名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.certtyp IS '证件类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.certno IS '证件号码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.custtypbcgy IS '客户类型大类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.custtypcgy IS '客户类型小类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.prdno IS '产品编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.prdnm IS '产品名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.cur IS '币种';
COMMENT ON COLUMN dwm.rpt_loan_dtl.acgorgno IS '核算机构编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.acgorgnm IS '核算机构名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.blgbrchno IS '所属分行编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.blgbrchnm IS '所属分行名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.mngorgno IS '贷后管理机构编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.mngorgnm IS '贷后管理机构名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.orgjuriscd IS '机构辖区代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.orgjurisnm IS '机构辖区名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.greencrdtidx IS '绿色信贷指标';
COMMENT ON COLUMN dwm.rpt_loan_dtl.greenproportion IS '绿色贷款所占比重';
COMMENT ON COLUMN dwm.rpt_loan_dtl.setlacctno IS '结算账号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loanacctno IS '贷款账号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loancgylv1 IS '贷款分类一级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loancgylv2 IS '贷款分类二级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loancgylv3 IS '贷款分类三级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loancgylv4 IS '贷款分类四级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.hsmrtgflg IS '住房按揭标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.hpntyp IS '发生类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loandect IS '贷款投向';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loandectlv1 IS '贷款投向一级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loandectlv2 IS '贷款投向二级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loandectlv3 IS '贷款投向三级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loandectlv4 IS '贷款投向四级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loantyp IS '贷款类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loanuse IS '贷款用途';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loanterm IS '贷款期限';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loantermcgy IS '贷款期限分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.grtmode IS '担保方式';
COMMENT ON COLUMN dwm.rpt_loan_dtl.grtform IS '担保形式';
COMMENT ON COLUMN dwm.rpt_loan_dtl.gnrnmgrp IS '担保人名称组';
COMMENT ON COLUMN dwm.rpt_loan_dtl.grtynmgrp IS '担保物名称组';
COMMENT ON COLUMN dwm.rpt_loan_dtl.addgrtmode IS '附加担保方式';
COMMENT ON COLUMN dwm.rpt_loan_dtl.rpymodecd IS '还款方式代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.rpymode IS '还款方式';
COMMENT ON COLUMN dwm.rpt_loan_dtl.fivelvlcgy IS '五级分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.fivelvlcgycd_jg IS '五级分类代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.yrbgnfivelvlcgy IS '年初五级分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.mobgnfivelvlcgy IS '月初五级分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.operorgnm IS '经办机构名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.bsnsts IS '业务状态';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dblsts IS '借据状态';
COMMENT ON COLUMN dwm.rpt_loan_dtl.valtdt IS '起息日期';
COMMENT ON COLUMN dwm.rpt_loan_dtl.expdt IS '到期日期';
COMMENT ON COLUMN dwm.rpt_loan_dtl.actexpdt IS '实际到期日期';
COMMENT ON COLUMN dwm.rpt_loan_dtl.cnlacctdt IS '销户日期';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loanwrtoffmemocd IS '贷款核销摘要代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loanrate IS '贷款利率';
COMMENT ON COLUMN dwm.rpt_loan_dtl.cntramt IS '合同金额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dblamt IS '借据金额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dblbal IS '借据余额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dblbalcnvtcny IS '借据余额折人民币';
COMMENT ON COLUMN dwm.rpt_loan_dtl.oduebal IS '逾期余额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.oduedys IS '逾期天数';
COMMENT ON COLUMN dwm.rpt_loan_dtl.pnpoduedys IS '本金逾期天数';
COMMENT ON COLUMN dwm.rpt_loan_dtl.intoduedys IS '利息逾期天数';
COMMENT ON COLUMN dwm.rpt_loan_dtl.maxoduedys IS '最大逾期天数';
COMMENT ON COLUMN dwm.rpt_loan_dtl.onshtdbtintbal IS '表内欠息';
COMMENT ON COLUMN dwm.rpt_loan_dtl.offshtdbtintbal IS '表外欠息';
COMMENT ON COLUMN dwm.rpt_loan_dtl.pnpoduepnyint IS '本金罚息';
COMMENT ON COLUMN dwm.rpt_loan_dtl.intoduepnyint IS '利息罚息';
COMMENT ON COLUMN dwm.rpt_loan_dtl.entrbaddt IS '纳入不良时间';
COMMENT ON COLUMN dwm.rpt_loan_dtl.twllvlcgycd IS '风险分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.datadt IS '数据日期';
COMMENT ON COLUMN dwm.rpt_loan_dtl.distributionkey IS '分桶键';
COMMENT ON COLUMN dwm.rpt_loan_dtl.custtypscgy IS '客户类型小类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.oldbsnkind IS '老业务品种';
COMMENT ON COLUMN dwm.rpt_loan_dtl.curcd IS '币种代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.rgstaddrblgcntycd IS '注册地所属县代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.rgstaddrblgcnty IS '注册地所属县';
COMMENT ON COLUMN dwm.rpt_loan_dtl.operaddrblgcntycd IS '经营地所属县代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.operaddrblgcnty IS '经营地所属县';
COMMENT ON COLUMN dwm.rpt_loan_dtl.hsaddr IS '住宅地址';
COMMENT ON COLUMN dwm.rpt_loan_dtl.hshldrgstaddr IS '户籍地址';
COMMENT ON COLUMN dwm.rpt_loan_dtl.pecoprtloanflg IS '农民合作社贷款标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.tholflg IS '两高一剩标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.indscgy IS '行业分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.indscgylv1 IS '行业分类一级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.indscgylv2 IS '行业分类二级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.indscgylv3 IS '行业分类三级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.indscgylv4 IS '行业分类四级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.entpscl IS '企业规模';
COMMENT ON COLUMN dwm.rpt_loan_dtl.holdtyp IS '控股类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.farmflgdesc IS '农户标志描述';
COMMENT ON COLUMN dwm.rpt_loan_dtl.offsiteloanflg IS '异地贷款标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.peprofcoprtloanflg IS '农民专业合作社贷款标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloanflg IS '涉农贷款标志';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloantyp IS '涉农贷款类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloantyplv1 IS '涉农贷款类型一级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloantyplv2 IS '涉农贷款类型二级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloantyplv3 IS '涉农贷款类型三级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.agtrloantyplv4 IS '涉农贷款类型四级';
COMMENT ON COLUMN dwm.rpt_loan_dtl.inofftabflgdbl IS '表内外标志借据';
COMMENT ON COLUMN dwm.rpt_loan_dtl.inofftabflgcntr IS '表内外标志合同';
COMMENT ON COLUMN dwm.rpt_loan_dtl.loantermcgycd IS '贷款期限分类代码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.csnrnm IS '委托人名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.csnrattr IS '委托人性质';
COMMENT ON COLUMN dwm.rpt_loan_dtl.csnrindscgy IS '委托人行业分类';
COMMENT ON COLUMN dwm.rpt_loan_dtl.opertlrno IS '经办柜员编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.operorgno IS '经办机构编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.lgopnbnk IS '保函开立行';
COMMENT ON COLUMN dwm.rpt_loan_dtl.lnafmngpsnno IS '贷后管理人员编号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.lnafmngpsnnm IS '贷后管理人员名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.billtyp IS '票据类型';
COMMENT ON COLUMN dwm.rpt_loan_dtl.billno IS '票据号码';
COMMENT ON COLUMN dwm.rpt_loan_dtl.billsts IS '票据状态';
COMMENT ON COLUMN dwm.rpt_loan_dtl.guarpct IS '保证金比例';
COMMENT ON COLUMN dwm.rpt_loan_dtl.guarcur IS '保证金币种';
COMMENT ON COLUMN dwm.rpt_loan_dtl.guaramt IS '保证金金额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.yraccmdblbal IS '年累计借据余额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.yrbgndblbal IS '年初借据余额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.accmdbtintbal IS '累计欠息余额';
COMMENT ON COLUMN dwm.rpt_loan_dtl.drctsbsdintadj IS '直贴利息调整';
COMMENT ON COLUMN dwm.rpt_loan_dtl.actocpscl IS '实际占用规模';
COMMENT ON COLUMN dwm.rpt_loan_dtl.bsnlcnsno IS '营业执照号';
COMMENT ON COLUMN dwm.rpt_loan_dtl.bsnlcnsno2 IS '营业执照号2';
COMMENT ON COLUMN dwm.rpt_loan_dtl.mngorgblgbrch IS '贷后管理机构所属分行名称';
COMMENT ON COLUMN dwm.rpt_loan_dtl.focusedindustry IS '重点关注行业';
COMMENT ON COLUMN dwm.rpt_loan_dtl.city IS '所在国家或地区';
COMMENT ON COLUMN dwm.rpt_loan_dtl.dp_load_dt IS '数据日期（快照日）；按 01_表知识库 的粒度补入，02_字段知识库未列';

-- public.acct_loan 贷款-贷款信息
CREATE TABLE public.acct_loan (
  serialno                 varchar(40)      NOT NULL,
  accountno                varchar(40)     ,
  contractserialno         varchar(40)     ,
  customerid               varchar(40)     ,
  customername             varchar(80)     ,
  businesstype             varchar(40)     ,
  productid                varchar(40)     ,
  specificid               varchar(40)     ,
  versionid                varchar(40)     ,
  currency                 varchar(10)     ,
  businesssum              numeric(20,2)   ,
  putoutdate               date            ,
  maturitydate             date            ,
  originalmaturitydate     date            ,
  operateorgid             varchar(40)     ,
  accountingorgid          varchar(32)     ,
  loanstatus               varchar(10)     ,
  finishdate               date            ,
  businessdate             date            ,
  lockflag                 varchar(10)     ,
  overduedays              integer         ,
  classifyresult           varchar(10)     ,
  putoutserialno           varchar(40)     ,
  approveserialno          varchar(40)     ,
  applyserialno            varchar(40)     ,
  businessstatus           varchar(10)     ,
  originalcorpuspaymethod  varchar(20)     ,
  originalvouchtype        varchar(18)     ,
  iterflag                 varchar(18)     ,
  principaloverduedays     numeric(22)     ,
  interestoverduedays      numeric(22)     ,
  haverecourse             varchar(10)     ,
  wpdserialno              varchar(32)     ,
  duebillserialno          varchar(32)     ,
  normalbalance            numeric(20,2)   ,
  odintebalance            numeric(20,2)   ,
  overduebalance           numeric(20,2)   ,
  compdintebalance         numeric(20,2)   ,
  fineintebalance          numeric(20,2)   ,
  subsidystatus            varchar(10)     ,
  transferflag             varchar(2)      ,
  stopcompdflag            varchar(2)      ,
  stopfineflag             varchar(2)      ,
  payruletypeflag          varchar(18)     ,
  farmerscopeflag          varchar(10)     ,
  isindz                   varchar(8)      ,
  CONSTRAINT pk_acct_loan PRIMARY KEY (serialno)
);
COMMENT ON TABLE public.acct_loan IS '贷款-贷款信息；粒度：一行一笔核算借据（账务账户）；坑点：★ 余额为当前快照，不可用于历史时点；多币种不可直接 SUM';
COMMENT ON COLUMN public.acct_loan.serialno IS '贷款账号；贷款的业务主键，唯一标识一笔贷款（借据级账务账户）；FK→public.business_duebill.loanaccountno';
COMMENT ON COLUMN public.acct_loan.accountno IS '贷款文本账号';
COMMENT ON COLUMN public.acct_loan.contractserialno IS '关联合同号；该笔贷款对应的业务合同编号';
COMMENT ON COLUMN public.acct_loan.customerid IS '客户编号；该笔贷款所属客户的编号';
COMMENT ON COLUMN public.acct_loan.customername IS '客户名称';
COMMENT ON COLUMN public.acct_loan.businesstype IS '业务品种；FK→public.business_type.typeno';
COMMENT ON COLUMN public.acct_loan.productid IS '产品编号';
COMMENT ON COLUMN public.acct_loan.specificid IS '特殊编号';
COMMENT ON COLUMN public.acct_loan.versionid IS '版本编号；固定V1.0';
COMMENT ON COLUMN public.acct_loan.currency IS '贷款币种；该笔贷款的计价币种；码值：CURRENCY';
COMMENT ON COLUMN public.acct_loan.businesssum IS '贷款金额；该笔贷款的发放金额（本金）';
COMMENT ON COLUMN public.acct_loan.putoutdate IS '贷款发放日期；该笔贷款的放款日期';
COMMENT ON COLUMN public.acct_loan.maturitydate IS '贷款到期日；该笔贷款的合同到期日';
COMMENT ON COLUMN public.acct_loan.originalmaturitydate IS '贷款原始到期日';
COMMENT ON COLUMN public.acct_loan.operateorgid IS '经办行号（用做异地支行标识）';
COMMENT ON COLUMN public.acct_loan.accountingorgid IS '贷款入账机构';
COMMENT ON COLUMN public.acct_loan.loanstatus IS '贷款状态；该笔贷款当前所处的生命周期状态；码值：LOANSTATUS';
COMMENT ON COLUMN public.acct_loan.finishdate IS '结清日期';
COMMENT ON COLUMN public.acct_loan.businessdate IS '贷款处理日期';
COMMENT ON COLUMN public.acct_loan.lockflag IS '锁定标识；贷款处理时锁定，防止进行其他更新操作';
COMMENT ON COLUMN public.acct_loan.overduedays IS '逾期天数';
COMMENT ON COLUMN public.acct_loan.classifyresult IS '分类结果；该笔贷款当前的五级分类结果；码值：CLASSIFYRESULT';
COMMENT ON COLUMN public.acct_loan.putoutserialno IS '出账流水号；出账时的流水号business_putout.serialno';
COMMENT ON COLUMN public.acct_loan.approveserialno IS '审批流水号；审批时的流水号business_approve.serialno';
COMMENT ON COLUMN public.acct_loan.applyserialno IS '申请流水号；申请时的流水号business_apply.serialno';
COMMENT ON COLUMN public.acct_loan.businessstatus IS '业务状态；码值：BUSINESSSTATUS';
COMMENT ON COLUMN public.acct_loan.originalcorpuspaymethod IS '原始还款方式';
COMMENT ON COLUMN public.acct_loan.originalvouchtype IS '原始主要担保方式-放款时初始化，后续不能更改';
COMMENT ON COLUMN public.acct_loan.iterflag IS '是否国结业务-代码YesNo；码值：ITERFLAG';
COMMENT ON COLUMN public.acct_loan.principaloverduedays IS '本金逾期天数';
COMMENT ON COLUMN public.acct_loan.interestoverduedays IS '利息逾期天数';
COMMENT ON COLUMN public.acct_loan.haverecourse IS '保理有无追索权；保理业务是否有追索权';
COMMENT ON COLUMN public.acct_loan.wpdserialno IS '对方业务流水号；旺铺贷业务的流水号';
COMMENT ON COLUMN public.acct_loan.duebillserialno IS '借据号；BUSINESS_DUEBILL.SERIALNO 借据表,借据号';
COMMENT ON COLUMN public.acct_loan.normalbalance IS '正常贷款余额；当前时点该笔贷款处于正常状态的未偿本金余额';
COMMENT ON COLUMN public.acct_loan.odintebalance IS '欠息余额；已计提但尚未收回的利息余额';
COMMENT ON COLUMN public.acct_loan.overduebalance IS '逾期余额；当前时点该笔贷款已逾期部分的未偿本金余额';
COMMENT ON COLUMN public.acct_loan.compdintebalance IS '逾期本金罚息';
COMMENT ON COLUMN public.acct_loan.fineintebalance IS '逾期利息罚息';
COMMENT ON COLUMN public.acct_loan.subsidystatus IS '贴息状态；码值：SUBSIDYSTATUS';
COMMENT ON COLUMN public.acct_loan.transferflag IS '数据移植标志；老系统移植时，产生的标志';
COMMENT ON COLUMN public.acct_loan.stopcompdflag IS '停复利计算标识(1-停 2-不停)；码值：STOPCOMPDFLAG';
COMMENT ON COLUMN public.acct_loan.stopfineflag IS '停罚息计算标识(1-停 2-不停)；码值：STOPFINEFLAG';
COMMENT ON COLUMN public.acct_loan.payruletypeflag IS '还款顺序';
COMMENT ON COLUMN public.acct_loan.farmerscopeflag IS '农户小微贷款标志；码值：FARMERSCOPEFLAG';
COMMENT ON COLUMN public.acct_loan.isindz IS '是否执行过以物抵债交易；码值：ISINDZ';

-- public.acct_payment_log 贷款-还款日志
CREATE TABLE public.acct_payment_log (
  relativeobjectno         varchar(64)     ,
  actualpaydate            date            ,
  actualpayprincipalamt    numeric(20,2)   ,
  actualpayinterestamt     numeric(20,2)   
);
COMMENT ON TABLE public.acct_payment_log IS '贷款-还款日志；粒度：一行一条还款流水（一借据多条）；坑点：★ 一流水一行，须先按借据聚合';
COMMENT ON COLUMN public.acct_payment_log.relativeobjectno IS '还款主体对象编号；对应贷款的账务借据号';
COMMENT ON COLUMN public.acct_payment_log.actualpaydate IS '实际支付日期；实际还款日期';
COMMENT ON COLUMN public.acct_payment_log.actualpayprincipalamt IS '实还本金；本次实还本金';
COMMENT ON COLUMN public.acct_payment_log.actualpayinterestamt IS '实还利息；本次实还利息';

-- public.acct_payment_schedule 贷款-还款计划
CREATE TABLE public.acct_payment_schedule (
  serialno                 varchar(40)      NOT NULL,
  parentserialno           varchar(40)     ,
  objecttype               varchar(32)     ,
  objectno                 varchar(40)     ,
  relativeobjecttype       varchar(40)     ,
  relativeobjectno         varchar(40)     ,
  periodno                 integer         ,
  paydate                  date            ,
  pstype                   varchar(10)     ,
  payitemcode              varchar(10)     ,
  payprincipalamt          numeric(20,2)   ,
  actualpayprincipalamt    numeric(20,2)   ,
  CONSTRAINT pk_acct_payment_schedule PRIMARY KEY (serialno)
);
COMMENT ON TABLE public.acct_payment_schedule IS '贷款-还款计划；粒度：一行一期还款计划（一借据多期）；坑点：★ 一期一行，按揭可达数百期，直接 JOIN 放大行数';
COMMENT ON COLUMN public.acct_payment_schedule.serialno IS '流水号；业务主键';
COMMENT ON COLUMN public.acct_payment_schedule.parentserialno IS '父还款计划流水号；特殊还款时，要关联原有的流水号(SERIALNO)';
COMMENT ON COLUMN public.acct_payment_schedule.objecttype IS '还款日志关联对象类型；一般为jbo.acct.ACCT_LOAN；码值：OBJECTTYPE';
COMMENT ON COLUMN public.acct_payment_schedule.objectno IS '还款日志关联对象编号；一般为ACCT_LOAN.SERIALNO';
COMMENT ON COLUMN public.acct_payment_schedule.relativeobjecttype IS '还款主体对象类型；一般为jbo.acct.ACCT_LOAN';
COMMENT ON COLUMN public.acct_payment_schedule.relativeobjectno IS '还款主体对象编号；一般为ACCT_LOAN.SERIALNO';
COMMENT ON COLUMN public.acct_payment_schedule.periodno IS '期次';
COMMENT ON COLUMN public.acct_payment_schedule.paydate IS '应还日期';
COMMENT ON COLUMN public.acct_payment_schedule.pstype IS '偿付类型；码值：PSTYPE';
COMMENT ON COLUMN public.acct_payment_schedule.payitemcode IS '还款项目';
COMMENT ON COLUMN public.acct_payment_schedule.payprincipalamt IS '应还本金；计划应还本金';
COMMENT ON COLUMN public.acct_payment_schedule.actualpayprincipalamt IS '实还本金；实际已还本金';

-- public.business_contract 业务合同信息
CREATE TABLE public.business_contract (
  serialno                 varchar(64)      NOT NULL,
  artificialno             varchar(64)     ,
  customerid               varchar(64)     ,
  businesstype             varchar(64)     ,
  businesssum              numeric(20,2)   ,
  vouchtype                varchar(64)     ,
  status                   varchar(64)     ,
  classifyresult           varchar(200)    ,
  manageuserid             varchar(64)     ,
  manageorgid              varchar(64)     ,
  accountorgid             varchar(64)     ,
  CONSTRAINT pk_business_contract PRIMARY KEY (serialno)
);
COMMENT ON TABLE public.business_contract IS '业务合同信息；粒度：一行一份业务合同（授信合同/业务合同）；坑点：★ 与借据为一对多，按合同统计时须先聚合借据侧，否则行数放大';
COMMENT ON COLUMN public.business_contract.serialno IS '合同流水号；合同业务主键；FK→public.business_duebill.relativeserialno2';
COMMENT ON COLUMN public.business_contract.artificialno IS '文本合同流水号；文本合同编号';
COMMENT ON COLUMN public.business_contract.customerid IS '客户ID；合同所属客户';
COMMENT ON COLUMN public.business_contract.businesstype IS '业务类型；合同业务品种码值；码值：资产业务品种';
COMMENT ON COLUMN public.business_contract.businesssum IS '合同金额；合同约定的金额';
COMMENT ON COLUMN public.business_contract.vouchtype IS '主要担保方式；合同的主要担保方式；码值：VouchType';
COMMENT ON COLUMN public.business_contract.status IS '状态；合同状态（英文码）；码值：STATUS';
COMMENT ON COLUMN public.business_contract.classifyresult IS '分类结果；合同上的分类结果；码值：CLASSIFYRESULT';
COMMENT ON COLUMN public.business_contract.manageuserid IS '管户客户经理；合同的责任人（管户客户经理）';
COMMENT ON COLUMN public.business_contract.manageorgid IS '管理机构；合同的贷后管理机构';
COMMENT ON COLUMN public.business_contract.accountorgid IS '入账机构；合同的入账机构';

-- public.business_duebill 业务借据(账户)信息
CREATE TABLE public.business_duebill (
  serialno                 varchar(64)      NOT NULL,
  relativeserialno2        varchar(200)    ,
  mfcustomerid             varchar(64)     ,
  customerid               varchar(64)     ,
  businesstype             varchar(64)     ,
  businessstatus           varchar(64)     ,
  businesscurrency         varchar(200)    ,
  businesssum              numeric(20,2)   ,
  balance                  numeric(20,2)   ,
  normalbalance            numeric(20,2)   ,
  overduebalance           numeric(20,2)   ,
  putoutdate               date            ,
  maturity                 date            ,
  actualmaturity           date            ,
  finishdate               date            ,
  CONSTRAINT pk_business_duebill PRIMARY KEY (serialno)
);
COMMENT ON TABLE public.business_duebill IS '业务借据(账户)信息；粒度：一行一笔借据；坑点：★ 与担保/展期/票据直接 JOIN 会放大行数';
COMMENT ON COLUMN public.business_duebill.serialno IS '借据流水号；借据的业务主键，唯一标识一笔借据';
COMMENT ON COLUMN public.business_duebill.relativeserialno2 IS '关联合同流水号；该借据所属业务合同的流水号';
COMMENT ON COLUMN public.business_duebill.mfcustomerid IS 'ECIF客户号；ECIF 系统客户编号';
COMMENT ON COLUMN public.business_duebill.customerid IS '客户id；借款人客户编号（信贷侧）';
COMMENT ON COLUMN public.business_duebill.businesstype IS '业务类型；该借据的业务品种码值；码值：资产业务品种';
COMMENT ON COLUMN public.business_duebill.businessstatus IS '业务形态；借据的业务形态（码值）；码值：BusinessStatus';
COMMENT ON COLUMN public.business_duebill.businesscurrency IS '币种；该借据的计价币种；码值：币种（XD999999）';
COMMENT ON COLUMN public.business_duebill.businesssum IS '借据金额；该借据的发放金额（本金）';
COMMENT ON COLUMN public.business_duebill.balance IS '贷款金额；该借据当前未偿余额（本金口径）';
COMMENT ON COLUMN public.business_duebill.normalbalance IS '正常余额；正常状态下未偿本金余额';
COMMENT ON COLUMN public.business_duebill.overduebalance IS '逾期金额；已逾期部分未偿本金余额';
COMMENT ON COLUMN public.business_duebill.putoutdate IS '放款日期；该借据的放款日期';
COMMENT ON COLUMN public.business_duebill.maturity IS '到期日期；合同约定的原始到期日';
COMMENT ON COLUMN public.business_duebill.actualmaturity IS '实际到期日期；展期后的实际到期日';
COMMENT ON COLUMN public.business_duebill.finishdate IS '结清日期；借据结清日期，为空表示未结清';

-- public.business_type 资产业务品种表
CREATE TABLE public.business_type (
  typeno                   varchar(64)      NOT NULL,
  typename                 varchar(64)     ,
  isinuse                  varchar(200)    ,
  CONSTRAINT pk_business_type PRIMARY KEY (typeno)
);
COMMENT ON TABLE public.business_type IS '资产业务品种表；粒度：一行一个资产业务品种';
COMMENT ON COLUMN public.business_type.typeno IS '业务码值；产品主键码值';
COMMENT ON COLUMN public.business_type.typename IS '业务名称；产品名称';
COMMENT ON COLUMN public.business_type.isinuse IS '状态；产品是否在用';

-- public.classify_record 五级分类数据
CREATE TABLE public.classify_record (
  objectno                 varchar(64)     ,
  classifyresult           varchar(200)    
);
COMMENT ON TABLE public.classify_record IS '五级分类数据；粒度：一行一个分类对象的分类记录；坑点：★ 与 ACCT_LOAN.CLASSIFYRESULT 两套口径，待业务确认';
COMMENT ON COLUMN public.classify_record.objectno IS '对象编号；分类对象的编号';
COMMENT ON COLUMN public.classify_record.classifyresult IS '分类结果；分类结果码值；码值：CLASSIFYRESULT';

-- public.code_library 码值表
CREATE TABLE public.code_library (
  codeno                   varchar(64)      NOT NULL,
  itemno                   varchar(64)      NOT NULL,
  itemname                 varchar(64)     ,
  colname                  varchar(64)     ,
  coleditsource            varchar(400)    ,
  CONSTRAINT pk_code_library PRIMARY KEY (codeno, itemno)
);
COMMENT ON TABLE public.code_library IS '码值表；粒度：一行一个码值（CODENO+ITEMNO）；坑点：★ 码值标准查法：按 COLNAME 检索，取 coleditsource';
COMMENT ON COLUMN public.code_library.codeno IS '码值类型；码值集标识（等于字段名）';
COMMENT ON COLUMN public.code_library.itemno IS '码值；码值编码';
COMMENT ON COLUMN public.code_library.itemname IS '码值名称；码值对应的中文名称';
COMMENT ON COLUMN public.code_library.colname IS '码值对应的字段名（05_业务规则库 RULE_018 的标准查法要求）；按知识库规则补入';
COMMENT ON COLUMN public.code_library.coleditsource IS '码值编辑来源/可选值串（同上）；按知识库规则补入';

-- public.customer_fsrecord 客户财务报表记录
CREATE TABLE public.customer_fsrecord (
  customerid               varchar(40)      NOT NULL,
  recordno                 varchar(40)      NOT NULL,
  reportdate               date             NOT NULL,
  reportscope              varchar(18)     ,
  CONSTRAINT pk_customer_fsrecord PRIMARY KEY (recordno, reportdate)
);
COMMENT ON TABLE public.customer_fsrecord IS '客户财务报表记录；粒度：一行一个客户的一份财务报表记录';
COMMENT ON COLUMN public.customer_fsrecord.customerid IS '客户编号；信贷客户编号';
COMMENT ON COLUMN public.customer_fsrecord.recordno IS '记录号；业务主键';
COMMENT ON COLUMN public.customer_fsrecord.reportdate IS '报表截至日期；VARCHAR2(10)';
COMMENT ON COLUMN public.customer_fsrecord.reportscope IS '报表口径；码值：REPORTSCOPE';

-- public.customer_info 客户基本信息
CREATE TABLE public.customer_info (
  customerid               varchar(40)      NOT NULL,
  customername             varchar(80)     ,
  customertype             varchar(20)     ,
  certtype                 varchar(20)     ,
  certid                   varchar(40)     ,
  customerpassword         varchar(20)     ,
  inputorgid               varchar(32)     ,
  inputuserid              varchar(32)     ,
  inputdate                date            ,
  remark                   varchar(250)    ,
  mfcustomerid             varchar(40)     ,
  status                   varchar(20)     ,
  belonggroupid            varchar(40)     ,
  channel                  varchar(18)     ,
  loancardno               varchar(32)     ,
  customerscale            varchar(20)     ,
  nationcode               varchar(40)     ,
  forbidstatus             varchar(12)     ,
  mfcustomername           varchar(80)     ,
  isbadcustomer            varchar(10)     ,
  riskalarmlevel           varchar(18)     ,
  merno                    varchar(60)     ,
  custst                   varchar(20)     ,
  mainframeorgid           varchar(32)     ,
  CONSTRAINT pk_customer_info PRIMARY KEY (customerid)
);
COMMENT ON TABLE public.customer_info IS '客户基本信息；粒度：一行一个客户（含对公/对私/同业/集团，由 CUSTOMERTYPE 区分）；坑点：★ 客户资产类表（约40张）交叉 JOIN 会笛卡尔放大，一次最多关联一张';
COMMENT ON COLUMN public.customer_info.customerid IS '客户编号；业务主键 信贷客户编号 和核心客户编号ECIF客户编号一致';
COMMENT ON COLUMN public.customer_info.customername IS '客户名称';
COMMENT ON COLUMN public.customer_info.customertype IS '客户类型；码值：CUSTOMERTYPE';
COMMENT ON COLUMN public.customer_info.certtype IS '证件类型；码值：CERTTYPE';
COMMENT ON COLUMN public.customer_info.certid IS '证件号';
COMMENT ON COLUMN public.customer_info.customerpassword IS '客户口令';
COMMENT ON COLUMN public.customer_info.inputorgid IS '登记机构；信贷机构号；FK→public.org_info.orgid';
COMMENT ON COLUMN public.customer_info.inputuserid IS '登记人';
COMMENT ON COLUMN public.customer_info.inputdate IS '登记日期';
COMMENT ON COLUMN public.customer_info.remark IS '备注';
COMMENT ON COLUMN public.customer_info.mfcustomerid IS '核心客户号；ECIF系统客户编号';
COMMENT ON COLUMN public.customer_info.status IS '认定状态；码值：STATUS';
COMMENT ON COLUMN public.customer_info.belonggroupid IS '所属集团编号；集团客户使用：集团客户的客户编号';
COMMENT ON COLUMN public.customer_info.channel IS '来源渠道';
COMMENT ON COLUMN public.customer_info.loancardno IS '贷款卡编号';
COMMENT ON COLUMN public.customer_info.customerscale IS '客户规模（区分中小企业）；码值：CUSTOMERSCALE';
COMMENT ON COLUMN public.customer_info.nationcode IS '国籍';
COMMENT ON COLUMN public.customer_info.forbidstatus IS '状态';
COMMENT ON COLUMN public.customer_info.mfcustomername IS '核心客户名称；ECIF系统客户编号';
COMMENT ON COLUMN public.customer_info.isbadcustomer IS '是否资产保全问题移交客户；码值：ISBADCUSTOMER';
COMMENT ON COLUMN public.customer_info.riskalarmlevel IS '风险预警认定结果';
COMMENT ON COLUMN public.customer_info.merno IS '旺铺贷商户ID';
COMMENT ON COLUMN public.customer_info.custst IS '客户标志；码值：CUSTST';
COMMENT ON COLUMN public.customer_info.mainframeorgid IS '开户机构；统一认证机构号';

-- public.flow_task 流程任务
CREATE TABLE public.flow_task (
  objectno                 varchar(64)     ,
  objecttype               varchar(64)     ,
  phasename                varchar(64)     ,
  userid                   varchar(64)     ,
  begintime                date            ,
  phaseaction              varchar(200)    
);
COMMENT ON TABLE public.flow_task IS '流程任务；粒度：一行一个流程节点（一借据/申请对应多行）；坑点：需要补充';
COMMENT ON COLUMN public.flow_task.objectno IS '对象编号；流程对应的业务对象编号';
COMMENT ON COLUMN public.flow_task.objecttype IS '对象类型；流程所属业务环节；码值：OBJECTTYPE';
COMMENT ON COLUMN public.flow_task.phasename IS '阶段名称；流程阶段名称';
COMMENT ON COLUMN public.flow_task.userid IS '当前处理人；该节点的处理人工号';
COMMENT ON COLUMN public.flow_task.begintime IS '开始时间；该节点开始时间';
COMMENT ON COLUMN public.flow_task.phaseaction IS '阶段动作；该节点执行的动作用';

-- public.guaranty_contract 担保合同信息
CREATE TABLE public.guaranty_contract (
  serialno                 varchar(64)      NOT NULL,
  guarantorid              varchar(64)     ,
  guarantorname            varchar(64)     ,
  contractstatus           varchar(64)     ,
  guarantysum              numeric(20,2)   ,
  CONSTRAINT pk_guaranty_contract PRIMARY KEY (serialno)
);
COMMENT ON TABLE public.guaranty_contract IS '担保合同信息；粒度：一行一份担保合同；坑点：一个担保合同可对应多个担保物，直接 JOIN 放大担保金额';
COMMENT ON COLUMN public.guaranty_contract.serialno IS '担保合同编号；担保合同主键；FK→public.guaranty_info.guarantyid';
COMMENT ON COLUMN public.guaranty_contract.guarantorid IS '担保人ID；担保人（保证人）客户编号';
COMMENT ON COLUMN public.guaranty_contract.guarantorname IS '担保人名称';
COMMENT ON COLUMN public.guaranty_contract.contractstatus IS '合同状态；担保合同状态；码值：CONTRACTSTATUS';
COMMENT ON COLUMN public.guaranty_contract.guarantysum IS '担保总金额；担保合同担保总金额';

-- public.guaranty_info 担保物信息表
CREATE TABLE public.guaranty_info (
  guarantyid               varchar(64)      NOT NULL,
  guarantystatus           varchar(64)     ,
  evalnetvalue             numeric(20,2)   ,
  guarantysum              numeric(20,2)   ,
  CONSTRAINT pk_guaranty_info PRIMARY KEY (guarantyid)
);
COMMENT ON TABLE public.guaranty_info IS '担保物信息表；粒度：一行一个抵质押物';
COMMENT ON COLUMN public.guaranty_info.guarantyid IS '质物编号；担保物主键';
COMMENT ON COLUMN public.guaranty_info.guarantystatus IS '状态；押品当前状态；码值：GuarantyStatus';
COMMENT ON COLUMN public.guaranty_info.evalnetvalue IS '评估价值；押品评估净值';
COMMENT ON COLUMN public.guaranty_info.guarantysum IS '本次担保金额；该押品本次担保的金额';

-- public.guaranty_relative 担保关联表
CREATE TABLE public.guaranty_relative (
  objecttype               varchar(64)     ,
  objectno                 varchar(64)     ,
  guarantyid               varchar(64)     ,
  contractno               varchar(64)     
);
COMMENT ON TABLE public.guaranty_relative IS '担保关联表；粒度：一行一条担保关联关系；坑点：★ 必须限定 OBJECTTYPE，否则混入其他对象的担保关系';
COMMENT ON COLUMN public.guaranty_relative.objecttype IS '关联对象类型；被担保对象类型；码值：OBJECTTYPE';
COMMENT ON COLUMN public.guaranty_relative.objectno IS '关联对象编号；被担保对象的编号（合同号/申请号/批复号）';
COMMENT ON COLUMN public.guaranty_relative.guarantyid IS '质物编号；担保物编号';
COMMENT ON COLUMN public.guaranty_relative.contractno IS '担保合同流水号；担保合同编号';

-- public.odm_cms_cms_collateraltype_info odm_cms_cms_collateraltype_info
CREATE TABLE public.odm_cms_cms_collateraltype_info (
  data_dt                  date            ,
  distribution_key         text            ,
  coltypeid                text            ,
  guarantytype             text            ,
  guarantytypename         text            ,
  fathertypecd             text            ,
  isleafnodes              text            ,
  status                   text            ,
  invaliddate              date            ,
  validdate                date            ,
  inputdate                date            ,
  updatedate               date            ,
  remark                   text            ,
  inputuserid              text            ,
  inputorgid               text            ,
  updateuserid             text            ,
  updateorgid              text            ,
  attribute1               text            
);
COMMENT ON TABLE public.odm_cms_cms_collateraltype_info IS 'odm_cms_cms_collateraltype_info';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.data_dt IS '数据日期';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.distribution_key IS '分桶键';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.coltypeid IS '流水号';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.guarantytype IS '押品类型编号';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.guarantytypename IS '押品类型名称';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.fathertypecd IS '父节点类型编号';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.isleafnodes IS '是否叶子节点';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.status IS '状态';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.invaliddate IS '生效日期';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.validdate IS '失效日期';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.inputdate IS '登记日期';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.updatedate IS '修改日期';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.remark IS '备注';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.inputuserid IS '登记人';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.inputorgid IS '登记机构';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.updateuserid IS '修改人';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.updateorgid IS '修改机构';
COMMENT ON COLUMN public.odm_cms_cms_collateraltype_info.attribute1 IS '属性1';

-- public.odm_cms_guaranty_contract odm_cms_guaranty_contract
CREATE TABLE public.odm_cms_guaranty_contract (
  data_dt                  date            ,
  distribution_key         text            ,
  serialno                 text            ,
  contracttype             text            ,
  guarantytype             text            ,
  contractstatus           text            ,
  contractno               text            ,
  signdate                 date            ,
  begindate                date            ,
  enddate                  date            ,
  customerid               text            ,
  guarantorid              text            ,
  guarantorname            text            ,
  creditorgid              text            ,
  creditorgname            text            ,
  guarantycurrency         text            ,
  guarantyvalue            numeric(24,6)   ,
  guarantyinfo             text            ,
  otherdescribe            text            ,
  checkguaranty            text            ,
  reception                text            ,
  receptionduty            text            ,
  guaranryopinion          text            ,
  checkguarantyman1        text            ,
  checkguarantyman2        text            ,
  inputorgid               text            ,
  inputuserid              text            ,
  inputdate                date            ,
  updateuserid             text            ,
  updatedate               date            ,
  remark                   text            ,
  certtype                 text            ,
  certid                   text            ,
  othername                text            ,
  loancardno               text            ,
  guaranteeform            text            ,
  commondate               date            ,
  bailratio                numeric(24,6)   ,
  systrelation             text            ,
  guaborelation            text            ,
  gudanlication            text            ,
  guarability              text            ,
  guarandesire             text            ,
  guarantysum              numeric(24,6)   ,
  maxrenewlimit            numeric(24,6)   ,
  guaranteeasset           numeric(24,6)   ,
  closingdate              date            ,
  ownguarantysum           numeric(24,6)   ,
  otherguarantysum         numeric(24,6)   ,
  gjserialno               text            ,
  dp_load_dt               date            
);
COMMENT ON TABLE public.odm_cms_guaranty_contract IS 'odm_cms_guaranty_contract';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.data_dt IS '数据日期';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.distribution_key IS '分桶键';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.serialno IS '担保信息编号';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.contracttype IS '担保类型';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantytype IS '担保方式';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.contractstatus IS '合同状态';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.contractno IS '担保合同号';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.signdate IS '合同签订日';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.begindate IS '起始日期';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.enddate IS '到期日期';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.customerid IS '客户ID';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantorid IS '担保人ID';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantorname IS '担保人名称';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.creditorgid IS '债权人机构代码';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.creditorgname IS '债权人机构名称';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantycurrency IS '币种';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantyvalue IS '担保总金额';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantyinfo IS '担保物概况';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.otherdescribe IS '其它特别约定';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.checkguaranty IS '核保时间';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.reception IS '接待人姓名';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.receptionduty IS '接待人职务';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guaranryopinion IS '担保意愿';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.checkguarantyman1 IS '核保人';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.checkguarantyman2 IS '核保人(二)';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.inputorgid IS '登记机构ID';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.inputuserid IS '登记人ID';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.inputdate IS '登记时间';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.updateuserid IS '更新用户编号';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.updatedate IS '更新时间';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.remark IS '备注';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.certtype IS '保证人证件类型';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.certid IS '保证人证件号码';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.othername IS '其他名称';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.loancardno IS '保证人贷款卡编号';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guaranteeform IS '保证担保形式';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.commondate IS '通用日期';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.bailratio IS '保证金比例';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.systrelation IS '保证人与借款人系统检测关系';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guaborelation IS '保证人与借款人关系';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.gudanlication IS '保证资格';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarability IS '保证能力';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarandesire IS '保证意愿';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guarantysum IS '担保总金额';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.maxrenewlimit IS '担保总金额';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.guaranteeasset IS '保证人净资产';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.closingdate IS '统计截止日期';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.ownguarantysum IS '本行已对外担保总额';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.otherguarantysum IS '他行已对外担保总额';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.gjserialno IS '国结流水号';
COMMENT ON COLUMN public.odm_cms_guaranty_contract.dp_load_dt IS '范围分区字段';

-- public.odm_cms_guaranty_info odm_cms_guaranty_info
CREATE TABLE public.odm_cms_guaranty_info (
  data_dt                  date            ,
  distribution_key         text            ,
  guarantyid               text            ,
  guarantytype             text            ,
  guarantystatus           text            ,
  ownerid                  text            ,
  ownername                text            ,
  ownertype                text            ,
  rate                     numeric(24,6)   ,
  custguarantytype         text            ,
  subjectno                text            ,
  relativeaccount          text            ,
  guarantyrightid          text            ,
  otherguarantyright       text            ,
  guarantyname             text            ,
  guarantysubtype          text            ,
  guarantyownway           text            ,
  guarantyusing            text            ,
  guarantylocation         text            ,
  guarantyamount           numeric(24,6)   ,
  guarantyamount1          numeric(24,6)   ,
  guarantyamount2          numeric(24,6)   ,
  guarantyresouce          text            ,
  guarantydate             date            ,
  begindate                date            ,
  ownertime                date            ,
  guarantydescript         text            ,
  aboutotherid1            text            ,
  aboutotherid2            text            ,
  aboutotherid3            text            ,
  aboutotherid4            text            ,
  purpose                  text            ,
  aboutsum1                numeric(24,6)   ,
  aboutsum2                numeric(24,6)   ,
  aboutrate                numeric(24,6)   ,
  guarantyana              text            ,
  guarantyprice            numeric(24,6)   ,
  evalmethod               text            ,
  evalorgid                text            ,
  evalorgname              text            ,
  evaldate                 date            ,
  evalnetvalue             numeric(24,6)   ,
  confirmvalue             numeric(24,6)   ,
  guarantyrate             numeric(24,6)   ,
  thirdparty1              text            ,
  thirdparty2              text            ,
  thirdparty3              text            ,
  guarantydescribe1        text            ,
  guarantydescribe2        text            ,
  guarantydescribe3        text            ,
  flag1                    text            ,
  flag2                    text            ,
  flag3                    text            ,
  flag4                    text            ,
  guarantyregno            text            ,
  guarantyregorg           text            ,
  guarantyregdate          date            ,
  guarantywodate           date            ,
  insurecertno             text            ,
  otherassumpsit           text            ,
  inputorgid               text            ,
  inputuserid              text            ,
  inputdate                date            ,
  updateuserid             text            ,
  updatedate               date            ,
  remark                   text            ,
  sapvouchtype             text            ,
  certtype                 text            ,
  certid                   text            ,
  loancardno               text            ,
  guarantycurrency         text            ,
  evalcurrency             text            ,
  guarantyjointlyid        text            ,
  guarantyjointlyname      text            ,
  ownerplustime            date            ,
  leasestatus              text            ,
  pledgestatus             text            ,
  pledgeotherid            text            ,
  guarantybuydate          date            ,
  aboutyenootherid1        text            ,
  insurenddate             date            ,
  insuremoney              numeric(24,6)   ,
  insurestartdate          date            ,
  insurepeople             text            ,
  insurefavoree            text            ,
  theinsureuser            text            ,
  unitpricevalue           numeric(24,6)   ,
  confirmtime              date            ,
  marketunitprice          numeric(24,6)   ,
  maxguaprobabity          numeric(24,6)   ,
  maxguarantmoney          numeric(24,6)   ,
  ortherguarantmoney       numeric(24,6)   ,
  guarantyvalue            numeric(24,6)   ,
  guarantyfreezevalue      numeric(24,6)   ,
  guarantypresentvalue     numeric(24,6)   ,
  insurerstatus            text            ,
  enddate                  date            ,
  amount7                  numeric(24,6)   ,
  reevaldate               date            ,
  reevalvalue              numeric(24,6)   ,
  guarantydescribe5        text            ,
  guarantydescribe6        text            ,
  freezedate               date            ,
  guarantyamount3          numeric(24,6)   ,
  guarantyamount4          numeric(24,6)   ,
  guarantyamount5          numeric(24,6)   ,
  guarantyamount6          numeric(24,6)   ,
  updateorgid              text            ,
  attachmenttype           text            ,
  grouconfirmvalue         numeric(24,6)   ,
  attaconfirmvalue         numeric(24,6)   ,
  firstselldate            date            ,
  interestbalance          numeric(24,6)   ,
  balance                  numeric(24,6)   ,
  insuretype               text            ,
  guarantydescribe4        text            ,
  guarantyamount7          numeric(24,6)   ,
  sendflag                 text            ,
  billtype                 text            ,
  passflag                 text            ,
  staplecapitalgoodflag    text            ,
  capitalgoodindustrytype  text            ,
  bondcreditrating         text            ,
  artificialno             text            ,
  assetstatus              text            ,
  guarantysum              numeric(24,6)   ,
  storerequire             text            ,
  accountorgid             text            ,
  isfirstline              text            ,
  priorityamt              numeric(24,6)   ,
  evaluatecycle            text            ,
  gjserialno               text            ,
  dp_load_dt               date            ,
  ismainguarantytype       text            ,
  isaddguarantytype        text            ,
  guarantyevalmethod       text            
);
COMMENT ON TABLE public.odm_cms_guaranty_info IS 'odm_cms_guaranty_info';
COMMENT ON COLUMN public.odm_cms_guaranty_info.data_dt IS '数据日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.distribution_key IS '分桶键';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyid IS '质物编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantytype IS '质物类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantystatus IS '担保物状态';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ownerid IS '出质人代码';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ownername IS '出质人名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ownertype IS '出质人类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.rate IS '担保利率';
COMMENT ON COLUMN public.odm_cms_guaranty_info.custguarantytype IS '客户担保物类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.subjectno IS '科目编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.relativeaccount IS '权利人账户账号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyrightid IS '票据号码';
COMMENT ON COLUMN public.odm_cms_guaranty_info.otherguarantyright IS '他项权证号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyname IS '承兑行名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantysubtype IS '土地级别';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyownway IS '存单支取方式';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyusing IS '土地现状';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantylocation IS '债券代理发行机构';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount IS '债券数量';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount1 IS '申请前一日基金收盘价';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount2 IS '质押基金份额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyresouce IS '债券发行单位';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydate IS '签发日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.begindate IS '开始日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ownertime IS '提单到期日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescript IS '抵押物说明';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutotherid1 IS '车辆型号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutotherid2 IS '供应商';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutotherid3 IS '车架号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutotherid4 IS '担保物其他相关号码4';
COMMENT ON COLUMN public.odm_cms_guaranty_info.purpose IS '地上附着物名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutsum1 IS '票面金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutsum2 IS '担保本金债权金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutrate IS '质押基金份额占总额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyana IS '存放地点';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyprice IS '车辆购入价值';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evalmethod IS '质物价值评估方式';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evalorgid IS '评估机构组织机构代码';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evalorgname IS '价值评估机构名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evaldate IS '价值评估时间';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evalnetvalue IS '质物金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.confirmvalue IS '质押金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyrate IS '抵质押率';
COMMENT ON COLUMN public.odm_cms_guaranty_info.thirdparty1 IS '车辆类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.thirdparty2 IS '币种';
COMMENT ON COLUMN public.odm_cms_guaranty_info.thirdparty3 IS '车辆品牌';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe1 IS '开证行国家地区';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe2 IS '保兑行名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe3 IS '保兑行所在国家和地区';
COMMENT ON COLUMN public.odm_cms_guaranty_info.flag1 IS '是否记名债券';
COMMENT ON COLUMN public.odm_cms_guaranty_info.flag2 IS '是否需要办理止付';
COMMENT ON COLUMN public.odm_cms_guaranty_info.flag3 IS '是否有当年车船使用税证';
COMMENT ON COLUMN public.odm_cms_guaranty_info.flag4 IS '年检是否正常';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyregno IS '抵押登记编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyregorg IS '抵押登记机关';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyregdate IS '抵押登记日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantywodate IS '抵押登记注销日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurecertno IS '抵押物保险单编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.otherassumpsit IS '其它特别约定';
COMMENT ON COLUMN public.odm_cms_guaranty_info.inputorgid IS '登记机构';
COMMENT ON COLUMN public.odm_cms_guaranty_info.inputuserid IS '登记人';
COMMENT ON COLUMN public.odm_cms_guaranty_info.inputdate IS '登记日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.updateuserid IS '更新用户编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.updatedate IS '更新日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.remark IS '备注';
COMMENT ON COLUMN public.odm_cms_guaranty_info.sapvouchtype IS 'SAP抵质押物类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.certtype IS '出质人证件类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.certid IS '出质人证件号码';
COMMENT ON COLUMN public.odm_cms_guaranty_info.loancardno IS '出质人贷款卡号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantycurrency IS '质押币种';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evalcurrency IS '质物币种';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyjointlyid IS '抵押物共有权人ID';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyjointlyname IS '抵押物共有权人名称';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ownerplustime IS '房屋剩余使用年限';
COMMENT ON COLUMN public.odm_cms_guaranty_info.leasestatus IS '出质人贷款卡号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.pledgestatus IS '房屋抵押状况';
COMMENT ON COLUMN public.odm_cms_guaranty_info.pledgeotherid IS '已抵押他项编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantybuydate IS '房屋购置年份';
COMMENT ON COLUMN public.odm_cms_guaranty_info.aboutyenootherid1 IS '有无土地使用权证书';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurenddate IS '保单到期日';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insuremoney IS '投保金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurestartdate IS '保单起始日';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurepeople IS '保险人';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurefavoree IS '保单受益人';
COMMENT ON COLUMN public.odm_cms_guaranty_info.theinsureuser IS '被保险人';
COMMENT ON COLUMN public.odm_cms_guaranty_info.unitpricevalue IS '抵押物认定单价';
COMMENT ON COLUMN public.odm_cms_guaranty_info.confirmtime IS '价值认定有效期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.marketunitprice IS '市场单价';
COMMENT ON COLUMN public.odm_cms_guaranty_info.maxguaprobabity IS '最高抵押率';
COMMENT ON COLUMN public.odm_cms_guaranty_info.maxguarantmoney IS '最高可抵押金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ortherguarantmoney IS '他行已抵押总金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyvalue IS '担保总价值';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyfreezevalue IS '我行在途冻结抵押金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantypresentvalue IS '本次抵押金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insurerstatus IS '是否投保';
COMMENT ON COLUMN public.odm_cms_guaranty_info.enddate IS '到期日期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.amount7 IS '账面净值';
COMMENT ON COLUMN public.odm_cms_guaranty_info.reevaldate IS '抵押物重估时间';
COMMENT ON COLUMN public.odm_cms_guaranty_info.reevalvalue IS '抵押物重估金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe5 IS '船籍港';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe6 IS '航线';
COMMENT ON COLUMN public.odm_cms_guaranty_info.freezedate IS '冻结时间';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount3 IS '每股补仓线比率';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount4 IS '每股补仓线';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount5 IS '每股平仓线比率';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount6 IS '每股平仓线';
COMMENT ON COLUMN public.odm_cms_guaranty_info.updateorgid IS '更新机构ID';
COMMENT ON COLUMN public.odm_cms_guaranty_info.attachmenttype IS '地上附着物类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.grouconfirmvalue IS '土地认定价值';
COMMENT ON COLUMN public.odm_cms_guaranty_info.attaconfirmvalue IS '地上附着物认定价值';
COMMENT ON COLUMN public.odm_cms_guaranty_info.firstselldate IS '国家首次出让该地块时间';
COMMENT ON COLUMN public.odm_cms_guaranty_info.interestbalance IS '业务欠息总额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.balance IS '业务合同余额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.insuretype IS '投保险种';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantydescribe4 IS '已投入工程款';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyamount7 IS '股票质押数量';
COMMENT ON COLUMN public.odm_cms_guaranty_info.sendflag IS '发送标志';
COMMENT ON COLUMN public.odm_cms_guaranty_info.billtype IS '票据类型';
COMMENT ON COLUMN public.odm_cms_guaranty_info.passflag IS '人行是否通过标志（电票系统返回）';
COMMENT ON COLUMN public.odm_cms_guaranty_info.staplecapitalgoodflag IS '动产质押大宗生产资料标志';
COMMENT ON COLUMN public.odm_cms_guaranty_info.capitalgoodindustrytype IS '大宗生产资料所属行业';
COMMENT ON COLUMN public.odm_cms_guaranty_info.bondcreditrating IS '公司债券信用等级';
COMMENT ON COLUMN public.odm_cms_guaranty_info.artificialno IS '文本合同编号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.assetstatus IS '押品状态（核算）';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantysum IS '抵押物本次担保金额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.storerequire IS '仓储物损耗标准';
COMMENT ON COLUMN public.odm_cms_guaranty_info.accountorgid IS '记账机构';
COMMENT ON COLUMN public.odm_cms_guaranty_info.isfirstline IS '是否第一顺位';
COMMENT ON COLUMN public.odm_cms_guaranty_info.priorityamt IS '优先受偿权数额';
COMMENT ON COLUMN public.odm_cms_guaranty_info.evaluatecycle IS '估值周期';
COMMENT ON COLUMN public.odm_cms_guaranty_info.gjserialno IS '国结流水号';
COMMENT ON COLUMN public.odm_cms_guaranty_info.dp_load_dt IS '范围分区字段';
COMMENT ON COLUMN public.odm_cms_guaranty_info.ismainguarantytype IS '是否为主担保方式';
COMMENT ON COLUMN public.odm_cms_guaranty_info.isaddguarantytype IS '是否为追加/补充担保';
COMMENT ON COLUMN public.odm_cms_guaranty_info.guarantyevalmethod IS '确定担保物价值的评估方法';

-- public.odm_cms_guaranty_relative odm_cms_guaranty_relative
CREATE TABLE public.odm_cms_guaranty_relative (
  data_dt                  date            ,
  distribution_key         text            ,
  objecttype               text            ,
  objectno                 text            ,
  contractno               text            ,
  guarantyid               text            ,
  channel                  text            ,
  status                   text            ,
  othersrightid            text            ,
  guarantysum              numeric(24,6)   ,
  payorder                 text            ,
  type                     text            ,
  relationstatus           text            ,
  describea                text            ,
  loadstatus               text            ,
  maxguaprobabity          numeric(24,6)   ,
  maxguarantmoney          numeric(24,6)   ,
  dp_load_dt               date            
);
COMMENT ON TABLE public.odm_cms_guaranty_relative IS 'odm_cms_guaranty_relative';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.data_dt IS '数据日期';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.distribution_key IS '分桶键';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.objecttype IS '担保关联对象类型';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.objectno IS '担保关联对象编号';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.contractno IS '担保合同流水号字段';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.guarantyid IS '抵质押物编号';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.channel IS '关联关系来源渠道';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.status IS '有效标志';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.othersrightid IS '他项权证号';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.guarantysum IS '担保债权金额';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.payorder IS '受偿次序';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.type IS '数据来源类型';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.relationstatus IS '关联有效标志';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.describea IS '描述';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.loadstatus IS '押品出入库状态';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.maxguaprobabity IS '最高抵押率';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.maxguarantmoney IS '最高可抵押金额';
COMMENT ON COLUMN public.odm_cms_guaranty_relative.dp_load_dt IS '范围分区字段';

-- public.org_info 机构表
CREATE TABLE public.org_info (
  orgid                    varchar(64)      NOT NULL,
  orgname                  varchar(64)     ,
  mainframeorgid           varchar(64)     ,
  orglevel                 varchar(200)    ,
  belongarea               varchar(200)    ,
  CONSTRAINT pk_org_info PRIMARY KEY (orgid)
);
COMMENT ON TABLE public.org_info IS '机构表；粒度：一行一个机构';
COMMENT ON COLUMN public.org_info.orgid IS '机构号；机构主键';
COMMENT ON COLUMN public.org_info.orgname IS '机构名称';
COMMENT ON COLUMN public.org_info.mainframeorgid IS '核心机构号；核心系统机构号';
COMMENT ON COLUMN public.org_info.orglevel IS '机构等级；机构层级';
COMMENT ON COLUMN public.org_info.belongarea IS '所属地区；机构所属地区码值；码值：BelongArea';

-- public.rpt_mrtgplg_dtl rpt_mrtgplg_dtl
CREATE TABLE public.rpt_mrtgplg_dtl (
  datadt                   date            ,
  distributionkey          text            ,
  orgno                    text            ,
  uporgno                  text            ,
  uporgnm                  text            ,
  dblno                    text            ,
  cntrno                   text            ,
  cur                      text            ,
  curnm                    text            ,
  custno                   text            ,
  custnm                   text            ,
  custtyp                  text            ,
  entpscl                  text            ,
  indscgylv1               text            ,
  indscgylv2               text            ,
  indscgylv3               text            ,
  indscgylv4               text            ,
  rgstaddr                 text            ,
  prdno                    text            ,
  prdnm                    text            ,
  loantyp                  text            ,
  loandect                 text            ,
  loanuse                  text            ,
  loanterm                 text            ,
  fivelvlcgycd             text            ,
  grtmode                  text            ,
  grtytyp                  text            ,
  grtyno                   text            ,
  grtynm                   text            ,
  grtyestn                 text            ,
  hstyp                    text            ,
  grtysts                  text            ,
  dblamt                   numeric(38,6)   ,
  dblbal                   numeric(38,6)   ,
  loanrate                 numeric(10,6)   ,
  valtdt                   date            ,
  expdt                    date            ,
  loansts                  text            ,
  dblamtcnvtcny            numeric(38,6)   ,
  dp_load_dt               date            
);
COMMENT ON TABLE public.rpt_mrtgplg_dtl IS 'rpt_mrtgplg_dtl';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.datadt IS '数据日期';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.distributionkey IS '分桶键';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.orgno IS '机构编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.uporgno IS '上级机构编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.uporgnm IS '上级机构名称';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.dblno IS '借据编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.cntrno IS '合同编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.cur IS '币种';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.curnm IS '币种名称';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.custno IS '客户编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.custnm IS '客户名称';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.custtyp IS '客户类型';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.entpscl IS '企业规模';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.indscgylv1 IS '行业分类一级';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.indscgylv2 IS '行业分类二级';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.indscgylv3 IS '行业分类三级';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.indscgylv4 IS '行业分类四级';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.rgstaddr IS '注册地址';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.prdno IS '产品编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.prdnm IS '产品名称';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loantyp IS '贷款类型';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loandect IS '贷款投向';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loanuse IS '贷款用途';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loanterm IS '贷款期限';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.fivelvlcgycd IS '五级分类代码';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtmode IS '担保方式';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtytyp IS '担保物类型';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtyno IS '担保物编号';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtynm IS '担保物名称';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtyestn IS '担保物估值';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.hstyp IS '房产类型';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.grtysts IS '担保物状态';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.dblamt IS '借据金额';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.dblbal IS '借据余额';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loanrate IS '贷款利率';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.valtdt IS '起息日期';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.expdt IS '到期日期';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.loansts IS '贷款状态';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.dblamtcnvtcny IS '借据金额折人民币';
COMMENT ON COLUMN public.rpt_mrtgplg_dtl.dp_load_dt IS '范围分区字段';

-- public.samecustomer_relative 同业客户关联信息
CREATE TABLE public.samecustomer_relative (
  certenddate              date            ,
  rltvtypeid               varchar(32)     
);
COMMENT ON TABLE public.samecustomer_relative IS '同业客户关联信息；坑点：贴源下发';
COMMENT ON COLUMN public.samecustomer_relative.certenddate IS '证件到期日';
COMMENT ON COLUMN public.samecustomer_relative.rltvtypeid IS '客户关联关系';

-- public.user_info 用户信息表
CREATE TABLE public.user_info (
  userid                   varchar(64)      NOT NULL,
  username                 varchar(64)     ,
  CONSTRAINT pk_user_info PRIMARY KEY (userid)
);
COMMENT ON TABLE public.user_info IS '用户信息表；粒度：一行一个用户（客户经理）';
COMMENT ON COLUMN public.user_info.userid IS '用户号；用户（客户经理）主键';
COMMENT ON COLUMN public.user_info.username IS '用户名称；用户姓名';


-- ============================================================================
-- 源系统关系，但对方表不在本批建的 24 张表内（不做约束，仅备查）
-- ============================================================================
--   dwd.dwd_lan_dbl_fcti.dblno → dwd.dwd_lan_dbl_sbj.duebillserialno（源系统关系，本库未建该表）
--   dwd.dwd_lan_dbl_fcti.prdno → dwd.dwd_lan_prd_dimi.prdno（源系统关系，本库未建该表）
--   public.acct_loan.putoutserialno → public.acct_putout.serialno（源系统关系，本库未建该表）
--   public.business_duebill.serialno → public.business_wastebook.relativeserialno（源系统关系，本库未建该表）
--   public.customer_info.belonggroupid → public.group_info.groupid（源系统关系，本库未建该表）
--   public.customer_info.customerid → public.house_info.customerid（源系统关系，本库未建该表）
--   public.guaranty_info.guarantyid → public.guaranty_audit.guarantyid（源系统关系，本库未建该表）
--   public.user_info.userid → public.customer_belong.userid（源系统关系，本库未建该表）

-- ============================================================================
-- 推断类型清单（205 条），需业务复核
-- ============================================================================
--   dwd_lan_dbl_fcti.dp_load_dt(空→date)
--   dwd_lan_dbl_fcti.syscgy(空→varchar(64))
--   dwd_lan_dbl_fcti.dblno(空→varchar(64))
--   dwd_lan_dbl_fcti.dblamt(空→numeric(20,2))
--   dwd_lan_dbl_fcti.dblbal(空→numeric(20,2))
--   dwd_lan_dbl_fcti.prdno(空→varchar(64))
--   dwd_lan_dbl_fcti.loansts(空→varchar(64))
--   dwd_lan_dbl_fcti.fivelvlcgycd_jg(空→varchar(200))
--   dwd_lan_dbl_fcti.custnm(空→varchar(64))
--   dwd_lan_dbl_fcti.certno(空→varchar(64))
--   dwd_lan_dbl_fcti.bnkincustno(空→varchar(64))
--   dwd_lan_dbl_fcti.wrtoffpnp(空→numeric(20,2))
--   dwd_lan_dbl_fcti.wrtoffdt(空→date)
--   dwd_lan_dbl_fcti.oper(空→varchar(200))
--   dwd_lan_dbl_fcti.cntrno(空→varchar(64))
--   rpt_loan_dtl.dblno(空→varchar(64))
--   rpt_loan_dtl.cntrno(空→varchar(64))
--   rpt_loan_dtl.cntrseqno(空→varchar(64))
--   rpt_loan_dtl.custno(空→varchar(64))
--   rpt_loan_dtl.bnkincustno(空→varchar(64))
--   rpt_loan_dtl.custnm(空→varchar(64))
--   rpt_loan_dtl.certtyp(空→varchar(64))
--   rpt_loan_dtl.certno(空→varchar(64))
--   rpt_loan_dtl.custtypbcgy(空→varchar(64))
--   rpt_loan_dtl.custtypcgy(空→varchar(64))
--   rpt_loan_dtl.prdno(空→varchar(64))
--   rpt_loan_dtl.prdnm(空→varchar(64))
--   rpt_loan_dtl.cur(空→varchar(200))
--   rpt_loan_dtl.acgorgno(空→varchar(64))
--   rpt_loan_dtl.acgorgnm(空→varchar(64))
--   rpt_loan_dtl.blgbrchno(空→varchar(64))
--   rpt_loan_dtl.blgbrchnm(空→varchar(64))
--   rpt_loan_dtl.mngorgno(空→varchar(64))
--   rpt_loan_dtl.mngorgnm(空→varchar(64))
--   rpt_loan_dtl.orgjuriscd(空→varchar(64))
--   rpt_loan_dtl.orgjurisnm(空→varchar(64))
--   rpt_loan_dtl.greencrdtidx(空→varchar(200))
--   rpt_loan_dtl.greenproportion(空→numeric(20,2))
--   rpt_loan_dtl.setlacctno(空→varchar(64))
--   rpt_loan_dtl.loanacctno(空→varchar(64))
--   rpt_loan_dtl.loancgylv1(空→varchar(200))
--   rpt_loan_dtl.loancgylv2(空→varchar(200))
--   rpt_loan_dtl.loancgylv3(空→varchar(200))
--   rpt_loan_dtl.loancgylv4(空→varchar(200))
--   rpt_loan_dtl.hsmrtgflg(空→varchar(64))
--   rpt_loan_dtl.hpntyp(空→varchar(64))
--   rpt_loan_dtl.loandect(空→varchar(200))
--   rpt_loan_dtl.loandectlv1(空→varchar(200))
--   rpt_loan_dtl.loandectlv2(空→varchar(200))
--   rpt_loan_dtl.loandectlv3(空→varchar(200))
--   rpt_loan_dtl.loandectlv4(空→varchar(200))
--   rpt_loan_dtl.loantyp(空→varchar(64))
--   rpt_loan_dtl.loanuse(空→varchar(200))
--   rpt_loan_dtl.loanterm(空→varchar(200))
--   rpt_loan_dtl.loantermcgy(空→varchar(64))
--   rpt_loan_dtl.grtmode(空→varchar(200))
--   rpt_loan_dtl.grtform(空→varchar(200))
--   rpt_loan_dtl.gnrnmgrp(空→varchar(200))
--   rpt_loan_dtl.grtynmgrp(空→varchar(200))
--   rpt_loan_dtl.addgrtmode(空→varchar(200))
--   rpt_loan_dtl.rpymodecd(空→varchar(64))
--   rpt_loan_dtl.rpymode(空→varchar(200))
--   rpt_loan_dtl.fivelvlcgy(空→varchar(64))
--   rpt_loan_dtl.fivelvlcgycd_jg(空→varchar(200))
--   rpt_loan_dtl.yrbgnfivelvlcgy(空→varchar(64))
--   rpt_loan_dtl.mobgnfivelvlcgy(空→varchar(64))
--   rpt_loan_dtl.operorgnm(空→varchar(64))
--   rpt_loan_dtl.bsnsts(空→varchar(64))
--   rpt_loan_dtl.dblsts(空→varchar(64))
--   rpt_loan_dtl.valtdt(空→date)
--   rpt_loan_dtl.expdt(空→date)
--   rpt_loan_dtl.actexpdt(空→date)
--   rpt_loan_dtl.cnlacctdt(空→date)
--   rpt_loan_dtl.loanwrtoffmemocd(空→varchar(64))
--   rpt_loan_dtl.loanrate(空→numeric(20,6))
--   rpt_loan_dtl.cntramt(空→numeric(20,2))
--   rpt_loan_dtl.dblamt(空→numeric(20,2))
--   rpt_loan_dtl.dblbal(空→numeric(20,2))
--   rpt_loan_dtl.dblbalcnvtcny(空→numeric(20,2))
--   rpt_loan_dtl.oduebal(空→numeric(20,2))
--   rpt_loan_dtl.oduedys(空→varchar(200))
--   rpt_loan_dtl.pnpoduedys(空→varchar(200))
--   rpt_loan_dtl.intoduedys(空→varchar(200))
--   rpt_loan_dtl.maxoduedys(空→varchar(200))
--   rpt_loan_dtl.onshtdbtintbal(空→numeric(20,2))
--   rpt_loan_dtl.offshtdbtintbal(空→numeric(20,2))
--   rpt_loan_dtl.pnpoduepnyint(空→varchar(200))
--   rpt_loan_dtl.intoduepnyint(空→varchar(200))
--   rpt_loan_dtl.entrbaddt(空→date)
--   rpt_loan_dtl.twllvlcgycd(空→varchar(64))
--   rpt_loan_dtl.datadt(date→date)
--   acct_loan.putoutdate(VARCHAR2(10)→date)
--   acct_loan.maturitydate(VARCHAR2(10)→date)
--   acct_loan.originalmaturitydate(VARCHAR2(10)→date)
--   acct_loan.finishdate(VARCHAR2(10)→date)
--   acct_loan.businessdate(VARCHAR2(10)→date)
--   acct_payment_log.relativeobjectno(空→varchar(64))
--   acct_payment_log.actualpaydate(空→date)
--   acct_payment_log.actualpayprincipalamt(空→numeric(20,2))
--   acct_payment_log.actualpayinterestamt(空→numeric(20,2))
--   acct_payment_schedule.paydate(VARCHAR2(10)→date)
--   acct_payment_schedule.payprincipalamt(空→numeric(20,2))
--   acct_payment_schedule.actualpayprincipalamt(空→numeric(20,2))
--   business_contract.serialno(空→varchar(64))
--   business_contract.artificialno(空→varchar(64))
--   business_contract.customerid(空→varchar(64))
--   business_contract.businesstype(空→varchar(64))
--   business_contract.businesssum(空→numeric(20,2))
--   business_contract.vouchtype(空→varchar(64))
--   business_contract.status(空→varchar(64))
--   business_contract.classifyresult(空→varchar(200))
--   business_contract.manageuserid(空→varchar(64))
--   business_contract.manageorgid(空→varchar(64))
--   business_contract.accountorgid(空→varchar(64))
--   business_duebill.serialno(空→varchar(64))
--   business_duebill.relativeserialno2(空→varchar(200))
--   business_duebill.mfcustomerid(空→varchar(64))
--   business_duebill.customerid(空→varchar(64))
--   business_duebill.businesstype(空→varchar(64))
--   business_duebill.businessstatus(空→varchar(64))
--   business_duebill.businesscurrency(空→varchar(200))
--   business_duebill.businesssum(空→numeric(20,2))
--   business_duebill.balance(空→numeric(20,2))
--   business_duebill.normalbalance(空→numeric(20,2))
--   business_duebill.overduebalance(空→numeric(20,2))
--   business_duebill.putoutdate(空→date)
--   business_duebill.maturity(空→date)
--   business_duebill.actualmaturity(空→date)
--   business_duebill.finishdate(空→date)
--   business_type.typeno(空→varchar(64))
--   business_type.typename(空→varchar(64))
--   business_type.isinuse(空→varchar(200))
--   classify_record.objectno(空→varchar(64))
--   classify_record.classifyresult(空→varchar(200))
--   code_library.codeno(空→varchar(64))
--   code_library.itemno(空→varchar(64))
--   code_library.itemname(空→varchar(64))
--   customer_fsrecord.reportdate(Y→date)
--   customer_info.inputdate(VARCHAR2(10)→date)
--   flow_task.objectno(空→varchar(64))
--   flow_task.objecttype(空→varchar(64))
--   flow_task.phasename(空→varchar(64))
--   flow_task.userid(空→varchar(64))
--   flow_task.begintime(空→date)
--   flow_task.phaseaction(空→varchar(200))
--   guaranty_contract.serialno(空→varchar(64))
--   guaranty_contract.guarantorid(空→varchar(64))
--   guaranty_contract.guarantorname(空→varchar(64))
--   guaranty_contract.contractstatus(空→varchar(64))
--   guaranty_contract.guarantysum(空→numeric(20,2))
--   guaranty_info.guarantyid(空→varchar(64))
--   guaranty_info.guarantystatus(空→varchar(64))
--   guaranty_info.evalnetvalue(空→numeric(20,2))
--   guaranty_info.guarantysum(空→numeric(20,2))
--   guaranty_relative.objecttype(空→varchar(64))
--   guaranty_relative.objectno(空→varchar(64))
--   guaranty_relative.guarantyid(空→varchar(64))
--   guaranty_relative.contractno(空→varchar(64))
--   odm_cms_cms_collateraltype_info.data_dt(date→date)
--   odm_cms_cms_collateraltype_info.invaliddate(string→date)
--   odm_cms_cms_collateraltype_info.validdate(string→date)
--   odm_cms_cms_collateraltype_info.inputdate(string→date)
--   odm_cms_cms_collateraltype_info.updatedate(string→date)
--   odm_cms_guaranty_contract.data_dt(date→date)
--   odm_cms_guaranty_contract.signdate(string→date)
--   odm_cms_guaranty_contract.begindate(string→date)
--   odm_cms_guaranty_contract.enddate(string→date)
--   odm_cms_guaranty_contract.inputdate(string→date)
--   odm_cms_guaranty_contract.updatedate(string→date)
--   odm_cms_guaranty_contract.commondate(string→date)
--   odm_cms_guaranty_contract.closingdate(string→date)
--   odm_cms_guaranty_contract.dp_load_dt(string→date)
--   odm_cms_guaranty_info.data_dt(date→date)
--   odm_cms_guaranty_info.guarantydate(string→date)
--   odm_cms_guaranty_info.begindate(string→date)
--   odm_cms_guaranty_info.ownertime(string→date)
--   odm_cms_guaranty_info.evaldate(string→date)
--   odm_cms_guaranty_info.guarantyregdate(string→date)
--   odm_cms_guaranty_info.guarantywodate(string→date)
--   odm_cms_guaranty_info.inputdate(string→date)
--   odm_cms_guaranty_info.updatedate(string→date)
--   odm_cms_guaranty_info.ownerplustime(string→date)
--   odm_cms_guaranty_info.guarantybuydate(string→date)
--   odm_cms_guaranty_info.insurenddate(string→date)
--   odm_cms_guaranty_info.insurestartdate(string→date)
--   odm_cms_guaranty_info.confirmtime(string→date)
--   odm_cms_guaranty_info.enddate(string→date)
--   odm_cms_guaranty_info.reevaldate(string→date)
--   odm_cms_guaranty_info.freezedate(string→date)
--   odm_cms_guaranty_info.firstselldate(string→date)
--   odm_cms_guaranty_info.dp_load_dt(string→date)
--   odm_cms_guaranty_relative.data_dt(date→date)
--   odm_cms_guaranty_relative.dp_load_dt(string→date)
--   org_info.orgid(空→varchar(64))
--   org_info.orgname(空→varchar(64))
--   org_info.mainframeorgid(空→varchar(64))
--   org_info.orglevel(空→varchar(200))
--   org_info.belongarea(空→varchar(200))
--   rpt_mrtgplg_dtl.datadt(date→date)
--   rpt_mrtgplg_dtl.valtdt(string→date)
--   rpt_mrtgplg_dtl.expdt(string→date)
--   rpt_mrtgplg_dtl.dp_load_dt(string→date)
--   samecustomer_relative.certenddate(VARCHAR2(10)→date)
--   user_info.userid(空→varchar(64))
--   user_info.username(空→varchar(64))
