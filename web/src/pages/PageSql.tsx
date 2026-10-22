import { useState } from 'react'
import { api, type E2eStatus, type GateLayer, type SqlExecResp, type SqlResp } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'
import { Section } from '../components/Collapsible'
import RichText from '../components/RichText'
import GapFiller from '../components/GapFiller'
import { fmtReqVer, genLabel } from '../lib/display'

/** 门禁层中文名（后端 layer 为 L0/L1… 技术 id，给业务方看人话）。 */
const GATE_LAYER_ZH: Record<string, string> = {
  L0: '语法校验',
  L1: '静态规则',
  L2: '语义覆盖',
  L3: '只读执行',
  L4: '交付判定',
}

/**
 * 页4 · 让系统写出 SQL
 * SQL + 门禁逐层结果 + 结果集前 N 行+ 溯源三件套 + 知识引用明细。
 */
export default function PageSql({
  demandId,
  dataset,
  actor,
  status,
  onRefresh,
}: {
  demandId: string | null
  dataset: string
  actor: string
  status: E2eStatus | null
  onRefresh: () => void
}) {
  const [sql, setSql] = useState<SqlResp | null>(null)
  const [exec, setExec] = useState<SqlExecResp | null>(null)
  const [cites, setCites] = useState<{ source?: string; items?: any[]; total?: number; degraded_reason?: string } | null>(null)
  const [busy, setBusy] = useState<'gen' | 'exec' | 'cit' | null>(null)
  const [error, setError] = useState<unknown>(null)
  // 「重新对齐」成功后的持久反馈（GapFiller 在 SQL 产出后即卸载，提示需留在页面级）
  const [realignFeedback, setRealignFeedback] = useState<string | null>(null)
  // 候选 SQL（Agent/技术人员手工提供）：走 candidate_sql 通道，
  // 绕过确定性兜底 planner（planner 只认已建模意图，未命中即拒）。
  // 网关侧 contract 一直是支持的（gateway/app.py: sql_generate 的 candidate_sql 参数），
  // 缺的是界面入口 —— 没有它，A3 路径在界面上无法使用。
  const [candidate, setCandidate] = useState('')

  if (!demandId) {
    return (
      <div className="page">
        <Card title="让系统写出 SQL">
          <p className="muted">还没有需求。请先到「提个需求」页提交一条。</p>
        </Card>
      </div>
    )
  }

  /**
   * 真正跑一次生成。
   * 当前数据集还没有分析轮次时，先补跑一轮分析（摄入最新知识库/语义层），
   * 否则确定性 planner 会因「读不到已建模意图」直接拒。
   */
  const runGenerate = async () => {
    if (!status?.analysis) {
      await api.analyze(demandId, dataset, actor)
    }
    const text = candidate.trim()
    const r = await api.sqlGenerate(demandId, dataset, text || undefined)
    setSql(r)
  }

  const gen = async () => {
    setBusy('gen')
    setError(null)
    setExec(null)
    setRealignFeedback(null)
    try {
      await runGenerate()
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  /**
   * 「重新对齐」：手工补写知识后，先重跑分析把新知识摄入进当前数据集的轮次，
   * 再重新生成 SQL。此前只重跑 sqlGenerate、不重跑 analyze，导致新知识永不被摄入，
   * 且没有任何反馈，用户感知「点了没反应」（2026-10-22 修复）。返回 Promise 供
   * GapFiller 显示加载与成功/失败提示。
   */
  const realign = async () => {
    setBusy('gen')
    setError(null)
    setExec(null)
    setRealignFeedback(null)
    try {
      await api.analyze(demandId, dataset, actor)
      await runGenerate()
      setRealignFeedback('✅ 已重新对齐：已在本数据集重新分析、摄入新知识，并重新生成 SQL')
      onRefresh()
    } catch (e) {
      setError(e)
      throw e
    } finally {
      setBusy(null)
    }
  }

  const run = async () => {
    setBusy('exec')
    setError(null)
    try {
      const text = sql?.revised_sql || sql?.sql_draft
      const r = await api.sqlExecute(demandId, dataset, text)
      setExec(r)
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const loadCites = async () => {
    setBusy('cit')
    setError(null)
    try {
      setCites(await api.citations(demandId))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const review = exec?.review || sql?.review
  const layers: GateLayer[] = review?.layers || []
  const cols: string[] = exec?.validation_result?.columns || []
  const rows: unknown[][] = exec?.sample_rows || []
  const runs: any[] = status?.runs?.items || []

  // 数据集展示名 + 另一个候选数据集（用于「选错库」引导）
  const DATASET_LABEL: Record<string, string> = {
    A: 'A 库 · 电商域',
    B: 'B 库 · 零售会员域',
  }
  const otherDataset = dataset === 'B' ? 'A' : 'B'
  const otherHasAnalysis = (status?.datasets_with_analysis || []).includes(otherDataset)
  const blocked = (sql?.generation_risks || []).length > 0 && !sql?.sql_draft && !sql?.revised_sql

  return (
    <div className="page">
      <Card
        title={`生成 SQL 与出数结果 · ${demandId}`}
        collapsible
        id="sql-result"
        extra={
          <>
            <button className="primary" onClick={gen} disabled={busy === 'gen'}>
              {busy === 'gen' ? '生成中…' : '生成 SQL'}
            </button>
            <button
              onClick={run}
              disabled={
                busy === 'exec' ||
                // 生成失败（无 SQL）时必须禁用 —— 否则会把空串送去执行，
                // 报出「未建模表/列」之类与真实原因无关的误导性错误
                (!sql && !exec) ||
                !(sql?.revised_sql || sql?.sql_draft)
              }
              title={
                sql?.revised_sql || sql?.sql_draft
                  ? undefined
                  : '还没有可执行的 SQL，请先成功生成'
              }
            >
              {busy === 'exec' ? '执行中…' : '执行取数'}
            </button>
            <button onClick={loadCites} disabled={busy === 'cit'}>
              {busy === 'cit' ? '加载中…' : '知识引用明细'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        <PartialErrorsBox errors={status?.partial_errors || []} />
        {realignFeedback ? (
          <div className="okbox">
            <RichText text={realignFeedback} />
          </div>
        ) : null}
        <div className="ds-current">
          当前数据集：<b>{DATASET_LABEL[dataset] || dataset}</b>
        </div>
        {status?.effective_dataset && status.effective_dataset !== dataset ? (
          <div className="warnbox">
            <div className="warnbox-title">数据集不一致提示</div>
            <div className="warnbox-hint">
              当前选中 <b>{dataset} 库</b>，但该需求此前是在 <b>{status.effective_dataset} 库</b> 分析的。
              若提示「口径未定义」，多半是选错了库——请到右上角「数据集」切到 {status.effective_dataset} 库后再生成。
            </div>
          </div>
        ) : null}
        {blocked && otherHasAnalysis ? (
          <div className="warnbox">
            <div className="warnbox-title">可能是选错了数据集</div>
            <div className="warnbox-hint">
              当前在 <b>{DATASET_LABEL[dataset] || dataset}</b> 生成被拦下（{sql!.generation_risks![0]}）。
              该需求也在 <b>{DATASET_LABEL[otherDataset] || otherDataset}</b> 分析过——若它实际属于
              {DATASET_LABEL[otherDataset] || otherDataset}，请到右上角「数据集」切到该库，再点下方「重新对齐」重新生成；
              系统在对应库重新分析摄入知识后会产出 SQL。
            </div>
          </div>
        ) : null}
        {busy === 'gen' ? <Loading tip="正在做取数计划与门禁校验…" /> : null}
        {busy === 'exec' ? <Loading tip="正在只读执行…" /> : null}

        {/* 生成侧的真实失败原因——此前被丢弃，用户只看到「还没有生成 SQL」 */}
        {(sql?.generation_risks || []).length > 0 && !sql?.sql_draft && !sql?.revised_sql ? (
          <GapFiller
            risks={sql!.generation_risks || []}
            block={(sql as any).generation_block}
            dataset={dataset}
            onRetried={realign}
          />
        ) : null}

        {!sql && !exec ? (
          <>
          <p className="muted">
            点右上角「生成 SQL」即可生成。若有标红的未确认口径，会先被拦下——到「对一对关键说法」答复完再来。
          </p>
            <details className="candbox">
              <summary>自动生成没命中？可在此粘贴分析人员提供的候选 SQL</summary>
              <p className="muted">
                自动生成只认<b>已建模的口径</b>（如门店维度销售额 / 坪效）。涉及尚未建模的维度（如会员活跃度）会「未命中即拒」；
                此时可由分析人员按口径写好 SQL 粘到这里，提交后照常过校验。
              </p>
              <textarea
                className="candinput"
                rows={6}
                placeholder={`SELECT ...\nFROM ...\nWHERE ...`}
                value={candidate}
                onChange={(e) => setCandidate(e.target.value)}
              />
              <p className="muted">粘贴后点「生成 SQL」，会照常做安全与口径校验。</p>
            </details>
          </>
        ) : null}

        {sql?.sql_draft || sql?.revised_sql ? (
          <Section title="生成的 SQL" id="sql-gen">
            <pre className="sqlbox">{sql.revised_sql || sql.sql_draft}</pre>
            <details className="techfold">
              <summary>技术细节（生成器与生成注记）</summary>
              {sql.generator ? <p className="muted">生成器：{genLabel(sql.generator)}</p> : null}
              {(sql.generation_notes || []).length > 0 ? (
                <ul className="notes">
                  {sql.generation_notes!.map((n, i) => (
                    <li key={i}><RichText text={String(n)} /></li>
                  ))}
                </ul>
              ) : null}
            </details>
          </Section>
        ) : null}

        {layers.length > 0 ? (
          <Section title="门禁逐层结果" id="sql-gate" count={layers.length} desc={`总状态：${review?.status ?? '-'}（从语法到交付逐层把关，任意一层未通过都不会出数）`}>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>门禁层</th>
                    <th>结果</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {layers.map((l, i) => (
                    <tr key={i} className={l.ok ? 'row-ok' : 'row-bad'}>
                      <td>
                        <div className="gate-layer">
                          <span className="gate-layer-name">{GATE_LAYER_ZH[l.layer] || l.name}</span>
                          <span className="gate-layer-code mono">{l.layer}</span>
                        </div>
                      </td>
                      <td>
                        {l.skipped ? (
                          <span className="chip chip-muted">跳过</span>
                        ) : l.ok ? (
                          <span className="chip chip-green">通过</span>
                        ) : (
                          <span className="chip chip-red">未通过</span>
                        )}
                      </td>
                      <td><RichText text={String(l.detail)} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        ) : null}

        {exec ? (
          <Section title="结果集" id="sql-resultset">
            <p className="muted">{exec.execution_summary}</p>
            <p>
              交付判定：
              {exec.deliverable ? (
                <span className="chip chip-green">可交付</span>
              ) : (
                <span className="chip chip-red">不可交付</span>
              )}
              <span className="muted"> <RichText text={String(exec.deliverable_reason)} /></span>
            </p>
            {cols.length > 0 ? (
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      {cols.map((c, i) => (
                        <th key={i}>{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr key={i}>
                        {r.map((c, j) => (
                          <td key={j}>{String(c ?? '')}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">无样本行。</p>
            )}
            {(exec.suspicious_signals || []).length > 0 ? (
              <div className="warnbox">
                <div className="warnbox-title">结果存在可疑信号</div>
                <pre className="sqlbox small">{JSON.stringify(exec.suspicious_signals, null, 2)}</pre>
              </div>
            ) : null}
          </Section>
        ) : null}

        <Section title="取数溯源" id="sql-trace">
          <div className="trace">
            <div>
              <span className="trace-k">demand_id</span>
              <code>{demandId}</code>
            </div>
            <div>
              <span className="trace-k">sql_run_id</span>
              <code>{exec?.sql_run_id || runs[0]?.sql_run_id || '-'}</code>
            </div>
            <div>
              <span className="trace-k">knowledge_citations</span>
              <code>{cites?.total ?? runs[0]?.citation_count ?? '-'}</code>
            </div>
          </div>
        </Section>

        {runs.length > 0 ? (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>sql_run_id</th>
                  <th>生成器</th>
                  <th>门禁</th>
                  <th>需求版本</th>
                  <th>生成时间</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.sql_run_id}>
                    <td className="mono">{r.sql_run_id}</td>
                    <td>{genLabel(r.generator_model)}</td>
                    <td>{r.review_status}</td>
                    <td>{fmtReqVer(r.requirement_version)}</td>
                    <td className="mono">{r.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {cites ? (
          <Section title="知识引用明细" id="sql-cites" count={cites.total ?? '-'}>
            {cites.degraded_reason ? (
              <div className="warnbox">
                <div className="warnbox-title">注意：这份清单是降级结果</div>
                <div className="warnbox-hint"><RichText text={String(cites.degraded_reason)} /></div>
              </div>
            ) : null}
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>文档</th>
                    <th>相似度</th>
                    <th>片段</th>
                  </tr>
                </thead>
                <tbody>
                  {(cites.items || []).map((c: any, i: number) => (
                    <tr key={i}>
                      <td>{c.n}</td>
                      <td>{c.document_name}</td>
                      <td>{c.similarity}</td>
                      <td className="snippet"><RichText text={c.snippet} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        ) : null}
      </Card>
    </div>
  )
}