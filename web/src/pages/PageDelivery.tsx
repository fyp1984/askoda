import { useState } from 'react'
import { api, type E2eStatus, type SqlExecResp, type SqlResp } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'
import DemandStatusActions from '../components/DemandStatusActions'
import AuditReplay from '../components/AuditReplay'

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

  // ---- 结果可视化：找「一个文本标签列 + 一个数值列」画柱状图 ----
  const numericIdx = (() => {
    for (let j = 0; j < cols.length; j++) {
      const vals = rows.map((r) => r[j])
      if (vals.length === 0) continue
      const ok = vals.every((v) => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v)))
      if (ok) return j
    }
    return -1
  })()
  const labelIdx = numericIdx >= 0 ? cols.findIndex((_, j) => j !== numericIdx) : -1
  const canChart = numericIdx >= 0 && labelIdx >= 0 && rows.length > 0

  const chartData = canChart
    ? rows.map((r) => ({ label: String(r[labelIdx] ?? ''), value: Number(r[numericIdx] ?? 0) }))
    : []
  const maxVal = Math.max(...chartData.map((d) => Math.abs(d.value)), 1)

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
              <span className="muted"> {exec.deliverable_reason}</span>
            </p>
          </>
        ) : null}
      </Card>

      {exec && canChart ? (
        <Card title={`结果可视化 · ${cols[numericIdx]}（按 ${cols[labelIdx]}）`}>
          <BarChart data={chartData} max={maxVal} unit={cols[numericIdx]} />
        </Card>
      ) : null}

      {exec ? (
        <Card title="结果集">
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

/** 极简柱状图：纯 SVG，不引第三方图表库（避免新增运行时依赖）。 */
function BarChart({ data, max, unit }: { data: Array<{ label: string; value: number }>; max: number; unit: string }) {
  const W = 640
  const H = 300
  const padL = 56
  const padB = 48
  const padT = 24
  const plotW = W - padL - 24
  const plotH = H - padT - padB
  const n = data.length || 1
  const slot = plotW / n
  const barW = Math.min(80, slot * 0.6)
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max)

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img">
      <rect x="0" y="0" width={W} height={H} fill="#ffffff" />
      <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke="#888888" strokeWidth="1.5" />
      <line x1={padL} y1={padT + plotH} x2={W - 24} y2={padT + plotH} stroke="#cccccc" strokeWidth="1" />
      {ticks.map((t, i) => {
        const y = padT + plotH - (t / (max || 1)) * plotH
        return (
          <g key={i}>
            <line x1={padL} y1={y} x2={W - 24} y2={y} stroke="#eeeeee" strokeWidth="1" />
            <text x={padL - 8} y={y + 4} fontSize="11" fill="#666666" textAnchor="end">
              {Math.round(t * 100) / 100}
            </text>
          </g>
        )
      })}
      {data.map((d, i) => {
        const h = (Math.abs(d.value) / (max || 1)) * plotH
        const x = padL + slot * i + (slot - barW) / 2
        const y = padT + plotH - h
        return (
          <g key={i}>
            <rect x={x} y={y} width={barW} height={h} fill="#2f6fb0" />
            <text x={x + barW / 2} y={y - 6} fontSize="12" fontWeight="700" fill="#1a1a1a" textAnchor="middle">
              {d.value}
            </text>
            <text x={x + barW / 2} y={padT + plotH + 20} fontSize="12" fill="#333333" textAnchor="middle">
              {d.label}
            </text>
          </g>
        )
      })}
      <text x={16} y={padT + plotH / 2} fontSize="12" fill="#666666" textAnchor="middle" transform={`rotate(-90 16 ${padT + plotH / 2})`}>
        {unit}
      </text>
    </svg>
  )
}
