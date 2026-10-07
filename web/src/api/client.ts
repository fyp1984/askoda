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