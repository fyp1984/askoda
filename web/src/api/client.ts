/**
 * 前端唯一的后端出口。
 *
 * 红线 R2：这里只允许出现**相对路径** `/api/v1/...`。
 * 不得出现任何开源组件的地址或端口（具体端口号见 bff/config.py，此处不落笔）。
 * 开发态由 vite proxy 转发到 BFF，生产态由 BFF 同源托管。
 */
import type { ApiErrorBody } from './schema'

export class ApiError extends Error {
  code: string
  hint: string
  status: number
  /** 原始错误体（口径校验的逐条原因等结构化信息在这里，不进 message） */
  body: Partial<ApiErrorBody> & Record<string, unknown>
  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code || 'UNKNOWN'
    this.hint = body.hint || '请稍后重试；若持续失败，请联系运维。'
    this.body = (body ?? {}) as Partial<ApiErrorBody> & Record<string, unknown>
  }
}

const BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch (e) {
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: `连不上后端服务：${(e as Error).message}`,
      hint: '请确认 BFF 已启动，然后刷新页面重试。',
    })
  }
  const text = await resp.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, (data ?? {}) as Partial<ApiErrorBody>)
  }
  return data as T
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

const put = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) })

export const api = {
  health: () => request<Record<string, unknown>>('/health'),
  tools: () => request<{ ok: boolean; count: number; tools: unknown[] }>('/tools'),
  datasets: () =>
    request<{ datasets?: Array<{ key: string; label: string; domain: string }> }>('/datasets'),

  similarPrecheck: (body: { title: string; description?: string; top?: number; threshold?: number }) =>
    post<SimilarPrecheckResp>('/demand/similar-precheck', body),

  // ---- 退回需求方补充（CR010）----
  // 缺口处置第一顺位是退需求而不是补知识：补知识是小时/天级的跨部门动作，
  // 退需求是分钟级的一个人动作。这三个接口支撑「预览 → 退回 → 就地补充 → 回流」。
  clarifyPack: (
    demandId: string,
    args: { dataset?: string; gap_kind?: string; gap_text?: string } = {},
  ) => {
    const q = new URLSearchParams()
    q.set('dataset', args.dataset || 'B')
    if (args.gap_kind) q.set('gap_kind', args.gap_kind)
    if (args.gap_text) q.set('gap_text', args.gap_text)
    return request<Record<string, unknown>>(
      `/demand/${encodeURIComponent(demandId)}/clarify?${q.toString()}`,
    )
  },
  returnDemand: (
    demandId: string,
    body: {
      dataset?: string
      gap_kind?: string
      gap_text?: string
      items?: { id?: string; slot?: string; question?: string; answer?: string }[]
      note?: string
      actor?: string
    },
  ) => post<Record<string, unknown>>(`/demand/${encodeURIComponent(demandId)}/return`, body),
  supplementDemand: (
    demandId: string,
    body: {
      answers?: { id?: string; slot?: string; question?: string; answer?: string }[]
      extra_description?: string
      fields?: Record<string, string>
      actor?: string
    },
  ) => post<Record<string, unknown>>(`/demand/${encodeURIComponent(demandId)}/supplement`, body),

  createDemand: (body: Record<string, unknown>) => post<Record<string, unknown>>('/demand', body),
  demandList: (args: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (args.status) q.set('status', args.status)
    q.set('limit', String(args.limit ?? 20))
    q.set('offset', String(args.offset ?? 0))
    return request<DemandListResp>(`/demand?${q.toString()}`)
  },
  setDemandStatus: (demandId: string, body: { status: string; note?: string; actor?: string }) =>
    post<Record<string, unknown>>(`/demand/${encodeURIComponent(demandId)}/set-status`, body),
  analyze: (demandId: string, dataset: string, actor: string) =>
    post<Record<string, unknown>>(`/demand/${encodeURIComponent(demandId)}/analyze`, { dataset, actor }),
  e2eStatus: (demandId: string, dataset = 'B') =>
    request<E2eStatus>(`/demand/${encodeURIComponent(demandId)}/e2e-status?dataset=${dataset}`),
  confirm: (
    demandId: string,
    body: { question_id: string; answer: string; choice?: string; actor?: string; dataset?: string },
  ) => post<ConfirmResp>(`/demand/${encodeURIComponent(demandId)}/confirm`, body),
  sqlGenerate: (demandId: string, dataset: string, candidateSql?: string | string[]) =>
    post<SqlResp>(`/demand/${encodeURIComponent(demandId)}/sql`, {
      dataset,
      candidate_sql: candidateSql,
    }),
  sqlExecute: (demandId: string, dataset: string, sql?: string) =>
    post<SqlExecResp>(`/demand/${encodeURIComponent(demandId)}/sql/execute`, { dataset, sql }),

  // ---- 三道闸门（FR-4.11 / FR-4.10 / FR-7.5）----
  // 位置不同、判据不同，**不合并**：合规预检（该不该答）/ 可满足性预检（能不能答）/
  // 危险语句拦截（语句危不危险）。
  demandGates: (demandId: string, dataset = 'B') =>
    request<GatesResp>(`/demand/${encodeURIComponent(demandId)}/gates?dataset=${dataset}`),
  compliancePrecheck: (body: { text?: string; demand_id?: string; dataset?: string; actor?: string }) =>
    post<CompliancePrecheckResp>('/compliance/precheck', body),
  dangerGuard: (body: {
    sql: string
    dataset?: string
    demand_id?: string
    explain_rows?: number
    actor?: string
  }) => post<DangerGuardResp>('/compliance/danger-guard', body),

  // ---- 全量人工复核（FR-6.7 / AC-13 / D15）----
  reviewQueue: (args: { status?: string; dataset?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (args.status) q.set('status', args.status)
    if (args.dataset) q.set('dataset', args.dataset)
    q.set('limit', String(args.limit ?? 50))
    return request<ReviewQueueResp>(`/review/queue?${q.toString()}`)
  },
  reviewDetail: (demandId: string) =>
    request<ReviewDetailResp>(`/demand/${encodeURIComponent(demandId)}/review`),
  reviewSummary: (demandId: string, dataset = '') =>
    request<ReviewSummaryResp>(
      `/demand/${encodeURIComponent(demandId)}/review/summary?dataset=${encodeURIComponent(dataset)}`,
    ),
  reviewDecide: (
    taskId: string,
    body: { decision: string; reviewer: string; note?: string; rewritten_sql?: string },
  ) => post<Record<string, any>>(`/review/${encodeURIComponent(taskId)}/decide`, body),

  // ---- 分级分类自动脱敏（FR-7.8 / FR-7.9）----
  classificationPolicies: (dataset = '') =>
    request<ClassificationPoliciesResp>(
      `/classification/policies?dataset=${encodeURIComponent(dataset)}`,
    ),
  saveClassificationPolicy: (body: {
    level: string
    category?: string
    pattern: string
    algorithm?: string
    params?: Record<string, unknown>
    description?: string
    dataset?: string
    policy_id?: string
    actor?: string
  }) => put<Record<string, any>>('/classification/policies', body),
  classificationPreview: (body: { dataset?: string; columns: string[]; rows: unknown[][] }) =>
    post<ClassificationPreviewResp>('/classification/preview', body),

  // ---- P2 三条：多轮追问（FR-4.8）/ 本体可视化（FR-2.6）/ 审计导出（FR-7.7）----
  // 导读第 1 节：Demo 范围为全量 60 条 FR，P2 三条在 Demo 中照常呈现。
  conversation: (demandId: string, dataset = 'B') =>
    request<ConversationResp>(
      `/demand/${encodeURIComponent(demandId)}/conversation?dataset=${encodeURIComponent(dataset)}`,
    ),
  followup: (demandId: string, body: { instruction: string; dataset?: string; actor?: string }) =>
    post<FollowupResp>(`/demand/${encodeURIComponent(demandId)}/followup`, body),
  ontologyBrowse: (args: { dataset?: string; domain?: string; table?: string; column?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    q.set('dataset', args.dataset || 'B')
    if (args.domain) q.set('domain', args.domain)
    if (args.table) q.set('table', args.table)
    if (args.column) q.set('column', args.column)
    q.set('limit', String(args.limit ?? 50))
    return request<OntologyBrowseResp>(`/ontology/browse?${q.toString()}`)
  },
  auditExport: (args: { dataset?: string; demand_id?: string; limit?: number; fmt?: string; actor?: string } = {}) => {
    const q = new URLSearchParams()
    if (args.dataset) q.set('dataset', args.dataset)
    if (args.demand_id) q.set('demand_id', args.demand_id)
    q.set('limit', String(args.limit ?? 500))
    q.set('fmt', args.fmt || 'csv')
    if (args.actor) q.set('actor', args.actor)
    return request<AuditExportResp>(`/audit/export?${q.toString()}`)
  },

  citations: (demandId: string) =>
    request<{ ok: boolean; source?: string; total?: number; items?: unknown[]; degraded_reason?: string }>(
      `/demand/${encodeURIComponent(demandId)}/citations`,
    ),

  // ---- 审计回放（技术人员视角的完整判断链）----
  // version 传 0 取最近一次；传 n 取第 n 次执行（从 1 起）。
  // BFF 已把「第 n 次」与列表项的对应关系算好（versions[].version_idx），
  // 前端直接回传该项的 version_idx 即可，不要自己拿数组下标推。
  sqlRunReplay: (demandId: string, version = 0) =>
    request<SqlRunReplayResp>(
      `/demand/${encodeURIComponent(demandId)}/replay?version=${version}`,
    ),

  // ---- M8 菜单① 语义层 · MDL 字典 ----
  semanticManifest: (dataset: string) =>
    request<Record<string, any>>(`/semantic/manifest?dataset=${encodeURIComponent(dataset)}`),
  semanticMdl: (args: { dataset: string; table?: string; keyword?: string; include_hidden?: boolean }) => {
    const q = new URLSearchParams({ dataset: args.dataset })
    if (args.table) q.set('table', args.table)
    if (args.keyword) q.set('keyword', args.keyword)
    q.set('include_hidden', String(!!args.include_hidden))
    return request<MdlResp>(`/semantic/mdl?${q.toString()}`)
  },
  semanticGlossary: (args: { term?: string; keyword?: string } = {}) => {
    const q = new URLSearchParams()
    if (args.term) q.set('term', args.term)
    if (args.keyword) q.set('keyword', args.keyword)
    return request<Record<string, any>>(`/semantic/glossary?${q.toString()}`)
  },
  semanticDryRun: (sql: string, dataset: string) =>
    post<Record<string, any>>('/semantic/dry-run', { sql, dataset }),
  semanticCollect: (dataset: string, engine = 'native') =>
    post<Record<string, any>>('/semantic/collect', { dataset, engine }),

  // ---- 知识管理接口已按 CR009 整体剔除 ----
  // 知识收集/治理全部在 WeKnora 知识库平台完成，Askoda 客户端不设上传/检索/资产入口；
  // 分析链路对知识的调取走网关 MCP（服务端内部消费面），不经客户端 REST。

  // ---- M8 菜单③ 数据源接入 ----
  datasourceScan: (dataset: string, persist = true) =>
    post<Record<string, any>>('/datasource/scan', { dataset, persist }),
  datasourceVersion: (dataset: string) =>
    request<Record<string, any>>(`/datasource/version?dataset=${encodeURIComponent(dataset)}`),
  datasourceCandidates: (demandId: string, dataset: string) =>
    request<Record<string, any>>(
      `/datasource/candidates?demand_id=${encodeURIComponent(demandId)}&dataset=${encodeURIComponent(dataset)}`,
    ),

  // ---- 系统设置 · 大模型连接 ----
  // 密钥只进不出：GET 回来的是掩码（credential_masked），提交时留空 = 不改动原值。
  llmSettings: () => request<LlmSettings>('/settings/llm'),
  saveLlmSettings: (body: LlmSettingsIn) => post<LlmSaveResp>('/settings/llm', body),
  testLlm: (body: LlmTestIn) => post<LlmTestResp>('/settings/llm/test', body),

  // ---- 口径维护（M11）----
  // 口径是数据：读回来的是当前生效版本，保存走的是一次「校验 + 热部署 + 落库」。
  metrics: (dataset: string) => request<MetricsResp>(`/metrics/${encodeURIComponent(dataset)}`),
  saveMetrics: (body: { dataset: string; metrics: unknown; dry_run?: boolean; actor?: string }) =>
    put<MetricSaveResp>('/metrics', body),
  previewMetric: (body: { dataset: string; question: string; limit?: number }) =>
    post<MetricPreviewResp>('/metrics/preview', body),

  // ---- 系统设置 · 知识库接入（M10）----
  knowledgeSettings: () => request<KnowledgeSettings>('/settings/knowledge'),
  saveKnowledge: (body: { kb_ids: string[]; actor?: string; reset?: boolean }) =>
    post<KnowledgeSettings>('/settings/knowledge', body),
  testKnowledge: (body: { kb_id?: string; question?: string }) =>
    post<KnowledgeTestResp>('/settings/knowledge/test', body),

  // ---- 数据源接入 · 数据集登记（M10）----
  datasetRegistry: () => request<DatasetRegistryResp>('/datasource/registry'),
  probeDataset: (body: DatasetFormIn) => post<DatasetProbeResp>('/datasource/probe', body),
  registerDataset: (body: DatasetFormIn & { key: string; label?: string; domain?: string }) =>
    post<Record<string, any>>('/datasource/registry', body),
  unregisterDataset: (key: string, actor = '') =>
    request<Record<string, any>>(
      `/datasource/registry/${encodeURIComponent(key)}?actor=${encodeURIComponent(actor)}`,
      { method: 'DELETE' },
    ),

  // ---- 需求清理（M10）----
  purgeDemands: (body: { demand_ids: string[]; actor?: string; dry_run?: boolean }) =>
    post<PurgeResp>('/demand/purge', body),
}

// ---- M8 新增响应形状 ----
export type MdlColumn = {
  column_name: string
  column_label?: string
  data_type?: string
  nullable?: boolean
  is_sensitive?: boolean
  is_primary_key?: boolean
  description?: string
  source?: string
  ai_visible?: boolean
}

export type MdlTable = {
  dataset?: string
  table_name: string
  table_label?: string
  domain?: string
  grain?: string
  row_estimate?: number
  source?: string
  columns: MdlColumn[]
  column_count?: number
  hidden_column_count?: number
}

export type MdlResp = {
  ok: boolean
  returned?: number
  tables?: MdlTable[]
}

// KnowledgeSearchResp 已随 CR009 客户端知识接口剔除而移除。

// ---------------------------------------------------------------------------
// 响应形状（跟随网关实盘返回；网关返回体未走强类型 schema，故在此集中声明）
// ---------------------------------------------------------------------------
export type SimilarPrecheckResp = {
  ok: boolean
  source?: string
  degraded_reason?: string
  matched: number
  items: Array<{ demand_id: string; title?: string; similarity: number; status?: string }>
}

/** 需求列表项（跟随网关 demand_list 实盘返回）。 */
export type DemandListItem = {
  demand_id: string
  title?: string
  status?: string
  created_at?: string
  attachment_count?: number
  version?: number
}

export type DemandListResp = {
  total?: number
  returned?: number
  status_filter?: string | null
  items?: DemandListItem[]
}

/** 需求状态机取值，与 gateway/demand.py STATUSES 一致（服务端会再校验一次）。 */
export const DEMAND_STATUSES = [
  '待分析',
  '分析中',
  '待业务确认',
  '待补充修改',
  '待审核通过',
  '已通过',
  '已退回',
] as const

export type PartialError = {
  block: string
  tool: string
  kind: string
  message: string
  hint: string
}

export type E2eStatus = {
  demand_id: string
  dataset: string
  /** 需求实际所属数据集（最新一轮跨数据集的 dataset）；与 dataset 不一致时前端提示切换 */
  effective_dataset?: string
  /** 该需求在哪些数据集上做过分析（只有一个时前端自动对齐，多个时提示手动切换） */
  datasets_with_analysis?: string[]
  demand: Record<string, any> | null
  analysis: Record<string, any> | null
  confirmations: Record<string, any> | null
  requirement: Record<string, any> | null
  context_pack: Record<string, any> | null
  runs: Record<string, any> | null
  /** 闸门① 合规预检（FR-4.11）：语义分析阶段，早于任何 SQL 生成 */
  compliance?: CompliancePrecheckResp | null
  /** 闸门② 可满足性预检（FR-4.10）：结构化后 / 检索前 */
  satisfiability?: SatisfiabilityResp | null
  /** 全量人工复核汇总（FR-6.7）：交付页据此判「未经人工确认不得标可执行」 */
  review_summary?: ReviewSummaryResp | null
  partial_errors: PartialError[]
  healthy: boolean
}

export type ConfirmResp = {
  ok: boolean
  demand_id: string
  confirmation: Record<string, any>
  reanalysis: Record<string, any> | null
  backfill: {
    slots_changed: Array<{ slot: string; before: any; after: any }>
    p2_evidence_count: number
    p2_evidence: Array<Record<string, any>>
    backfilled: boolean
  }
  notice: string
}

export type GateLayer = {
  layer: string
  name: string
  ok: boolean
  skipped: boolean
  detail: string
}

// ---------------------------------------------------------------------------
// 三道闸门（FR-4.11 / FR-4.10 / FR-7.5）
// 位置不同、判据不同，**不合并**（导读第 2 节第 3 条）：
//   ① 合规预检（该不该答）→ ② 可满足性预检（能不能答）→ ③ 危险语句拦截（语句危不危险）
// ---------------------------------------------------------------------------
/** 合规预检命中项。matched 为 PII 时是预览串数组，命中规则时是命中片段。 */
export type ComplianceRisk = {
  type: string
  severity: string
  rule_id: string
  matched: string | string[]
  why: string
  suggestion: string
}

export type CompliancePrecheckResp = {
  ok: boolean
  /** 拦截 / 告警 / 通过；'拦截' ⇒ 生成 SQL 之前必须停下 */
  verdict?: string
  stage?: string
  demand_id?: string | null
  dataset?: string
  risk_count?: number
  blocking_count?: number
  risks?: ComplianceRisk[]
  rewrite_suggestion?: string
  /** 误拦率 / 命中率：**目标值 / 演示参数**，非实测（数字纪律） */
  targets?: Record<string, string>
  checked_at?: string
  error?: string
}

export type SatisfiabilityMissing = { tables?: string[]; columns?: string[]; terms?: string[] }
export type SatisfiabilityAlternative = { kind: string; label: string; detail: string }
export type DialectScope = { supported: string[]; unsupported: string[] }

export type SatisfiabilityResp = {
  ok: boolean
  /** 不支持 / 部分支持 / 可满足 */
  verdict?: string
  satisfiable?: boolean
  stage?: string
  demand_id?: string | null
  dataset?: string
  reasons?: string[]
  missing?: SatisfiabilityMissing
  alternatives?: SatisfiabilityAlternative[]
  /** 方言不承诺范围（NFR-1.3）：存储过程 / 复杂 UDF / 动态 SQL / 跨库联邦 */
  dialect_scope?: DialectScope
  policy?: string
  error?: string
}

export type DangerGuardReason = { type: string; detail: string; guide: string }

export type DangerGuardResp = {
  ok: boolean
  blocked?: boolean
  /** 拦截 / 通过 */
  verdict?: string
  demand_id?: string | null
  dataset?: string
  sql?: string
  reasons?: DangerGuardReason[]
  /** 未评估项：**缺席必须显式报未执行**，不得静默读成「没问题」 */
  not_evaluated?: string[]
  thresholds?: { explain_rows?: number; note?: string }
  error?: string
}

export type GateItem = {
  seq: number
  key: string
  title: string
  question: string
  stage: string
  result: Record<string, any> | null
}

export type GatesResp = {
  ok: boolean
  demand_id?: string
  dataset?: string
  /** 闸门③ 实际作用的那条 SQL（该需求最近一次生成 / 交付） */
  sql_effective?: string
  gates?: GateItem[]
}

// ---------------------------------------------------------------------------
// 全量人工复核（FR-6.7 / AC-13 / D15）—— 质量内核
// 铁律：每条 SQL 都进队列，**不看置信度分流**；结论只增不改。
// ---------------------------------------------------------------------------
export type ReviewPolicy = {
  full_review: string
  confidence_routing: boolean
  note: string
}

export type ReviewDecision = {
  decision_id: string
  task_id: string
  demand_id?: string
  decision: string
  reviewer: string
  note: string
  rewritten_sql: string
  created_at?: string
}

export type ReviewTaskItem = {
  task_id: string
  demand_id: string
  sql_run_id: string
  dataset?: string
  generator?: string
  confidence?: number | string | null
  status: string
  /** 列表接口不返回全文，只给预览 */
  sql_text?: string
  sql_preview?: string
  created_at?: string
  updated_at?: string
  /** 明细接口（reviewDetail）才带 */
  decisions?: ReviewDecision[]
  latest_decision?: ReviewDecision | null
}

export type ReviewQueueResp = {
  ok: boolean
  total?: number
  pending?: number
  items?: ReviewTaskItem[]
  policy?: ReviewPolicy
  error?: string
}

export type ReviewDetailResp = {
  ok: boolean
  demand_id?: string
  total?: number
  items?: ReviewTaskItem[]
  policy?: ReviewPolicy
  error?: string
}

export type ReviewSummaryResp = {
  ok: boolean
  demand_id?: string | null
  total?: number
  counts?: Record<string, number>
  /** 全部已复核通过；交付页据此判「未经人工确认不得标可执行」 */
  all_approved?: boolean
  policy?: ReviewPolicy
  error?: string
}

// ---------------------------------------------------------------------------
// 分级分类自动脱敏（FR-7.8 / FR-7.9 · NFR-3.8）
// 落点在**应用端**，不在 SQL 执行端；默认交付路径不携带脱敏逻辑。
// ---------------------------------------------------------------------------
export type LevelLegend = { level: string; label: string }

export type ClassificationPolicyItem = {
  policy_id: string
  dataset?: string
  level: string
  category?: string
  pattern: string
  algorithm: string
  params?: Record<string, unknown>
  description?: string
  enabled?: boolean
  /** table=界面维护的；builtin=内置默认策略 */
  source?: string
  updated_by?: string
  updated_at?: string
}

export type ClassificationPoliciesResp = {
  ok: boolean
  /** 策略源不可用 ⇒ 失败即关闭（按最高敏感级处理） */
  sealed?: boolean
  source_error?: string | null
  count?: number
  levels?: LevelLegend[]
  categories?: string[]
  algorithms?: Array<{ key: string; label: string }>
  items?: ClassificationPolicyItem[]
  note?: string
  error?: string
}

export type ColumnClassification = {
  column: string
  level: string
  level_label?: string
  category?: string
  algorithm: string
  algorithm_label?: string
  matched?: boolean
  /** closed-by-default ⇒ 未匹配 / 策略源不可用，按最高敏感级关闭 */
  source?: string
  description?: string
  params?: Record<string, unknown>
  reason?: string
}

export type ClassificationMaskResp = {
  ok: boolean
  sealed?: boolean
  reveal?: boolean
  columns?: string[]
  /** reveal=false 时为脱敏后的行；reveal=true 时为原始行 */
  rows?: unknown[][]
  raw_rows?: unknown[][] | null
  per_column?: ColumnClassification[]
  applied?: Array<{
    column: string
    level: string
    algorithm: string
    algorithm_label?: string
    category?: string
  }>
  closed?: Array<{ column: string; reason?: string }>
  levels?: LevelLegend[]
  location?: string
  note?: string
  error?: string
}

export type ClassificationPreviewResp = {
  ok: boolean
  sealed?: boolean
  columns?: string[]
  before_rows?: unknown[][]
  after_rows?: unknown[][]
  per_column?: ColumnClassification[]
  applied?: ColumnClassification[]
  closed?: ColumnClassification[]
  levels?: LevelLegend[]
  location?: string
  boundary?: string
  error?: string
}

// ---- P2 三条：多轮追问（FR-4.8）/ 本体可视化（FR-2.6）/ 审计导出（FR-7.7）----
// 导读第 1 节：Demo 范围为全量 60 条 FR，P2 三条在 Demo 中照常呈现。

/** 多轮追问 · 会话轮次（对齐 analysis_rounds 与 structured_requirements 版本）。 */
export type ConversationTurn = {
  turn: number
  /** 第 1 轮 = 原始需求；其后各轮 = 多轮追问 */
  kind: string
  instruction: string
  intents: Array<{ kind: string; snippet: string }>
  round_no: number | null
  requirement_version: number | null
  created_at: string
}

export type ConversationResp = {
  ok: boolean
  demand_id?: string
  dataset?: string
  title?: string
  status?: string
  turn_count?: number
  turns?: ConversationTurn[]
  /** 上下文保留口径说明：原文追加 + 历轮分析 + 历版结构化需求，只增不改 */
  context_policy?: string
  error?: string
}

export type FollowupResp = {
  ok: boolean
  demand_id?: string
  dataset?: string
  turn?: number
  instruction?: string
  intents?: Array<{ kind: string; snippet: string }>
  round_no?: number | null
  requirement_version?: number | null
  turns?: ConversationTurn[]
  context_retained?: string
  next?: string
  error?: string
}

/** 本体可视化浏览：每层给面包屑 + 下一层可下钻项。 */
export type OntologyNode = {
  key: string
  label: string
  table_name?: string
  column_name?: string
  grain?: string
  description?: string
  row_estimate?: number | null
  data_type?: string
  nullable?: boolean | null
  is_sensitive?: boolean
  is_primary_key?: boolean
  table_count?: number
}

export type OntologyCase = {
  key: string
  sql_run_id: string
  demand_id: string
  demand_title: string
  review_status: string
  adopted: boolean
  generator_model: string
  created_at: string
}

export type OntologyBrowseResp = {
  ok: boolean
  dataset?: string
  level?: string
  path?: Array<{ level: string; label: string; domain?: string; table?: string; column?: string }>
  children?: OntologyNode[]
  cases?: OntologyCase[]
  case_note?: string
  error?: string
}

/** 审计导出（FR-7.7）：返回内容由前端落成文件下载。 */
export type AuditExportResp = {
  ok: boolean
  format?: string
  count?: number
  filename?: string
  content?: string
  columns?: string[]
  retention?: string
  audit_fields?: string
  note?: string
  error?: string
}

export type SqlResp = {
  sql_draft?: string
  revised_sql?: string | null
  generator?: string
  generation_notes?: string[]
  /**
   * 生成侧拒绝/风险说明（如「未命中已建模意图」）。
   * 此前类型里漏了它 → 前端拿不到真实失败原因，只显示「还没有生成 SQL」，
   * 用户看不出到底是口径问题还是建模缺失。这是 2026-10-07 演示暴露的缺陷。
   */
  generation_risks?: string[]
  /** 结构化的拒绝说明，供界面排版（标题/差距/责任方/分步指引），非日志原文 */
  generation_block?: {
    blocked?: boolean
    title?: string
    reason?: string
    guide?: string
    supported?: string
    gap?: string
    guide_steps?: string[]
    needs?: { kind?: string; owner?: string }
  }
  field_mapping?: unknown[]
  review?: {
    status: string
    layers?: GateLayer[]
    review_notes?: string
    syntax_issues?: unknown[]
    semantic_issues?: unknown[]
    rule_violations?: unknown[]
  }
}

export type SqlExecResp = {
  execution_summary?: string
  row_count?: number
  sample_rows?: unknown[][]
  validation_result?: { columns?: string[]; row_count?: number; signals?: unknown[] }
  suspicious_signals?: unknown[]
  review?: { status: string; layers?: GateLayer[]; review_notes?: string }
  sql_run_id?: string
  requirement_version?: number
  schema_version?: string
  pack_version?: string
  deliverable?: boolean
  deliverable_reason?: string
  adopted?: boolean
  /** 分级分类脱敏块（FR-7.9）：落点在应用端，不在 SQL 执行端 */
  masking?: ClassificationMaskResp
  /** 闸门③ 危险语句拦截结果（FR-7.5） */
  danger_guard?: DangerGuardResp
  /** 生成后自动入队的复核任务（FR-6.7，全量入队不看置信度分流） */
  review_task?: { ok?: boolean; task_id?: string; created?: boolean; status?: string; error?: string }
}

// ---- 审计回放 ----
/** 一次执行的版本清单项（由 BFF 按「第 N 次」编号，前端直接回传 version_idx 即可回放）。 */
export type ReplayVersion = {
  version_idx: number
  sql_run_id: string
  created_at?: string
  review_status?: string
  adopted?: boolean
  generator_model?: string
  actor?: string
  requirement_version?: number | null
  schema_version?: string
  pack_version?: string | null
  generated_sql_preview?: string
}

/** 单个版本的完整判断链快照（网关 sql_run_replay 实盘返回）。 */
export type ReplaySnapshot = {
  demand_id: string
  sql_run_id: string
  version_idx: number
  created_at?: string
  actor?: string
  input?: {
    structured_requirement?: unknown
    structured_requirement_version_resolved?: number | null
    pack_version?: string | null
    schema_version?: string
    requirement_version?: number | null
  }
  sql?: {
    generated_sql?: string
    final_delivery_sql?: string
    adopted?: boolean
  }
  review?: {
    status?: string
    detail?: { layers?: GateLayer[]; review_notes?: string } | null
  }
  result?: {
    row_count?: number
    validation_result?: Record<string, any>
    suspicious_signals?: unknown[]
  }
  knowledge_citations?: Array<{
    citation_id: string
    document_name?: string
    chunk_id?: string
    question?: string
    similarity?: number
    retired_at?: string | null
    retired_reason?: string | null
  }>
  gaps?: string[]
}

export type SqlRunReplayResp = {
  ok: boolean
  demand_id: string
  total: number
  versions: ReplayVersion[]
  replay: ReplaySnapshot | null
  notice?: string
}

// ---------------------------------------------------------------------------
// 系统设置 · 大模型连接
// ---------------------------------------------------------------------------
/** 供应商预设（由后端下发，前端不硬编码；新增一家只需后端加一条）。 */
export type LlmProvider = {
  key: string
  label: string
  base_url: string
  models: string[]
  note?: string
}

export type LlmSettings = {
  ok: boolean
  /** 生效来源：db=界面保存的；env=环境变量；none */
  source?: 'db' | 'env' | 'none'
  provider?: string
  base_url?: string
  model?: string
  /** 用户意图（开关原值） */
  enabled?: boolean
  /** 实际是否会调用大模型（开关打开 且 三项配齐） */
  effective?: boolean
  configured?: boolean
  /** 还缺哪几项：base_url / api_key / model */
  missing?: string[]
  temperature?: number
  timeout?: number
  max_repair?: number
  /** 库里是否已有持久化配置（有 = 环境变量不再生效） */
  persisted?: boolean
  updated_by?: string
  updated_at?: string
  /** 凭据只给「有没有」和「尾号」。缺字段时按「已保存但不回显」处理。 */
  has_credential?: boolean
  credential_masked?: string
  env_configured?: boolean
  env_model?: string
  db_error?: string
  providers?: LlmProvider[]
  recent_changes?: Array<{
    action: string
    actor?: string
    at?: string
    credential_changed?: boolean
    model?: string
  }>
}

export type LlmSettingsIn = {
  provider?: string
  base_url?: string
  model?: string
  /** 留空 = 保持原有密钥不变 */
  api_key?: string
  enabled?: boolean
  temperature?: number
  timeout?: number
  max_repair?: number
  clear_credential?: boolean
  reset?: boolean
  actor?: string
}

export type LlmSaveResp = {
  ok: boolean
  changed?: string[]
  warnings?: string[]
  notice?: string
  effective?: boolean
  settings?: LlmSettings
}

export type LlmTestIn = {
  provider?: string
  base_url?: string
  model?: string
  /** 留空则用已保存的密钥测 */
  api_key?: string
  timeout?: number
}

export type LlmTestResp = {
  ok: boolean
  latency_ms?: number
  reply?: string
  model_reported?: string
  /** true = 这次用的是库里已存的密钥，不是本次输入 */
  used_saved_credential?: boolean
  notice?: string
  error?: string
  config?: { base_url?: string; model?: string; credential_masked?: string }
  /** 第一段探活（模型清单）的结论：用来区分「地址不通」和「模型太慢」 */
  probe?: {
    reachable?: boolean
    status?: number
    model_count?: number
    /** true=清单里有；false=没有；null=清单为空或不认识，无法判断 */
    model_listed?: boolean | null
    /** 这段结论能否定案：false 表示该地址不提供模型清单，需靠生成结果判断 */
    conclusive?: boolean
    error?: string
  }
}

// ---------------------------------------------------------------------------
// 知识库接入（M10）
// ---------------------------------------------------------------------------
export type KnowledgeBaseVisible = {
  id: string
  name: string
  /** 本次问答实际是否使用（勾选状态以服务端为准，避免刷新后被本地状态覆盖） */
  is_active?: boolean
  /** 是否为环境变量里配的默认库（部署期决定，界面上只是标注） */
  is_env_default?: boolean
  description?: string
  capabilities?: string[]
}

export type KnowledgeSettings = {
  ok: boolean
  kb_ids?: string[]
  /** 生效来源：db=界面保存；env=环境变量；none=未接入 */
  source?: 'db' | 'env' | 'none'
  env_kb_ids?: string[]
  persisted?: boolean
  updated_by?: string
  updated_at?: string
  db_error?: string
  /** 底座可见范围（硬边界）：不在这个列表里的库，勾了也搜不到 */
  visible?: KnowledgeBaseVisible[] | null
  visible_error?: string
  base?: string
  notice?: string
}

export type KnowledgeTestResp = {
  ok: boolean
  question?: string
  hits: number
  sources: Array<{ document_name?: string; similarity?: number; snippet?: string }>
  notice?: string
}

// ---------------------------------------------------------------------------
// 数据集登记（M10）
// ---------------------------------------------------------------------------
export type DatasetFormIn = {
  host?: string
  port?: number
  dbname?: string
  user?: string
  password?: string
  engine?: string
  mdl_json?: string
  actor?: string
}

export type RegisteredDataset = {
  key: string
  label?: string
  domain?: string
  engine?: string
  /** 只回显库名，地址与口令不出网关 */
  database?: string
  models?: number
  fields?: number
  has_inline_mdl?: boolean
  enabled?: boolean
  updated_by?: string
  updated_at?: string
}

export type DatasetRegistryResp = {
  ok: boolean
  registered: RegisteredDataset[]
  usable: Array<{ key: string; label?: string; domain?: string; models?: number; fields?: number; error?: string }>
  env_keys: string[]
}

export type DatasetProbeResp = {
  ok: boolean
  checks: Record<string, { ok: boolean; error?: string; hint?: string; [k: string]: any }>
  notice?: string
}

// ---------------------------------------------------------------------------
// 需求清理（M10）
// ---------------------------------------------------------------------------
export type PurgeResp = {
  ok: boolean
  dry_run?: boolean
  deleted?: Record<string, number>
  would_delete?: Record<string, number>
  missing?: string[]
  notice?: string
}

// ---------------------------------------------------------------------------
// 口径维护（M11）
// ---------------------------------------------------------------------------
export type MetricView = {
  name: string
  columns?: string[]
  comment?: string
  /** 只有编辑草稿里才有完整语句；列表读回来是空的（视图由数据侧维护） */
  statement?: string
}

export type MetricDef = {
  key: string
  label: string
  aliases: string[]
  view: string
  expr: string
  unit?: string
  fmt?: string
  /** snapshot=时点快照（取最新一期）；flow=区间流水（按自然月） */
  time_mode: 'snapshot' | 'flow'
  time_col: string
  dims: string[]
  definition?: string
  caveats?: string
  source?: string
}

export type MetricsResp = {
  ok: boolean
  dataset: string
  /** 该数据集有没有接入口径登记；false ⇒ 空状态，不是故障 */
  registered?: boolean
  notice?: string
  version: string
  views: MetricView[]
  metrics: MetricDef[]
  models?: number
  updated_by?: string
  updated_at?: string
}

export type MetricSaveResp = {
  ok: boolean
  dataset: string
  version: string
  views: number
  metrics: number
  added: string[]
  removed: string[]
  dry_run?: boolean
  notice?: string
}

export type MetricPreviewResp = {
  ok: boolean
  question: string
  metric?: string
  metric_label?: string
  engine?: string
  sql?: string
  row_count?: number
  columns?: string[]
  /** 后端按「列名数组 + 行数组」返回；也兼容按列名取值的对象行 */
  rows?: Array<Record<string, unknown> | unknown[]>
  blocked?: boolean
  reason?: string
  guide?: string
  error?: string
}
