import { useCallback, useEffect, useState } from 'react'
import {
  api,
  type ReviewDetailResp,
  type ReviewQueueResp,
  type ReviewTaskItem,
} from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import { Section } from '../components/Collapsible'
import RichText from '../components/RichText'

/**
 * 菜单 · 人工复核（FR-6.7 / AC-13 / D15）—— 质量内核
 *
 * 铁律：**全量人工复核，不看置信度分流**
 * ------------------------------------------------------------------
 * 每一条生成的 SQL 都进这个队列，系统只出「快捷草稿」。
 * 置信度是给复核人**排序用的参考**，不是免复核开关：
 *   · 高置信度 ≠ 免复核；
 *   · 未经人工在真实环境预测试确认，**不得**标注「可执行」。
 * 复核结论**只增不改**：旧结论留在历史里可回放。
 *
 * 与「门禁」的分工：门禁是**机器**判「SQL 写得对不对」；
 * 复核是**人**判「这条 SQL 是不是业务真要的那一条」。两者都过，才叫可交付。
 */

const STATUS_OPTIONS = ['', '待复核', '已通过', '已退回', '已改写']

/** 状态 → chip 样式 */
function statusChip(status?: string) {
  if (status === '已通过') return 'chip chip-green'
  if (status === '已退回') return 'chip chip-red'
  if (status === '已改写') return 'chip chip-blue'
  return 'chip chip-amber'
}

function confidenceText(c: ReviewTaskItem['confidence']) {
  if (c === null || c === undefined || c === '') return '-'
  const n = typeof c === 'string' ? Number(c) : c
  if (Number.isNaN(n)) return String(c)
  return `${Math.round(n * 100)}%`
}

export default function PageReview({
  actor,
  readonly,
  onNotify,
  onOpenDemand,
}: {
  actor: string
  readonly: boolean
  onNotify: (msg: string) => void
  /** 跳到该需求的「需求链路」——避免复核页成为死胡同 */
  onOpenDemand?: (demandId: string) => void
}) {
  const [queue, setQueue] = useState<ReviewQueueResp | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [datasetFilter, setDatasetFilter] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const [selected, setSelected] = useState<ReviewTaskItem | null>(null)
  const [detail, setDetail] = useState<ReviewDetailResp | null>(null)
  const [detailBusy, setDetailBusy] = useState(false)

  // 结论表单
  const [decision, setDecision] = useState('通过')
  const [reviewer, setReviewer] = useState(actor)
  const [note, setNote] = useState('')
  const [revisedSql, setRevisedSql] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<unknown>(null)

  const load = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      setQueue(
        await api.reviewQueue({
          status: statusFilter,
          dataset: datasetFilter,
          limit: 50,
        }),
      )
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [statusFilter, datasetFilter])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setReviewer((r) => r || actor)
  }, [actor])

  const openTask = async (t: ReviewTaskItem) => {
    setSelected(t)
    setDetail(null)
    setDecision('通过')
    setNote('')
    setRevisedSql('')
    setFormError(null)
    setDetailBusy(true)
    try {
      const d = await api.reviewDetail(t.demand_id)
      setDetail(d)
      const fresh = (d.items || []).find((x) => x.task_id === t.task_id)
      if (fresh) setSelected(fresh)
    } catch (e) {
      setFormError(e)
    } finally {
      setDetailBusy(false)
    }
  }

  const submit = async () => {
    if (!selected) return
    setSubmitting(true)
    setFormError(null)
    try {
      await api.reviewDecide(selected.task_id, {
        decision,
        reviewer,
        note: note.trim(),
        rewritten_sql: decision === '改写' ? revisedSql.trim() : '',
      })
      onNotify(`已登记复核结论：${decision}（${selected.task_id}）`)
      setSelected(null)
      setDetail(null)
      await load()
    } catch (e) {
      setFormError(e)
    } finally {
      setSubmitting(false)
    }
  }

  const items = queue?.items || []
  const policy = queue?.policy
  const needNote = decision === '退回' || decision === '改写'
  const needSql = decision === '改写'
  const canSubmit =
    !readonly &&
    !!selected &&
    !!reviewer.trim() &&
    (!needNote || !!note.trim()) &&
    (!needSql || !!revisedSql.trim()) &&
    !submitting

  return (
    <div className="page">
      <Card
        title="人工复核 · 质量内核"
        collapsible
        id="review-main"
        extra={
          <button type="button" onClick={load} disabled={busy}>
            {busy ? '刷新中…' : '刷新队列'}
          </button>
        }
      >
        {policy ? (
          <div className="review-policy" role="note">
            <div className="review-policy-title">{policy.full_review}（不看置信度分流）</div>
            <div className="review-policy-body"><RichText text={policy.note} /></div>
          </div>
        ) : null}
        <ErrorBox error={error} />

        <div className="review-filters">
          <label>
            <span className="muted">状态</span>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s || '全部'}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="muted">数据集</span>
            <select value={datasetFilter} onChange={(e) => setDatasetFilter(e.target.value)}>
              <option value="">全部</option>
              <option value="A">A 库</option>
              <option value="B">B 库</option>
            </select>
          </label>
          <span className="review-count">
            共 <b>{queue?.total ?? 0}</b> 条，其中 <b>{queue?.pending ?? 0}</b> 条待复核
          </span>
        </div>

        {busy && !queue ? <Loading tip="正在读取复核队列…" /> : null}

        {items.length === 0 && !busy ? (
          <p className="muted">
            队列为空。到「需求链路」生成并执行 SQL 后，每条都会自动进入这里——
            全量入队，不需要你手动挑。
          </p>
        ) : (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>需求</th>
                  <th>数据集</th>
                  <th>生成器</th>
                  <th>置信度（仅排序参考）</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.task_id} className={selected?.task_id === t.task_id ? 'row-ok' : undefined}>
                    <td className="mono">{t.task_id}</td>
                    <td className="mono">
                      {onOpenDemand ? (
                        <button type="button" className="linklike" onClick={() => onOpenDemand(t.demand_id)}>
                          {t.demand_id}
                        </button>
                      ) : (
                        t.demand_id
                      )}
                    </td>
                    <td>{t.dataset}</td>
                    <td>{t.generator || '-'}</td>
                    <td>{confidenceText(t.confidence)}</td>
                    <td>
                      <span className={statusChip(t.status)}>{t.status}</span>
                    </td>
                    <td className="mono">{t.created_at}</td>
                    <td>
                      <button type="button" className="linklike" onClick={() => openTask(t)}>
                        复核
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {selected ? (
        <Card
          title={`复核任务 · ${selected.task_id}`}
          collapsible
          id="review-detail"
          extra={
            <button type="button" onClick={() => setSelected(null)}>
              关闭
            </button>
          }
        >
          {detailBusy ? <Loading tip="正在读取任务明细与结论历史…" /> : null}
          <ErrorBox error={formError} />

          <div className="review-meta">
            <span>需求：<code>{selected.demand_id}</code></span>
            <span>运行：<code>{selected.sql_run_id}</code></span>
            <span>当前状态：<span className={statusChip(selected.status)}>{selected.status}</span></span>
          </div>

          {selected.sql_text ? (
            <Section title="待复核 SQL" id="review-sql">
              <pre className="sqlbox small">{selected.sql_text}</pre>
            </Section>
          ) : null}

          {(detail?.items?.[0]?.decisions || selected.decisions || []).length > 0 ? (
            <Section
              title="结论历史（只增不改）"
              id="review-history"
              count={(detail?.items?.find((x) => x.task_id === selected.task_id)?.decisions || []).length}
            >
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      <th>结论</th>
                      <th>复核人</th>
                      <th>理由</th>
                      <th>改写后 SQL</th>
                      <th>时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail?.items?.find((x) => x.task_id === selected.task_id)?.decisions || []).map((d) => (
                      <tr key={d.decision_id}>
                        <td><span className={statusChip(d.decision === '通过' ? '已通过' : d.decision === '退回' ? '已退回' : '已改写')}>{d.decision}</span></td>
                        <td>{d.reviewer}</td>
                        <td><RichText text={d.note || '-'} /></td>
                        <td className="mono">{d.rewritten_sql ? `${d.rewritten_sql.slice(0, 60)}…` : '-'}</td>
                        <td className="mono">{d.created_at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          ) : null}

          <Section title="登记复核结论" id="review-decide">
            {readonly ? (
              <p className="muted">当前身份对该模块只读，不能登记复核结论。</p>
            ) : (
              <>
                <div className="review-form">
                  <label>
                    <span>结论</span>
                    <select value={decision} onChange={(e) => setDecision(e.target.value)}>
                      <option value="通过">通过（可执行）</option>
                      <option value="退回">退回（重做）</option>
                      <option value="改写">改写（给修正后的 SQL）</option>
                    </select>
                  </label>
                  <label>
                    <span>复核人（必填，留痕到人）</span>
                    <input
                      value={reviewer}
                      onChange={(e) => setReviewer(e.target.value)}
                      placeholder="如 R2-分析师"
                    />
                  </label>
                </div>
                <label className="review-form-block">
                  <span>理由{needNote ? '（必填）' : '（可选）'}</span>
                  <textarea
                    rows={2}
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder={needNote ? '说明为什么退回 / 改写，便于复盘' : '可补充说明'}
                  />
                </label>
                {needSql ? (
                  <label className="review-form-block">
                    <span>改写后的 SQL（必填）</span>
                    <textarea
                      className="sqlinput"
                      rows={6}
                      value={revisedSql}
                      onChange={(e) => setRevisedSql(e.target.value)}
                      placeholder="SELECT ..."
                    />
                  </label>
                ) : null}
                <div className="review-actions">
                  <button type="button" className="primary" onClick={submit} disabled={!canSubmit}>
                    {submitting ? '提交中…' : '登记结论'}
                  </button>
                  <span className="muted">
                    通过 = 人工在真实环境预测试确认可用；未确认前，该 SQL 不得标注「可执行」。
                  </span>
                </div>
              </>
            )}
          </Section>
        </Card>
      ) : null}
    </div>
  )
}
