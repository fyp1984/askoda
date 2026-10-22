/**
 * 前端展示层「说人话」工具集。
 *
 * 背景：后端返回的是面向系统的字段（slot id、置信度小数、证据等级 P0/P1/P8、
 * 生成器机型 deterministic-planner、可能为 null 的版本号）。这些直接铺到界面上
 * 就是业务方看不懂的技术黑话（见 2026-10-21 用户体验反馈）。
 * 这里集中做一层映射，页面只调这些函数，保证「同一字段在全站说法一致」。
 */

/** 六槽位中文名（后端 slot_labels 为 null 时的兜底）。 */
export const SLOT_LABELS_ZH: Record<string, string> = {
  subject: '统计主体',
  granularity: '时间粒度',
  time: '统计期间',
  scope: '范围条件',
  fields: '指标字段',
  risks: '风险项',
}

/** 槽位 id → 中文名；未知则原样返回，避免显示空。 */
export function slotLabel(k: string): string {
  return SLOT_LABELS_ZH[k] || k
}

/** 证据等级人话（有 level_name 时优先用 level_name）。 */
const EVIDENCE_ZH: Record<string, string> = {
  P0: '权威基准',
  P1: '业务口径',
  P2: '业务确认',
  P8: '通用规则',
}

/** 证据等级展示：优先后端给的 level_name，否则按字典映射。 */
export function evName(level?: string, levelName?: string): string {
  if (levelName) return levelName
  if (level && EVIDENCE_ZH[level]) return EVIDENCE_ZH[level]
  return level || '-'
}

/** 置信度渲染成百分比：0.55 → 55%；已是百分数的（>1）原样四舍五入。 */
export function fmtPct(v: unknown): string {
  if (typeof v !== 'number' || Number.isNaN(v)) return '-'
  return `${Math.round(v <= 1 ? v * 100 : v)}%`
}

/** 字段缺失原因友好化（后端 miss_reason.code）。 */
const MISS_REASON_ZH: Record<string, string> = {
  'F-MISS-NO-SUBJECT': '还没识别到统计主体（门店 / 商品 / 会员等），暂无法给出指标候选',
  'F-MISS-NO-GRAN': '还没确定时间粒度，暂无法给出指标候选',
}

/** miss_reason 可能是字符串或 {code, message} 对象，统一取出友好文案。 */
export function missReasonText(miss: unknown): string | null {
  if (!miss) return null
  if (typeof miss === 'string') return miss
  const m = miss as { code?: string; message?: string }
  if (m.code && MISS_REASON_ZH[m.code]) return MISS_REASON_ZH[m.code]
  return m.message || m.code || null
}

/** SQL 生成器的人话名。 */
const GEN_ZH: Record<string, string> = {
  'deterministic-planner': '确定性规划器（规则）',
  deterministic: '确定性规划器（规则）',
  llm: '大模型生成',
  candidate_sql: '候选 SQL（人工 / Agent）',
  candidate: '候选 SQL（人工 / Agent）',
}

/** 生成器机型 → 人话；未知机型原样返回。 */
export function genLabel(g?: string): string {
  if (!g) return '-'
  return GEN_ZH[g] || g
}

/** 需求版本号展示：null → 破折号，避免出现孤零零的 "v"。 */
export function fmtReqVer(v: unknown): string {
  if (typeof v === 'number') return `v${v}`
  if (typeof v === 'string' && v) return v.startsWith('v') ? v : `v${v}`
  return '—'
}

/**
 * 证据「定位」列人话化。
 * 后端 locator 是给系统查来源用的内部指针（字段路径 / chunk 哈希 / 坐标 /
 * MDL 关系名 / 规则号），直接铺给业务方就是天书。这里统一翻成业务能读的标签，
 * 需要精确原文的排查由接口 detail 承担，不占业务界面。
 */
const LOCATOR_ZH: Record<string, string> = {
  'demand.title': '需求标题',
  'demand.business_context': '业务背景',
  'demand.description': '需求描述',
  'demand.expected_output': '期望产出',
}

export function locatorText(loc?: string): string {
  if (!loc) return '—'
  const s = String(loc).trim()
  if (LOCATOR_ZH[s]) return LOCATOR_ZH[s]
  if (s.startsWith('confirmation:')) return '本次口径答复'
  if (s.startsWith('question_id=')) return '本次口径答复'
  if (s.startsWith('chunk=')) return '知识片段'
  if (s.startsWith('mdl.relationships.')) return '表关系'
  if (s.startsWith('mdl.')) return '语义模型'
  if (s.startsWith('rules.')) return '通用规则'
  if (s.startsWith('column:')) return '字段'
  if (s.startsWith('table:')) return '数据表'
  if (s.startsWith('metric:')) return '指标'
  return s
}

/** 证据等级分组统计（给「证据链」折叠头做一句话摘要，业务方不展开也能看懂）。 */
const EVIDENCE_GROUP_ZH: Record<string, string> = {
  P1: '业务说明',
  P2: '业务确认',
  P5: '系统资料',
  P7: '表关系',
  P8: '通用规则',
}

export function evidenceSummary(chain: Array<Record<string, any>>): string {
  const order = ['P0', 'P1', 'P2', 'P5', 'P7', 'P8']
  const cnt: Record<string, number> = {}
  for (const e of chain) {
    const lv = String(e?.level || '')
    if (!lv) continue
    cnt[lv] = (cnt[lv] || 0) + 1
  }
  const parts = order.filter((l) => cnt[l]).map((l) => `${EVIDENCE_GROUP_ZH[l] || l} ${cnt[l]}`)
  return parts.join(' · ')
}
