import { useState } from 'react'
import { api, type E2eStatus } from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'

/**
 * 页3 · 口径确认
 *
 * 实测行为（关键）：答复提交后**必须重跑一次 analyze**，
 * 否则答复不会作为 P2 证据进入证据链、也不会回填槽位。
 * 所以每次答复成功后，本页都会调analyze 并把「槽位是否已被回填」直观显示出来。
 */
export default function PageConfirm({
  demandId,
  status,
  onRefresh,
}: {
  demandId: string | null
  status: E2eStatus | null
  onRefresh: () => void
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [busyQ, setBusyQ] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [backfill, setBackfill] = useState<{
    question: string
    slots_changed: Array<{ slot: string; before: any; after: any }>
    p2_evidence_count: number
    p2_evidence: Array<Record<string, any>>
    backfilled: boolean
  } | null>(null)

  if (!demandId) {
    return (
      <div className="page">
        <Card title="口径确认">
          <p className="muted">还没有需求。请先到「提交需求」页提交一条。</p>
        </Card>
      </div>
    )
  }

  const conf = status?.confirmations
  const items: any[] = conf?.items || []
  const open = items.filter((i) => !i.answered)

  const answer = async (questionId: string) => {
    const answerText = (answers[questionId] || '').trim()
    if (!answerText) {
      setError({
        message: '还没填答复内容',
        hint: '请先选择一个选项；确实都不合适时选「其他（请补充说明）」并写明口径。',
        code: 'EMPTY_ANSWER',
      })
      return
    }
    setBusyQ(questionId)
    setError(null)
    try {
      const resp = await api.confirm(demandId, {
        question_id: questionId,
        answer: answerText,
        choice: answerText,
        actor: '',
      })
      setBackfill({ question: questionId, ...(resp.backfill as any) })
      setAnswers((a) => ({ ...a, [questionId]: '' }))
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusyQ(null)
    }
  }

  return (
    <div className="page">
      <Card
        title={`口径确认 · ${demandId}`}
        extra={
          <span className="muted">
            待确认 {conf?.open_count ?? 0} 条 · 已答复 {conf?.answered_count ?? 0} 条
          </span>
        }
      >
        <ErrorBox error={error} />
        <PartialErrorsBox errors={status?.partial_errors || []} />

        {open.length === 0 ? (
          <p className="muted">
            当前没有待确认的口径。
            {items.length > 0 ? '已答复记录见下方。' : '若系统未产出问题，通常说明该需求的口径已足够明确。'}
          </p>
        ) : (
          <div className="qlist">
            {open.map((q) => (
              <div className="q" key={q.question_id}>
                <div className="q-head">
                  <span className="q-slot">槽位：{q.slot}</span>
                  <span className="q-id">{q.question_id}</span>
                </div>
                <div className="q-title">{q.question}</div>
                {q.reason ? <div className="q-reason">为什么要问：{q.reason}</div> : null}
                {q.impact_scope ? <div className="q-impact">不确认的后果：{q.impact_scope}</div> : null}
                <div className="q-options">
                  {(q.options || []).map((opt: string, i: number) => (
                    <label className="opt" key={i}>
                      <input
                        type="radio"
                        name={q.question_id}
                        value={opt}
                        checked={(answers[q.question_id] || '') === opt}
                        onChange={() => setAnswers((a) => ({ ...a, [q.question_id]: opt }))}
                      />
                      <span>{opt}</span>
                    </label>
                  ))}
                </div>
                <div className="q-submit">
                  <button
                    className="primary"
                    disabled={busyQ === q.question_id}
                    onClick={() => answer(q.question_id)}
                  >
                    {busyQ === q.question_id ? '提交并重跑分析中…' : '提交答复并重跑分析'}
                  </button>
                  <span className="hint-inline">
                    答复会自动触发一轮重分析，答复将作为 P2 证据进入证据链
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}

        {busyQ ? <Loading tip="已提交答复，正在重跑分析以回填槽位…" /> : null}

        {backfill ? (
          <div className={`backfill${backfill.backfilled ? ' backfill-ok' : ' backfill-none'}`}>
            <h4 className="sec">
              上一条答复的回填结果
              <span className="muted">（问题 {backfill.question}）</span>
            </h4>
            <p>
              证据链 P2「业务确认」条数：
              <b className={backfill.p2_evidence_count > 0 ? 'ok-text' : 'bad-text'}>
                {backfill.p2_evidence_count}
              </b>
            </p>
            {backfill.slots_changed.length === 0 ? (
              <p className="muted">本轮没有槽位发生变化（答复已进入证据链，但该槽位仍未被自动判定）。</p>
            ) : (
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      <th>槽位</th>
                      <th>答复前</th>
                      <th>答复后</th>
                    </tr>
                  </thead>
                  <tbody>
                    {backfill.slots_changed.map((s) => (
                      <tr key={s.slot}>
                        <td>{s.slot}</td>
                        <td>
                          候选 {s.before?.candidate_count ?? 0} · 待确认{' '}
                          {s.before?.needs_confirmation ? '是' : '否'}
                        </td>
                        <td>
                          候选 {s.after?.candidate_count ?? 0} · 待确认{' '}
                          {s.after?.needs_confirmation ? '是' : '否'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {backfill.p2_evidence.map((e, i) => (
              <div className="p2-item" key={i}>
                <span className="lv lv-P2">P2</span>
                <span className="mono">{e.locator}</span>
                <span>{e.content}</span>
                {e.answered_by ? <span className="muted">答复人：{e.answered_by}</span> : null}
              </div>
            ))}
          </div>
        ) : null}

        {items.length > 0 ? (
          <>
            <h4 className="sec">全部确认记录</h4>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>问题</th>
                    <th>槽位</th>
                    <th>答复</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((i) => (
                    <tr key={i.confirmation_id}>
                      <td className="ev-content">{i.question}</td>
                      <td>{i.slot}</td>
                      <td>{i.answer || '-'}</td>
                      <td>
                        {i.answered ? (
                          <span className="chip chip-green">已答复 v{i.version}</span>
                        ) : (
                          <span className="chip chip-red">待确认</span>
                        )}
                      </td>
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