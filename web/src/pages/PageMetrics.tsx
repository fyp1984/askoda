import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  ApiError,
  type MetricDef,
  type MetricPreviewResp,
  type MetricsResp,
  type MetricView,
} from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import RichText from '../components/RichText'

/**
 * 菜单 · 口径维护
 *
 * 为什么要有这一页
 * ---------------
 * 「不良率怎么算」「放款金额含不含冲正」这类口径，以前写在文件里：改一条要改 JSON、
 * 跑脚本、重建镜像。结果是业务侧最想改的东西，长期停在「没人敢改」。
 *
 * 这一页做三件事，且只有三件事：
 *   1. 看得见 —— 每条口径的定义、别名、时间语义、依赖的视图与列；
 *   2. 改得动 —— 就地编辑，点保存当场生效（校验不过会逐条说明原因）；
 *   3. 试得出 —— 写一句业务话术，立刻看到「命中哪条口径、生成什么 SQL、返回几行」。
 *
 * 界面原则：**口径是数据不是代码**。改口径不该需要发版，也不该绕过门禁——
 * 保存时用的判据与门禁是同一份实现，所以「页面能存、门禁判红」不该发生。
 */

type FormState = MetricDef

const EMPTY: FormState = {
  key: '',
  label: '',
  aliases: [],
  view: '',
  expr: '',
  unit: '',
  fmt: 'num',
  time_mode: 'snapshot',
  time_col: '',
  dims: [],
  definition: '',
  caveats: '',
  source: '',
}

/** 逗号（中英文都算）分隔的多个值 ⇄ 数组 */
const toText = (arr?: string[]) => (arr || []).join('，')
const toList = (s: string) =>
  s
    .replace(/[,，、]/g, ',')
    .split(',')
    .map((x) => x.trim())
    .filter((x, i, a) => x && a.indexOf(x) === i)

export default function PageMetrics({
  dataset,
  actor,
  readonly,
  onNotify,
}: {
  dataset: string
  actor: string
  readonly?: boolean
  onNotify?: (msg: string) => void
}) {
  const [data, setData] = useState<MetricsResp | null>(null)
  const [metrics, setMetrics] = useState<MetricDef[]>([])
  const [views, setViews] = useState<MetricView[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [problems, setProblems] = useState<string[]>([])
  const [okMsg, setOkMsg] = useState('')

  // 编辑器：editing 为 null 表示没在编辑；'' 表示新增
  const [editing, setEditing] = useState<string | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY)

  // 试跑
  const [question, setQuestion] = useState('')
  const [preview, setPreview] = useState<MetricPreviewResp | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setProblems([])
    setOkMsg('')
    try {
      const r = await api.metrics(dataset)
      setData(r)
      setMetrics(r.metrics || [])
      setViews(r.views || [])
      setEditing(null)
      // 试跑默认给一句最典型的问法：让用户不必先想「怎么问」才敢按按钮
      if (!question) setQuestion(r.metrics?.[0]?.aliases?.[0] ? `${r.metrics[0].aliases[0]}是多少` : '')
    } catch (e) {
      setError(e)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [dataset, question])

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset])

  const viewCols = useMemo(() => {
    const m: Record<string, string[]> = {}
    for (const v of views) m[v.name] = v.columns || []
    return m
  }, [views])

  const startEdit = (m?: MetricDef) => {
    setEditing(m ? m.key : '')
    setForm(m ? { ...m, aliases: [...(m.aliases || [])], dims: [...(m.dims || [])] } : {
      ...EMPTY,
      view: views[0]?.name || '',
      time_col: viewCols[views[0]?.name || '']?.[0] || '',
    })
    setProblems([])
    setOkMsg('')
  }

  const commitEdit = () => {
    const key = form.key.trim()
    const label = form.label.trim() || key
    if (!key) {
      setProblems(['口径编码（key）不能为空'])
      return
    }
    if (!form.view || !form.expr.trim()) {
      setProblems(['口径依赖的视图与表达式不能为空'])
      return
    }
    const item: MetricDef = {
      ...form,
      key,
      label,
      aliases: toList(toText(form.aliases)),
      dims: toList(toText(form.dims)),
    }
    setMetrics((prev) => {
      const i = prev.findIndex((x) => x.key === (editing || ''))
      if (editing === '') {
        if (prev.some((x) => x.key === key)) return prev
        return [...prev, item]
      }
      const next = [...prev]
      if (i >= 0) next[i] = item
      return next
    })
    setEditing(null)
    setProblems([])
    setOkMsg('已加入草稿，点「保存并生效」后才会真正生效')
  }

  const removeOne = (key: string) => {
    setMetrics((prev) => prev.filter((m) => m.key !== key))
    if (editing === key) setEditing(null)
    setOkMsg(`已从草稿移除「${key}」，点「保存并生效」后生效`)
  }

  const submit = async (dryRun: boolean) => {
    setBusy(dryRun ? 'check' : 'save')
    setError(null)
    setProblems([])
    setOkMsg('')
    try {
      const r = await api.saveMetrics({
        dataset,
        metrics: { version: data?.version || '', views, metrics },
        dry_run: dryRun,
        actor,
      })
      setOkMsg(r.notice || (dryRun ? '校验通过' : '已保存并生效'))
      onNotify?.(dryRun ? '口径校验通过' : `口径已生效（${r.version}）`)
      if (!dryRun) await load()
    } catch (e) {
      if (e instanceof ApiError) {
        const p = (e.body?.problems as string[] | undefined) || []
        setProblems(p.length ? p : [e.hint || e.message])
      } else {
        setError(e)
      }
    } finally {
      setBusy('')
    }
  }

  const runPreview = async () => {
    if (!question.trim()) return
    setBusy('preview')
    setError(null)
    setPreview(null)
    try {
      setPreview(await api.previewMetric({ dataset, question: question.trim() }))
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const pick = (m: MetricDef) => {
    setQuestion(m.aliases?.[0] || m.label)
    setPreview(null)
  }

  return (
    <div className="page">
      <Card
        title="口径维护"
        collapsible
        id="metrics-list"
        extra={
          <>
            <button onClick={load} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
            <button onClick={() => submit(true)} disabled={!!busy || readonly}>
              {busy === 'check' ? '校验中…' : '先校验'}
            </button>
            <button className="primary" onClick={() => submit(false)} disabled={!!busy || readonly}>
              {busy === 'save' ? '保存中…' : '保存并生效'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        {loading ? <Loading tip="正在读取口径…" /> : null}

        <div className="trace">
          <div>
            <span className="trace-k">数据集</span>
            <code>{data?.dataset || dataset}</code>
          </div>
          <div>
            <span className="trace-k">版本</span>
            <code>{data?.version || '-'}</code>
          </div>
          <div>
            <span className="trace-k">口径数</span>
            <code>{metrics.length}</code>
          </div>
          <div>
            <span className="trace-k">视图数</span>
            <code>{views.length}</code>
          </div>
          <div>
            <span className="trace-k">最近修改</span>
            <code>{data?.updated_by || '-'}</code>
          </div>
        </div>

        {problems.length > 0 ? (
          <div className="errbox">
            <div className="errbox-title">口径校验未通过（{problems.length} 处）</div>
            <div className="errbox-hint">
              <ul className="plainlist">
                {problems.map((p) => (
                  <li key={p}>
                    <RichText text={p} />
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}
        {okMsg ? (
          <div className="warnbox">
            <div className="warnbox-hint">
              <RichText text={okMsg} />
            </div>
          </div>
        ) : null}

        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>口径</th>
                <th>业务说法（别名）</th>
                <th>表达式</th>
                <th>时间语义</th>
                <th>单位</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {metrics.map((m) => (
                <tr key={m.key} className={editing === m.key ? 'row-ok' : ''}>
                  <td>
                    <div>{m.label}</div>
                    <code className="mono">{m.key}</code>
                  </td>
                  <td className="snippet">{(m.aliases || []).join('、') || <span className="muted">未登记</span>}</td>
                  <td className="mono snippet">{m.expr}</td>
                  <td>
                    {m.time_mode === 'snapshot' ? '时点快照' : '区间流水'}
                    <div className="muted">{m.time_col}</div>
                  </td>
                  <td>{m.unit || '-'}</td>
                  <td>
                    <button type="button" className="linklike" onClick={() => pick(m)}>
                      试一句
                    </button>
                    {!readonly ? (
                      <>
                        <button type="button" className="linklike" onClick={() => startEdit(m)}>
                          编辑
                        </button>
                        <button type="button" className="linklike" onClick={() => removeOne(m.key)}>
                          移除
                        </button>
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {metrics.length === 0 && !loading ? (
          <p className="muted">这个数据集还没有登记任何口径。登记一条，规划器才认得出业务说法。</p>
        ) : null}

        {!readonly ? (
          <div className="pager">
            <button onClick={() => startEdit()} disabled={!views.length}>
              新增口径
            </button>
            {!views.length ? <span className="muted">先有口径视图，才能登记口径</span> : null}
          </div>
        ) : null}
      </Card>

      {editing !== null ? (
        <Card title={editing === '' ? '新增口径' : `编辑口径 · ${editing}`} id="metric-edit">
          <div className="formgrid">
            <label>
              <span>口径编码</span>
              <input
                value={form.key}
                disabled={editing !== ''}
                onChange={(e) => setForm({ ...form, key: e.target.value })}
                placeholder="loan_bal"
              />
            </label>
            <label>
              <span>中文名</span>
              <input value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="贷款余额" />
            </label>
            <label className="wide">
              <span>业务说法（逗号分隔，越常用越靠前）</span>
              <input
                value={toText(form.aliases)}
                onChange={(e) => setForm({ ...form, aliases: toList(e.target.value) })}
                placeholder="余额，贷款余额，贷款规模"
              />
            </label>
            <label>
              <span>依赖视图</span>
              <select
                value={form.view}
                onChange={(e) => {
                  const v = e.target.value
                  setForm({ ...form, view: v, time_col: (viewCols[v] || [])[0] || '', dims: [] })
                }}
              >
                <option value="">（选一个视图）</option>
                {views.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>时间语义</span>
              <select
                value={form.time_mode}
                onChange={(e) => setForm({ ...form, time_mode: e.target.value as MetricDef['time_mode'] })}
              >
                <option value="snapshot">时点快照（取最新一期）</option>
                <option value="flow">区间流水（按自然月）</option>
              </select>
            </label>
            <label>
              <span>时间列</span>
              <select value={form.time_col} onChange={(e) => setForm({ ...form, time_col: e.target.value })}>
                <option value="">（选一个）</option>
                {(viewCols[form.view] || []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>单位</span>
              <input value={form.unit || ''} onChange={(e) => setForm({ ...form, unit: e.target.value })} placeholder="元 / 笔 / %" />
            </label>
            <label className="wide">
              <span>表达式（对视图的聚合）</span>
              <input value={form.expr} onChange={(e) => setForm({ ...form, expr: e.target.value })} placeholder="SUM(dblbal)" />
            </label>
            <label className="wide">
              <span>可用维度（逗号分隔，必须都在视图列里）</span>
              <input
                value={toText(form.dims)}
                onChange={(e) => setForm({ ...form, dims: toList(e.target.value) })}
                placeholder="product_name，org_name"
              />
            </label>
            <label className="wide">
              <span>口径定义（讲清楚怎么算，业务方看得懂）</span>
              <textarea
                rows={2}
                value={form.definition || ''}
                onChange={(e) => setForm({ ...form, definition: e.target.value })}
              />
            </label>
            <label className="wide">
              <span>坑位提示（哪一步容易算错）</span>
              <textarea rows={2} value={form.caveats || ''} onChange={(e) => setForm({ ...form, caveats: e.target.value })} />
            </label>
          </div>
          <div className="pager">
            <button className="primary" onClick={commitEdit}>
              加入草稿
            </button>
            <button onClick={() => setEditing(null)}>取消</button>
          </div>
        </Card>
      ) : null}

      <Card
        title="试跑一句"
        collapsible
        id="metric-preview"
        extra={
          <>
            <input
              className="grow"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="例如：统计本月各贷款产品的放款金额"
              onKeyDown={(e) => {
                if (e.key === 'Enter') runPreview()
              }}
            />
            <button onClick={runPreview} disabled={!!busy || !question.trim()}>
              {busy === 'preview' ? '试跑中…' : '试跑'}
            </button>
          </>
        }
      >
        <p className="muted">
          试跑按当前已生效的口径走一遍「命中口径 → 生成 SQL → 只读执行」，不会创建需求单。
          刚改的口径要先「保存并生效」才会被用上。
        </p>
        {preview ? (
          preview.ok ? (
            <>
              <div className="trace">
                <div>
                  <span className="trace-k">命中口径</span>
                  <code>{preview.metric_label || preview.metric || '-'}</code>
                </div>
                <div>
                  <span className="trace-k">规划器</span>
                  <code>{preview.engine || '-'}</code>
                </div>
                <div>
                  <span className="trace-k">返回行数</span>
                  <code>{preview.row_count ?? '-'}</code>
                </div>
              </div>
              <pre className="sqlbox small">{preview.sql}</pre>
              {preview.rows?.length ? (
                <div className="table-wrap">
                  <table className="grid">
                    <thead>
                      <tr>
                        {(preview.columns || []).map((c) => (
                          <th key={c}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.rows.slice(0, 20).map((r, i) => (
                        <tr key={i}>
                          {(preview.columns || []).map((c) => (
                            <td key={c} className="mono">
                              {r[c] === null || r[c] === undefined ? '-' : String(r[c])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">这条 SQL 有结果结构但没有数据行。</p>
              )}
            </>
          ) : (
            <div className="errbox">
              <div className="errbox-title">{preview.blocked ? '没有命中口径' : '试跑失败'}</div>
              <div className="errbox-hint">
                <RichText text={preview.reason || preview.error || '未知原因'} />
              </div>
              {preview.guide ? <pre className="sqlbox small">{preview.guide}</pre> : null}
            </div>
          )
        ) : (
          <p className="muted">还没有试跑结果。</p>
        )}
      </Card>

      <Card title="口径依赖的视图" collapsible id="metric-views">
        <p className="muted">
          视图是数据侧提供的可执行定义（一段 SELECT），页面只登记口径、不改视图。
          要加视图得回到数据侧，避免在这里把物理口径改歪。
        </p>
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>视图</th>
                <th>列</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {views.map((v) => (
                <tr key={v.name}>
                  <td className="mono">{v.name}</td>
                  <td className="snippet">{(v.columns || []).join('、') || '-'}</td>
                  <td>{v.comment || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
