import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type SimilarPrecheckResp } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 页1 · 提交需求
 * 表单字段对齐网关 demand_create 的实盘入参（tools/list 核对）：
 *   title / business_context / description / expected_output / contact  为必填；
 *   time_range / expected_finish_at / actor 为可选。demand_id 由服务端生成。
 */
export type DemandForm = {
  title: string
  business_context: string
  description: string
  expected_output: string
  contact: string
  time_range: string
  expected_finish_at: string
  actor: string
  dataset: string
}

const EMPTY: DemandForm = {
  title: '',
  business_context: '',
  description: '',
  expected_output: '',
  contact: '',
  time_range: '',
  expected_finish_at: '',
  actor: '',
  dataset: 'B',
}

const REQUIRED: Array<{ key: keyof DemandForm; label: string; hint: string }> = [
  { key: 'title', label: '需求标题', hint: '一句话说清要看什么' },
  { key: 'business_context', label: '业务背景', hint: '为什么要看、给谁看' },
  { key: 'description', label: '需求说明', hint: '统计口径、维度、过滤条件' },
  { key: 'expected_output', label: '输出预期', hint: '希望结果长什么样' },
  { key: 'contact', label: '联系人', hint: '口径有歧义时找谁确认' },
]

export default function PageSubmit({
  onSubmitted,
  demandId,
}: {
  onSubmitted: (id: string) => void
  demandId: string | null
}) {
  const [form, setForm] = useState<DemandForm>(EMPTY)
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [similar, setSimilar] = useState<SimilarPrecheckResp | null>(null)
  const timer = useRef<number | null>(null)

  const set = (k: keyof DemandForm, v: string) => {
    setForm((f) => ({ ...f, [k]: v }))
    setTouched((t) => ({ ...t, [k]: true }))
  }

  // 输入时 debounce 500ms 查重
  useEffect(() => {
    const title = form.title.trim()
    if (timer.current) window.clearTimeout(timer.current)
    if (title.length < 4) {
      setSimilar(null)
      return
    }
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.similarPrecheck({
          title,
          description: form.description,
          top: 5,
          threshold: 0.55,
        })
        setSimilar(r)
      } catch {
        setSimilar(null)
      }
    }, 500)
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [form.title, form.description])

  const missing = REQUIRED.filter((r) => !form[r.key].trim())
  const canSubmit = missing.length === 0 && !busy

  const submit = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        title: form.title.trim(),
        business_context: form.business_context.trim(),
        description: form.description.trim(),
        expected_output: form.expected_output.trim(),
        contact: form.contact.trim(),
      }
      if (form.time_range.trim()) payload.time_range = form.time_range.trim()
      if (form.expected_finish_at.trim()) payload.expected_finish_at = form.expected_finish_at.trim()
      if (form.actor.trim()) payload.actor = form.actor.trim()

      const created = await api.createDemand(payload)
      const id = (created as any).demand_id
      if (id) {
        setForm(EMPTY)
        setSimilar(null)
        onSubmitted(id)
      }
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [form, onSubmitted])

  return (
    <div className="page">
      <Card title="填写需求">
        <div className="form">
          {REQUIRED.map((r) => {
            const bad = touched[r.key] && !form[r.key].trim()
            const isArea = r.key === 'business_context' || r.key === 'description' || r.key === 'expected_output'
            return (
              <label key={r.key} className="field">
                <span className="field-label">
                  {r.label}
                  <em className="req">必填</em>
                </span>
                {isArea ? (
                  <textarea
                    value={form[r.key]}
                    rows={r.key === 'description' ? 4 : 2}
                    placeholder={r.hint}
                    onChange={(e) => set(r.key, e.target.value)}
                    onBlur={() => setTouched((t) => ({ ...t, [r.key]: true }))}
                    className={bad ? 'invalid' : ''}
                  />
                ) : (
                  <input
                    value={form[r.key]}
                    placeholder={r.hint}
                    onChange={(e) => set(r.key, e.target.value)}
                    onBlur={() => setTouched((t) => ({ ...t, [r.key]: true }))}
                    className={bad ? 'invalid' : ''}
                  />
                )}
                {bad ? <span className="field-err">这里必须填，缺了会被硬门禁挡下来</span> : null}
              </label>
            )
          })}

          <div className="field-row">
            <label className="field">
              <span className="field-label">时间范围</span>
              <input
                value={form.time_range}
                placeholder="例如 2026-01-01 ~ 2026-09-30（强烈建议写具体日期）"
                onChange={(e) => set('time_range', e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">期望完成时间</span>
              <input
                value={form.expected_finish_at}
                placeholder="例如 2026-10-20"
                onChange={(e) => set('expected_finish_at', e.target.value)}
              />
            </label>
          </div>

          <div className="field-row">
            <label className="field">
              <span className="field-label">提交人</span>
              <input value={form.actor} placeholder="用于留痕" onChange={(e) => set('actor', e.target.value)} />
            </label>
            <label className="field">
              <span className="field-label">数据集</span>
              <select value={form.dataset} onChange={(e) => set('dataset', e.target.value)}>
                <option value="B">B 库 · 零售会员域</option>
                <option value="A">A 库 · 电商域</option>
              </select>
            </label>
          </div>

          {similar && similar.matched > 0 ? (
            <div className="warnbox">
              <div className="warnbox-title">
                检测到 {similar.matched} 条相似需求，是否先看已有的？
              </div>
              <ul className="warnbox-list">
                {similar.items.map((it) => (
                  <li key={it.demand_id}>
                    <button className="linklike" onClick={() => onSubmitted(it.demand_id)}>
                      {it.demand_id}
                    </button>
                    <span className="warnbox-msg">{it.title}</span>
                    <span className="sim">相似度 {it.similarity}</span>
                  </li>
                ))}
              </ul>
              <div className="warnbox-hint">
                如果这就是同一个需求，直接看已有的即可，不必重复提交。
              </div>
            </div>
          ) : null}

          <ErrorBox error={error} />

          <div className="actions">
            <button className="primary" disabled={!canSubmit} onClick={submit}>
              {busy ? '提交中…' : '提交需求'}
            </button>
            {missing.length > 0 ? (
              <span className="hint-inline">还差：{missing.map((m) => m.label).join('、')}</span>
            ) : null}
          </div>
        </div>
      </Card>

      {demandId ? (
        <Card title="最近一次提交">
          <p>
            当前需求编号：<code>{demandId}</code>
          </p>
          <p className="muted">已自动切换到「查看分析」页。</p>
        </Card>
      ) : null}
      {busy ? <Loading tip="正在提交…" /> : null}
    </div>
  )
}

export { ApiError }