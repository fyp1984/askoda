import { useState } from 'react'
import { api, type E2eStatus } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox, RuleBadge } from '../components/ui'
import RichText from '../components/RichText'

const SLOT_ORDER = ['subject', 'granularity', 'time', 'scope', 'fields', 'risks'] as const

/**
 * 页2 · 查看分析
 * 六槽位 + 证据链 + 规则分级（blocking 标红、warning 标黄）。
 */
export default function PageAnalysis({
  demandId,
  dataset,
  status,
  onRefresh,
}: {
  demandId: string | null
  dataset: string
  status: E2eStatus | null
  onRefresh: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  if (!demandId) {
    return (
      <div className="page">
        <Card title="查看分析">
          <p className="muted">还没有需求。请先到「提交需求」页提交一条。</p>
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
            还没有分析结果。点右上角「开始分析」生成第一轮语义分析与证据链。
          </p>
        ) : (
          <>
            <div className="meta-row">
              <span>
                轮次 round_no：<b>{analysis.round_no ?? '-'}</b>
              </span>
              <span>
                整体置信度：<b>{analysis.confidence_overall ?? '-'}</b>
              </span>
              {analysis.confidence_cap ? (
                <span className="cap">置信度上限：{analysis.confidence_cap}</span>
              ) : null}
              {(analysis.degraded_sources || []).length > 0 ? (
                <span className="cap">降级来源：{(analysis.degraded_sources || []).join('、')}</span>
              ) : null}
            </div>

            {(blocking.length > 0 || warning.length > 0) && (
              <div className="rules">
                {blocking.map((r, i) => (
                  <div className="rule rule-blocking" key={`b${i}`}>
                    <RuleBadge severity="blocking" ruleId={r.rule_id} />
                    <div className="rule-body">
                      <div className="rule-stmt"><RichText text={r.statement} /></div>
                      {r.why ? <div className="rule-why"><RichText text={r.why} /></div> : null}
                    </div>
                  </div>
                ))}
                {warning.map((r, i) => (
                  <div className="rule rule-warning" key={`w${i}`}>
                    <RuleBadge severity="warning" ruleId={r.rule_id} />
                    <div className="rule-body">
                      <div className="rule-stmt"><RichText text={r.statement} /></div>
                      {r.why ? <div className="rule-why"><RichText text={r.why} /></div> : null}
                    </div>
                  </div>
                ))}
                {blocking.length > 0 ? (
                  <div className="rule-hint">
                    有阻断级规则：必须先到「口径确认」页把标红口径答复完，才能生成 SQL。
                  </div>
                ) : null}
              </div>
            )}

            <h4 className="sec">六槽位</h4>
            <div className="slots">
              {SLOT_ORDER.map((k) => {
                const body = slots[k]
                const cands = body?.candidates || []
                const needConfirm = !!body?.needs_confirmation
                return (
                  <div className={`slot${needConfirm ? ' slot-confirm' : ''}`} key={k}>
                    <div className="slot-head">
                      <span className="slot-name">{labels[k] || k}</span>
                      {needConfirm ? <span className="chip chip-red">待确认</span> : null}
                    </div>
                    {cands.length === 0 ? (
                      <div className="slot-empty">本轮无候选</div>
                    ) : (
                      <ul className="slot-list">
                        {cands.map((c: any, i: number) => (
                          <li key={i}>
                            <div className="slot-val"><RichText text={c.value} /></div>
                            <div className="slot-meta">
                              置信度 {c.confidence}
                              {c.needs_confirmation ? <span className="chip chip-red">需确认</span> : null}
                            </div>
                            {(c.evidence || []).length > 0 ? (
                              <div className="slot-ev">
                                {(c.evidence || []).slice(0, 3).map((e: any, j: number) => (
                                  <span className="ev-tag" key={j}>
                                    {e.level} · {e.locator}
                                  </span>
                                ))}
                              </div>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                    {body?.row_definition_notes ? (
                      <div className="slot-notes"><RichText text={String(body.row_definition_notes)} /></div>
                    ) : null}
                  </div>
                )
              })}
            </div>

            <h4 className="sec">
              证据链 <span className="muted">（共 {evidence.length} 条）</span>
            </h4>
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
                      <td className="mono">{e.locator}</td>
                      <td className="ev-content"><RichText text={e.content} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>
    </div>
  )
}