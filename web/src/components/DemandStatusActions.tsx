import { useState } from 'react'
import { api, DEMAND_STATUSES } from '../api/client'
import { Card, ErrorBox } from '../components/ui'

/**
 * 需求状态流转（PRD §9.3 回退与修改闭环）
 *
 * 为什么必须有这一块：状态机定义了终态「已通过」，但前端此前零入口，
 * 业务人员演示到"取到数"就没有下一步——需求永远卡在中间态。
 *
 * 两条流转都要**二次确认**：状态变更逐条留痕且不可覆盖，误点成本很高。
 * 「退回」额外强制填原因——退回不给理由等于把问题踢回给提交方。
 */
export default function DemandStatusActions({
  demandId,
  currentStatus,
  onDone,
}: {
  demandId: string
  currentStatus?: string
  onDone: () => void
}) {
  const [confirming, setConfirming] = useState<'已通过' | '已退回' | null>(null)
  const [note, setNote] = useState('')
  const [actor, setActor] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [done, setDone] = useState('')

  const isFinal = currentStatus === '已通过'

  const submit = async () => {
    if (!confirming) return
    const reason = note.trim()
    if (confirming === '已退回' && !reason) {
      setError({
        message: '退回必须写明原因。',
        hint: '请在退回原因里写清楚业务方要改什么（例如口径不对、维度缺失），否则提交方无法定位问题。',
        code: 'NOTE_REQUIRED',
      })
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.setDemandStatus(demandId, {
        status: confirming,
        note: confirming === '已退回' ? reason : note.trim() || undefined,
        actor: actor.trim(),
      })
      setDone(`已流转为「${confirming}」，状态变更已留痕。`)
      setConfirming(null)
      setNote('')
      onDone()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const cancel = () => {
    setConfirming(null)
    setNote('')
    setError(null)
  }

  return (
    <Card title={`状态流转 · ${demandId}`}>
      <ErrorBox error={error} />

      <p>
        当前状态：
        <span className="chip chip-muted">{currentStatus || '未知'}</span>
        {isFinal ? <span className="chip chip-green">已收口</span> : null}
      </p>

      {done ? (
        <div className="backfill backfill-ok">
          <p>{done}</p>
        </div>
      ) : null}

      {confirming === null ? (
        <>
          <div className="actions">
            <button
              className="primary"
              disabled={busy || isFinal}
              onClick={() => {
                setDone('')
                setConfirming('已通过')
              }}
            >
              验收通过
            </button>
            <button
              disabled={busy || isFinal}
              onClick={() => {
                setDone('')
                setConfirming('已退回')
              }}
            >
              退回修改
            </button>
            <span className="hint-inline">
              {isFinal
                ? '该需求已验收通过，状态为终态，无需再流转。'
                : '「验收通过」把需求收到终态；「退回修改」需要写明原因，供提交方定位问题。'}
            </span>
          </div>
          {isFinal ? null : (
            <p className="muted">
              状态机取值：{DEMAND_STATUSES.join(' / ')}。本块提供其中最常用的两个终态流转入口。
            </p>
          )}
        </>
      ) : (
        <div className="confirmbox">
          <div className="confirmbox-title">
            确认把需求流转为「{confirming}」？
          </div>
          <div className="confirmbox-hint">
            状态变更逐条留痕且不可覆盖，请确认后再提交。
          </div>

          <label className="field">
            <span className="field-label">
              操作人
              <em className="opt-tag">可选</em>
            </span>
            <input value={actor} placeholder="用于留痕，例如你的姓名" onChange={(e) => setActor(e.target.value)} />
          </label>

          <label className="field">
            <span className="field-label">
              {confirming === '已退回' ? '退回原因' : '说明'}
              {confirming === '已退回' ? <em className="req">必填</em> : <em className="opt-tag">可选</em>}
            </span>
            <textarea
              rows={3}
              placeholder={
                confirming === '已退回'
                  ? '例如：时间口径应为自然月而非滚动30天；缺少渠道维度'
                  : '例如：业务方已确认口径，验收通过'
              }
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>

          <div className="actions">
            <button className="primary" disabled={busy} onClick={submit}>
              {busy ? '提交中…' : `确认流转为「${confirming}」`}
            </button>
            <button disabled={busy} onClick={cancel}>
              取消
            </button>
          </div>
        </div>
      )}
    </Card>
  )
}