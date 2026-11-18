import { useCallback, useEffect, useState } from 'react'
import {
  api,
  type CompliancePrecheckResp,
  type DangerGuardResp,
  type GateItem,
  type GatesResp,
  type SatisfiabilityResp,
} from '../api/client'
import { ErrorBox, Loading } from './ui'
import RichText from './RichText'

/**
 * 三道闸门串联可视化（FR-4.11 / FR-4.10 / FR-7.5）
 *
 * 为什么是三张独立卡片、而不是一句「安全检查通过」
 * ------------------------------------------------------------------
 * 三者在**不同阶段**回答**三个不同问题**（导读第 2 节第 3 条，不可合并）：
 *   ① 合规预检      语义分析阶段（早于任何 SQL 生成）  该不该答
 *   ② 可满足性预检  结构化后 / 检索前                  能不能答
 *   ③ 危险语句拦截  SQL 生成后                          语句危不危险
 * 合并会把「合规风险」与「能力缺口」混成一句错误提示，业务方与复核人都无法据以行动。
 *
 * 判据不复用：合规看敏感明文 / 越权 / 违规意图 / 提示注入；可满足性看要素缺口与方言范围；
 * 危险拦截看只读 / 多语句 / 无边界全表 / 超阈值。三条链路的结论各自独立。
 */

const VERDICT_CHIP: Record<string, string> = {
  通过: 'chip chip-green',
  可满足: 'chip chip-green',
  告警: 'chip chip-amber',
  部分支持: 'chip chip-amber',
  未评估: 'chip chip-muted',
  拦截: 'chip chip-red',
  不支持: 'chip chip-red',
}

function VerdictChip({ verdict }: { verdict?: string }) {
  if (!verdict) return <span className="chip chip-muted">未知</span>
  return <span className={VERDICT_CHIP[verdict] || 'chip chip-muted'}>{verdict}</span>
}

function GateBody({ gate }: { gate: GateItem }) {
  const r: any = gate.result
  if (gate.key === 'compliance_precheck') {
    const d = r as CompliancePrecheckResp | null
    if (!d || d.ok === false) return <p className="muted">{d?.error || '未取得结果'}</p>
    const risks = d.risks || []
    return (
      <>
        <div className="gatepipe-meta">
          <span>命中风险 <b>{d.risk_count ?? 0}</b> 条（阻断 {d.blocking_count ?? 0}）</span>
        </div>
        {risks.length === 0 ? (
          <p className="muted">未命中敏感明文 / 越权 / 违规意图 / 提示注入。</p>
        ) : (
          <ul className="gatepipe-risks">
            {risks.map((x, i) => (
              <li key={i} className={x.severity === 'blocking' ? 'risk-blocking' : ''}>
                <div className="gatepipe-risk-head">
                  <span className="chip chip-red">{x.type}</span>
                  <code className="mono">{x.rule_id}</code>
                </div>
                <div className="gatepipe-risk-why"><RichText text={x.why} /></div>
                {x.suggestion ? (
                  <div className="gatepipe-risk-sug">怎么改：<RichText text={x.suggestion} /></div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {d.targets ? (
          <div className="gatepipe-num">
            演示参数（目标值，非实测）：
            {Object.entries(d.targets).map(([k, v]) => (
              <span key={k} className="gatepipe-num-item">{k} {String(v)}</span>
            ))}
          </div>
        ) : null}
      </>
    )
  }

  if (gate.key === 'compliance_satisfiability') {
    const d = r as SatisfiabilityResp | null
    if (!d || d.ok === false) return <p className="muted">{d?.error || '未取得结果'}</p>
    const reasons = d.reasons || []
    const alts = d.alternatives || []
    return (
      <>
        {reasons.length === 0 ? (
          <p className="muted">需求要素齐全，当前数据源与方言范围内可满足。</p>
        ) : (
          <ul className="gatepipe-reasons">
            {reasons.map((x, i) => (
              <li key={i}><RichText text={x} /></li>
            ))}
          </ul>
        )}
        {alts.length > 0 ? (
          <div className="gatepipe-alts">
            <div className="gatepipe-alts-title">可替代方案</div>
            <ul>
              {alts.map((a, i) => (
                <li key={i}>
                  <b>{a.label}</b>
                  <span className="muted"> · <RichText text={a.detail} /></span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {d.dialect_scope ? (
          <div className="gatepipe-scope">
            <div>支持：{d.dialect_scope.supported.join('、')}</div>
            <div>不承诺：{d.dialect_scope.unsupported.join('、')}</div>
          </div>
        ) : null}
      </>
    )
  }

  // danger_guard
  const d = r as DangerGuardResp | null
  if (!d || d.ok === false) return <p className="muted">{d?.error || '未取得结果'}</p>
  const reasons = d.reasons || []
  const notEval = d.not_evaluated || []
  return (
    <>
      {d.sql ? null : <p className="muted">该需求还没有生成过 SQL，本条闸门暂无可作用对象。</p>}
      {reasons.length > 0 ? (
        <ul className="gatepipe-reasons">
          {reasons.map((x, i) => (
            <li key={i}>
              <span className="chip chip-red">{x.type}</span>
              <span className="muted"> {x.detail}</span>
              {x.guide ? <div className="gatepipe-risk-sug">怎么办：<RichText text={x.guide} /></div> : null}
            </li>
          ))}
        </ul>
      ) : d.sql ? (
        <p className="muted">只读、单语句、有边界，未命中危险特征。</p>
      ) : null}
      {notEval.length > 0 ? (
        <div className="gatepipe-noteval">
          <div className="gatepipe-noteval-title">未评估项（缺席即报未执行，不当作「没问题」）</div>
          <ul>
            {notEval.map((x, i) => (
              <li key={i}><RichText text={x} /></li>
            ))}
          </ul>
        </div>
      ) : null}
      {d.thresholds ? (
        <div className="gatepipe-num">
          演示参数（`[待核实 per DB]`）：EXPLAIN 阻断阈值 {d.thresholds.explain_rows?.toLocaleString?.() ?? d.thresholds.explain_rows} 行
          {d.thresholds.note ? ` · ${d.thresholds.note}` : ''}
        </div>
      ) : null}
    </>
  )
}

export default function GatePipeline({
  demandId,
  dataset,
  refreshToken = 0,
}: {
  demandId: string
  dataset: string
  /** 上层状态刷新令牌：变化时重新取数 */
  refreshToken?: number
}) {
  const [data, setData] = useState<GatesResp | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      setData(await api.demandGates(demandId, dataset))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [demandId, dataset])

  useEffect(() => {
    load()
  }, [load, refreshToken])

  const gates: GateItem[] = data?.gates || []
  const gate1 = gates.find((g) => g.key === 'compliance_precheck')
  const blocked = (gate1?.result as CompliancePrecheckResp | null)?.verdict === '拦截'

  return (
    <div className="gatepipe">
      <div className="gatepipe-head">
        <div className="gatepipe-head-l">
          <span className="gatepipe-title">三道闸门 · 串联把关</span>
          <span className="muted">
            阶段不同、判据不同，逐道独立；任意一道拦下，都不会产出可执行结果。
          </span>
        </div>
        <button type="button" className="linklike" onClick={load} disabled={busy}>
          {busy ? '刷新中…' : '刷新'}
        </button>
      </div>

      {busy && !data ? <Loading tip="正在逐道核对…" /> : null}
      <ErrorBox error={error} />

      {blocked ? (
        <div className="gatepipe-blocked" role="alert">
          <div className="gatepipe-blocked-title">闸门① 合规预检拦截：这条需求不该答</div>
          <div className="gatepipe-blocked-body">
            已在<b>语义分析阶段（早于任何 SQL 生成）</b>拦下。请按下方「怎么改」调整需求表述后重新分析；
            本系统不接受任何关闭门禁或留痕的指令。
          </div>
        </div>
      ) : null}

      <div className="gatepipe-flow">
        {gates.map((g, i) => {
          const verdict = (g.result as any)?.verdict
          const bad = verdict === '拦截' || verdict === '不支持'
          return (
            <div className="gatepipe-node-wrap" key={g.key}>
              <div className={`gatepipe-node${bad ? ' node-bad' : ''}`}>
                <div className="gatepipe-node-head">
                  <span className="gatepipe-seq">{g.seq}</span>
                  <span className="gatepipe-node-title">{g.title}</span>
                  <VerdictChip verdict={verdict} />
                </div>
                <div className="gatepipe-q">
                  <span className="chip chip-blue">{g.question}</span>
                  <span className="gatepipe-stage">{g.stage}</span>
                </div>
                <div className="gatepipe-node-body">
                  <GateBody gate={g} />
                </div>
              </div>
              {i < gates.length - 1 ? <div className="gatepipe-arrow" aria-hidden="true">→</div> : null}
            </div>
          )
        })}
      </div>

      {data?.sql_effective ? (
        <details className="techfold">
          <summary>闸门③ 作用的那条 SQL</summary>
          <pre className="sqlbox small">{data.sql_effective}</pre>
        </details>
      ) : null}
    </div>
  )
}
