import { useState } from 'react'
import { api, type E2eStatus, type SqlExecResp, type SqlResp } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'
import RichText from '../components/RichText'
import DemandStatusActions from '../components/DemandStatusActions'
import AuditReplay from '../components/AuditReplay'
import ResultChart from '../components/ResultChart'

/**
 * 菜单⑤ · SQL 联调 · 交付
 *
 * PRD §12.7：测试库联调、迭代记录、结果可视化核查、生产脚本导出。
 * 关键交互：联调通过且门禁全过后才可导出；导出脚本自带口径依据与门禁记录头注。
 */
export default function PageDelivery({
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
  const [candidate, setCandidate] = useState('')
  const [sql, setSql] = useState<SqlResp | null>(null)
  const [exec, setExec] = useState<SqlExecResp | null>(null)
  const [busy, setBusy] = useState<'gen' | 'exec' | null>(null)
  const [error, setError] = useState<unknown>(null)
  // 结果区视图：图表 / 表格。部分用户习惯直接看数字，所以两个视图都保留，且可一键切换。
  const [view, setView] = useState<'chart' | 'table'>('chart')

  if (!demandId) {
    return (
      <div className="page">
        <Card title="SQL 联调 · 交付">
          <p className="muted">还没有需求。请先到「需求分析 · SQL 生成」提交一条需求。</p>
        </Card>
      </div>
    )
  }

  const sqlText = sql?.revised_sql || sql?.sql_draft || ''

  const gen = async () => {
    setBusy('gen')
    setError(null)
    try {
      const r = await api.sqlGenerate(demandId, dataset, candidate.trim() || undefined)
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
      const r = await api.sqlExecute(demandId, dataset, sqlText || candidate.trim() || undefined)
      setExec(r)
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const cols: string[] = exec?.validation_result?.columns || []
  const rows: unknown[][] = exec?.sample_rows || []
  const runs: any[] = status?.runs?.items || []

  // 后端只回传前若干行样本（实测真实 9 行时 sample_rows 仅 5 行）。
  // 这里如实告知用户「图上画的是样本，不是全量」，不谎称已展示全部数据。
  const totalRows = exec?.row_count ?? exec?.validation_result?.row_count ?? rows.length
  const truncated = rows.length > 0 && totalRows > rows.length

  // ---- 生产脚本导出（头注：口径依据 + 门禁记录 + 溯源三件套）----
  const buildExport = () => {
    const layers = (exec?.review?.layers || sql?.review?.layers || []) as any[]
    const gateLines = layers.map(
      (l) => `--   ${l.layer} ${l.name}: ${l.skipped ? '跳过' : l.ok ? '通过' : '未通过'} — ${l.detail}`,
    )
    const ctx: any = status?.context_pack || {}
    const analysis: any = status?.analysis || {}
    const head = [
      '-- ============================================================',
      '-- Askoda 生产脚本导出',
      `-- demand_id      : ${demandId}`,
      `-- dataset        : ${dataset}`,
      `-- sql_run_id     : ${exec?.sql_run_id || '-'}`,
      `-- schema_version : ${exec?.schema_version || ctx?.schema_version || '-'}`,
      `-- requirement_ver: ${exec?.requirement_version ?? '-'}`,
      `-- pack_version   : ${exec?.pack_version || ctx?.pack_version || '-'}`,
      `-- 导出时间       : ${new Date().toISOString()}`,
      '--',
      '-- 门禁记录（五层）：',
      ...(gateLines.length > 0 ? gateLines : ['--   （无门禁记录）']),
      `-- 门禁总状态       : ${exec?.review?.status || sql?.review?.status || '-'}`,
      '--',
      '-- 口径依据：',
      `--   需求标题  : ${status?.demand?.title || '-'}`,
      `--   需求说明  : ${status?.demand?.description || '-'}`,
      `--   语义置信  : ${analysis?.confidence_overall ?? '-'}`,
      '--',
      '-- 注意：本脚本由受控链路生成并通过门禁，导出后请在生产库复核后再执行。',
      '-- ============================================================',
      '',
    ].join('\n')
    const body = (sqlText || candidate.trim() || '-- （暂无 SQL）').trim()
    return `${head}${body}\n`
  }

  const download = () => {
    const text = buildExport()
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${demandId}-delivery.sql`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="page">
      <Card
        title={`SQL 联调 · 交付 · ${demandId}`}
        extra={
          <>
            <button onClick={gen} disabled={busy === 'gen'}>
              {busy === 'gen' ? '生成中…' : '生成 SQL'}
            </button>
            <button className="primary" onClick={run} disabled={busy === 'exec' || (!sqlText && !candidate.trim())}>
              {busy === 'exec' ? '执行中…' : '执行取数'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        <PartialErrorsBox errors={status?.partial_errors || []} />
        {busy === 'gen' ? <Loading tip="正在做取数计划与门禁校验…" /> : null}
        {busy === 'exec' ? <Loading tip="正在只读执行…" /> : null}

        <p className="muted">
          网关不调用模型：未命中已建模的意图会被硬门禁拒绝（这是设计使然）。
          此时可在下方填入候选 SQL，由本页提交给网关过五层门禁后执行。
        </p>
        <textarea
          className="sqlinput"
          rows={4}
          placeholder="（可选）粘贴候选 SQL，留空则由内置确定性规划器尝试生成"
          value={candidate}
          onChange={(e) => setCandidate(e.target.value)}
        />

        {sqlText ? (
          <>
            <h4 className="sec">待执行 SQL</h4>
            <pre className="sqlbox">{sqlText}</pre>
          </>
        ) : null}

        {exec ? (
          <>
            <h4 className="sec">执行结果</h4>
            <p className="muted">{exec.execution_summary}</p>
            <p>
              交付判定：
              {exec.deliverable ? (
                <span className="chip chip-green">可交付</span>
              ) : (
                <span className="chip chip-red">不可交付</span>
              )}
              <span className="muted"> <RichText text={exec.deliverable_reason} /></span>
            </p>
          </>
        ) : null}
      </Card>

      {exec ? (
        <Card
          title={`执行结果 · ${rows.length} 行样本 × ${cols.length} 列`}
          extra={
            <div className="seg" role="group" aria-label="结果视图切换">
              <button className={view === 'chart' ? 'seg-btn on' : 'seg-btn'} onClick={() => setView('chart')}>
                图表
              </button>
              <button className={view === 'table' ? 'seg-btn on' : 'seg-btn'} onClick={() => setView('table')}>
                表格
              </button>
            </div>
          }
        >
          {cols.length === 0 ? (
            <p className="muted">无样本行。</p>
          ) : view === 'chart' ? (
            <>
              <ResultChart columns={cols} rows={rows} />
              {truncated ? (
                <p className="notice">
                  本次共返回 {totalRows} 行，图表基于其中 {rows.length} 行样本绘制（未聚合、未补齐）；如需核对全部数字，请切到「表格」。
                </p>
              ) : null}
            </>
          ) : (
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
          )}
        </Card>
      ) : null}

      <Card
        title="生产脚本导出"
        extra={
          <button onClick={download} disabled={!sqlText && !candidate.trim()}>
            下载 .sql
          </button>
        }
      >
        <p className="muted">
          导出脚本自带口径依据、五层门禁记录与溯源版本号，便于在生产库复核后执行。
        </p>
        <details className="fold">
          <summary>预览导出内容</summary>
          <pre className="sqlbox small">{buildExport()}</pre>
        </details>
      </Card>

      <Card title="迭代记录">
        {runs.length === 0 ? (
          <p className="muted">暂无取数记录。</p>
        ) : (
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
        )}
      </Card>

      {/* 审计回放：把「迭代记录」那张留痕表还原成可对照复算的完整判断链 */}
      <AuditReplay demandId={demandId} />

      {/* 取到数之后的下一步：把需求收口到终态「已通过」，或退回让业务方改 */}
      <DemandStatusActions
        demandId={demandId}
        currentStatus={status?.demand?.status}
        onDone={onRefresh}
      />
    </div>
  )
}