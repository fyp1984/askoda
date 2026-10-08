import { useState } from 'react'
import { api, type E2eStatus, type GateLayer, type SqlExecResp, type SqlResp } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'
import RichText from '../components/RichText'
import GapFiller from '../components/GapFiller'

/**
 * 页4 · 看 SQL 与结果
 * SQL + 五层门禁逐层结果 + 结果集前 N 行+ 溯源三件套 + 知识引用明细。
 */
export default function PageSql({
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
  const [sql, setSql] = useState<SqlResp | null>(null)
  const [exec, setExec] = useState<SqlExecResp | null>(null)
  const [cites, setCites] = useState<{ source?: string; items?: any[]; total?: number; degraded_reason?: string } | null>(null)
  const [busy, setBusy] = useState<'gen' | 'exec' | 'cit' | null>(null)
  const [error, setError] = useState<unknown>(null)
  // 候选 SQL（Agent/技术人员手工提供）：走 candidate_sql 通道，
  // 绕过确定性兜底 planner（planner 只认已建模意图，未命中即拒）。
  // 网关侧 contract 一直是支持的（gateway/app.py: sql_generate 的 candidate_sql 参数），
  // 缺的是界面入口 —— 没有它，A3 路径在界面上无法使用。
  const [candidate, setCandidate] = useState('')

  if (!demandId) {
    return (
      <div className="page">
        <Card title="看 SQL 与结果">
          <p className="muted">还没有需求。请先到「提交需求」页提交一条。</p>
        </Card>
      </div>
    )
  }

  const gen = async () => {
    setBusy('gen')
    setError(null)
    setExec(null)
    try {
      const text = candidate.trim()
      const r = await api.sqlGenerate(demandId, dataset, text || undefined)
      setSql(r)
      onRefresh()
    } catch (e) {
      setError(e)
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

  return (
    <div className="page">
      <Card
        title={`SQL 与结果 · ${demandId}`}
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
        {busy === 'gen' ? <Loading tip="正在做取数计划与门禁校验…" /> : null}
        {busy === 'exec' ? <Loading tip="正在只读执行…" /> : null}

        {/* 生成侧的真实失败原因——此前被丢弃，用户只看到「还没有生成 SQL」 */}
        {(sql?.generation_risks || []).length > 0 && !sql?.sql_draft && !sql?.revised_sql ? (
          <GapFiller
            risks={sql!.generation_risks || []}
            block={(sql as any).generation_block}
            dataset={dataset}
            onRetried={() => gen()}
          />
        ) : null}

        {!sql && !exec ? (
          <>
            <p className="muted">
              还没有生成 SQL。点右上角「生成 SQL」。
              注意：若存在标红（阻断）的未确认口径，会被硬门禁挡下——先去「口径确认」页答复。
            </p>
            <details className="candbox">
              <summary>自动生成失败？在这里粘贴候选 SQL（走 Agent 路径）</summary>
              <p className="muted">
                确定性兜底只认<b>已建模的口径</b>（如门店维度销售额/坪效）。若你的需求涉及尚未建模的维度
                （如会员活跃度），自动生成会「未命中即拒」。此时可由分析人员/Agent
                按知识库口径写出 SQL 粘贴到此处，走 <code>candidate_sql</code> 通道——
                网关与BFF 均已支持该参数，只是此前界面没有入口。
              </p>
              <textarea
                className="candinput"
                rows={6}
                placeholder={`SELECT ...\nFROM ...\nWHERE ...`}
                value={candidate}
                onChange={(e) => setCandidate(e.target.value)}
              />
              <p className="muted">粘贴后点右上角「生成 SQL」，门禁会照常做语法/静态/只读/语义四层校验。</p>
            </details>
          </>
        ) : null}

        {sql?.sql_draft || sql?.revised_sql ? (
          <>
            <h4 className="sec">生成的 SQL</h4>
            <pre className="sqlbox">{sql.revised_sql || sql.sql_draft}</pre>
            {sql.generator ? <p className="muted">生成器：{sql.generator}</p> : null}
            {(sql.generation_notes || []).length > 0 ? (
              <ul className="notes">
                  {sql.generation_notes!.map((n, i) => (
                    <li key={i}><RichText text={String(n)} /></li>
                  ))}
              </ul>
            ) : null}
          </>
        ) : null}

        {layers.length > 0 ? (
          <>
            <h4 className="sec">
              门禁逐层结果 <span className="muted">（总状态：{review?.status}）</span>
            </h4>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>层</th>
                    <th>名称</th>
                    <th>结果</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {layers.map((l, i) => (
                    <tr key={i} className={l.ok ? 'row-ok' : 'row-bad'}>
                      <td className="mono">{l.layer}</td>
                      <td>{l.name}</td>
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
          </>
        ) : null}

        {exec ? (
          <>
            <h4 className="sec">结果集</h4>
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
          </>
        ) : null}

        <h4 className="sec">溯源三件套</h4>
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
                    <td>{r.generator_model}</td>
                    <td>{r.review_status}</td>
                    <td>v{r.requirement_version}</td>
                    <td className="mono">{r.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {cites ? (
          <>
            <h4 className="sec">
              知识引用明细
              {cites.source === 'bff-degraded' ? <span className="chip chip-muted">降级来源</span> : null}
            </h4>
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
          </>
        ) : null}
      </Card>
    </div>
  )
}