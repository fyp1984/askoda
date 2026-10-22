import { useState } from 'react'
import { api, type E2eStatus } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox, RuleBadge } from '../components/ui'
import { Section, SlotCard } from '../components/Collapsible'
import RichText from '../components/RichText'
import { evName, fmtPct, missReasonText, slotLabel, locatorText, evidenceSummary } from '../lib/display'

const SLOT_ORDER = ['subject', 'granularity', 'time', 'scope', 'fields', 'risks'] as const

/** 把后端规则文本里裸露的槽位 id（如 granularity）换成中文口径名，避免业务方看不懂英文。 */
function humanizeSlots(text: string, labels: Record<string, string>): string {
  if (!text) return text
  const keys = Object.keys(labels)
  const list = keys.length ? keys : (SLOT_ORDER as readonly string[])
  const re = new RegExp(`\\b(${list.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})\\b`, 'gi')
  return text.replace(re, (m) => labels[m.toLowerCase()] || labels[m] || slotLabel(m))
}

/**
 * 页2 · 读懂你的需求
 * 六槽位 + 证据链 + 规则分级（blocking 标红、warning 标黄）。
 */
export default function PageAnalysis({
  demandId,
  dataset,
  status,
  onRefresh,
  onGoConfirm,
}: {
  demandId: string | null
  dataset: string
  status: E2eStatus | null
  onRefresh: () => void
  /** 有阻断级口径时，一键跳到「对一对关键说法」页答复 */
  onGoConfirm?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  if (!demandId) {
    return (
      <div className="page">
        <Card title="读懂你的需求">
          <p className="muted">还没有需求。请先到「提个需求」页提交一条。</p>
        </Card>
      </div>
    )
  }

  const analysis = status?.analysis
  const ruleCheck = analysis?.rule_check
  const blocking: any[] = ruleCheck?.blocking_risks || []
  const warning: any[] = ruleCheck?.warning_risks || []
  const evidence: any[] = analysis?.evidence_chain || []
  const slots = analysis?.slots || {}
  const labels = analysis?.slot_labels || {}

  // 已答复的槽位：确认页答完但后端还没自动回填时，分析页不能继续标红「待确认」
  // （否则两页状态打架——用户明明答过了，回来看还是红的）。
  const answeredSlots = new Set<string>(
    (((status?.confirmations?.items as any[]) || []).filter((i: any) => i.answered)).map((i: any) => String(i.slot)),
  )
  const evSummary = evidenceSummary(evidence)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.analyze(demandId, dataset, '')
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <Card
        title={`分析结果 · ${demandId}`}
        collapsible
        id="analysis-result"
        extra={
          <button className="primary" onClick={run} disabled={busy}>
            {busy ? '分析中…' : '开始分析 / 重新分析'}
          </button>
        }
      >
        {busy ? <Loading tip="正在跑语义分析并拼装证据链…" /> : null}
        <ErrorBox error={error} />
        <PartialErrorsBox errors={status?.partial_errors || []} />

        {!analysis || analysis.ok === false ? (
          <p className="muted">
            还没分析。点右上角「开始分析」，系统会拆解六槽位并生成证据链。
          </p>
        ) : (
          <>
            <div className="meta-row">
              <span>已分析轮次：<b>{analysis.round_no ?? '-'}</b></span>
              {typeof analysis.confidence_overall === 'number' ? (
                <span>理解置信度：<b>{fmtPct(analysis.confidence_overall)}</b></span>
              ) : null}
            </div>

            {(blocking.length > 0 || warning.length > 0) && (
              <div className="rules">
                {blocking.map((r, i) => (
                  <div className="rule rule-blocking" key={`b${i}`}>
                    <RuleBadge severity="blocking" ruleId={r.rule_id} />
                    <div className="rule-body">
                      <div className="rule-stmt"><RichText text={r.statement} /></div>
                      {r.why ? <div className="rule-why"><RichText text={humanizeSlots(String(r.why), labels)} /></div> : null}
                    </div>
                  </div>
                ))}
                {warning.map((r, i) => (
                  <div className="rule rule-warning" key={`w${i}`}>
                    <RuleBadge severity="warning" ruleId={r.rule_id} />
                    <div className="rule-body">
                      <div className="rule-stmt"><RichText text={r.statement} /></div>
                      {r.why ? <div className="rule-why"><RichText text={humanizeSlots(String(r.why), labels)} /></div> : null}
                    </div>
                  </div>
                ))}
                {blocking.length > 0 ? (
                  <div className="rule-hint">
                    <span>
                      有阻断级规则：请先到「对一对关键说法」页把标红口径答复完，系统才会放行生成 SQL。
                    </span>
                    {onGoConfirm ? (
                      <button type="button" className="linklike rule-hint-go" onClick={onGoConfirm}>
                        去「对一对关键说法」答复 →
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )}

            <Section
              title="六槽位"
              id="analysis-slots"
              desc="6 个口径维度，逐项给出理解与置信度；带「待确认」的到「对一对关键说法」答复"
            >
              <div className="slots">
                {SLOT_ORDER.map((k) => {
                  const body = slots[k]
                  const cands = body?.candidates || []
                  const needConfirm = !!body?.needs_confirmation
                  const answered = needConfirm && answeredSlots.has(k)
                  const resolved = body?.needs_confirmation === false && cands.length > 0
                  const missText = missReasonText(body?.miss_reason)
                  const noActiveRisk = k === 'risks' && blocking.length === 0 && warning.length === 0
                  return (
                    <SlotCard
                      name={labels[k] || slotLabel(k)}
                      needConfirm={needConfirm}
                      answered={answered}
                      resolved={resolved}
                      key={k}
                    >
                      {noActiveRisk ? (
                        <div className="slot-empty">当前未检出需要关注的风险（上游口径已全部确认）</div>
                      ) : cands.length === 0 ? (
                        <div className="slot-empty">
                          {missText ? missText : '本轮系统未给出候选，可到「对一对关键说法」补充口径'}
                        </div>
                      ) : (
                        <ul className="slot-list">
                          {cands.map((c: any, i: number) => (
                            <li key={i}>
                              <div className="slot-val"><RichText text={c.value} /></div>
                              <div className="slot-meta">
                                置信度 {fmtPct(c.confidence)}
                                {!resolved && !answered && c.needs_confirmation ? (
                                  <span className="chip chip-red">需确认</span>
                                ) : null}
                              </div>
                              {(c.evidence || []).length > 0 ? (
                                <div className="slot-ev">
                                  {(c.evidence || []).slice(0, 3).map((e: any, j: number) => (
                                    <span className="ev-tag" key={j}>
                                      {evName(e.level, e.level_name)} · {locatorText(e.locator)}
                                    </span>
                                  ))}
                                </div>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      )}
                      {needConfirm && !answered && !resolved && body?.row_definition_notes ? (
                        <div className="slot-notes"><RichText text={String(body.row_definition_notes)} /></div>
                      ) : null}
                      {answered ? (
                        <div className="slot-notes slot-notes-ok">
                          你已答复这条口径。若这里仍显示「待确认」，点右上角「重新分析」即可刷新。
                        </div>
                      ) : null}
                    </SlotCard>
                  )
                })}
              </div>
            </Section>

            <Section
              title="证据链"
              id="analysis-evidence"
              count={evidence.length}
              desc="业务方只看「业务说明 / 业务确认」即可，其余为系统内部依据（默认收起）"
              defaultOpen={false}
            >
              <div className="ev-summary">
                共 {evidence.length} 条依据：{evSummary}。带 P1 / P2 的「业务说明 / 业务确认」是判断你需求的依据；
                表关系、系统资料、通用规则属于系统内部依据，排查时才需要展开。
              </div>
              <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>等级</th>
                    <th>来源</th>
                    <th>定位</th>
                    <th>内容</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.map((e, i) => (
                    <tr key={i} className={e.level === 'P2' ? 'row-p2' : undefined}>
                      <td>
                        <span className={`lv lv-${(e.level || '').replace(/[^A-Za-z0-9]/g, '')}`}>
                          {e.level}
                        </span>
                        <span className="lv-name">{e.level_name}</span>
                      </td>
                      <td>{e.source}</td>
                      <td className="mono">{locatorText(e.locator)}</td>
                      <td className="ev-content"><RichText text={e.content} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </Section>
          </>
        )}
      </Card>
    </div>
  )
}