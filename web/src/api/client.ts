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
  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code || 'UNKNOWN'
    this.hint = body.hint || '请稍后重试；若持续失败，请联系运维。'
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

export const api = {
  health: () => request<Record<string, unknown>>('/health'),
  tools: () => request<{ ok: boolean; count: number; tools: unknown[] }>('/tools'),
  datasets: () =>
    request<{ datasets?: Array<{ key: string; label: string; domain: string }> }>('/datasets'),

  similarPrecheck: (body: { title: string; description?: string; top?: number; threshold?: number }) =>
    post<SimilarPrecheckResp>('/demand/similar-precheck', body),

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
  confirm: (demandId: string, body: { question_id: string; answer: string; choice?: string; actor?: string }) =>
    post<ConfirmResp>(`/demand/${encodeURIComponent(demandId)}/confirm`, body),
  sqlGenerate: (demandId: string, dataset: string, candidateSql?: string | string[]) =>
    post<SqlResp>(`/demand/${encodeURIComponent(demandId)}/sql`, {
      dataset,
      candidate_sql: candidateSql,
    }),
  sqlExecute: (demandId: string, dataset: string, sql?: string) =>
    post<SqlExecResp>(`/demand/${encodeURIComponent(demandId)}/sql/execute`, { dataset, sql }),
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

  // ---- M8 菜单② 知识储备 ----
  knowledgeHealth: () => request<Record<string, any>>('/knowledge/health'),
  knowledgeDocuments: (limit = 50) => request<Record<string, any>>(`/knowledge/documents?limit=${limit}`),
  knowledgeSearch: (body: { question: string; top_k?: number; threshold?: number; vector_weight?: number; demand_id?: string }) =>
    post<KnowledgeSearchResp>('/knowledge/search', body),
  knowledgeCitations: (args: { demand_id?: string; include_retired?: boolean; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (args.demand_id) q.set('demand_id', args.demand_id)
    if (args.include_retired) q.set('include_retired', 'true')
    q.set('limit', String(args.limit ?? 50))
    return request<Record<string, any>>(`/knowledge/citations?${q.toString()}`)
  },

  // ---- M8 菜单③ 数据源接入 ----
  datasourceScan: (dataset: string, persist = true) =>
    post<Record<string, any>>('/datasource/scan', { dataset, persist }),
  datasourceVersion: (dataset: string) =>
    request<Record<string, any>>(`/datasource/version?dataset=${encodeURIComponent(dataset)}`),
  datasourceCandidates: (demandId: string, dataset: string) =>
    request<Record<string, any>>(
      `/datasource/candidates?demand_id=${encodeURIComponent(demandId)}&dataset=${encodeURIComponent(dataset)}`,
    ),
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

export type KnowledgeSearchResp = {
  ok?: boolean
  question?: string
  citations?: Array<{
    document_name?: string
    chunk_id?: string
    similarity?: number
    content?: string
    positions?: unknown
  }>
  answer_context?: string
  error?: string
}

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
  demand: Record<string, any> | null
  analysis: Record<string, any> | null
  confirmations: Record<string, any> | null
  requirement: Record<string, any> | null
  context_pack: Record<string, any> | null
  runs: Record<string, any> | null
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

export type SqlResp = {
  sql_draft?: string
  revised_sql?: string | null
  generator?: string
  generation_notes?: string[]
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
