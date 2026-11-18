import { useCallback, useEffect, useState } from 'react'
import { api, type ConversationResp, type FollowupResp } from '../api/client'
import { ErrorBox, Loading } from './ui'

/**
 * 多轮追问（FR-4.8 · P2）
 *
 * 导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条在 Demo 中照常呈现**。
 *
 * 什么叫「不丢失上下文」——本组件把它落到可见的事实上，而不是一句承诺：
 *   · 原文：追问整句**追加**进需求描述（`【追问·第N轮】…`），不覆盖原需求；
 *   · 历轮：每轮追问重跑语义分析 = 新增一个 `round_no`，旧轮次原样保留；
 *   · 历版：每轮重新合成结构化需求 = `version+1`，旧版原样保留。
 * 所以卡片上半是「轮次链」（每轮挂到具体的 round 与 version），下半才是「提问」——
 * 先让人看见上下文确实还在，再让他加一轮。
 */
export default function FollowupPanel({
  demandId,
  dataset,
  onDone,
}: {
  demandId: string
  dataset: string
  /** 追问成功后会重跑分析，通知外层刷新整页状态 */
  onDone?: () => void
}) {
  const [conv, setConv] = useState<ConversationResp | null>(null)
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [last, setLast] = useState<FollowupResp | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setConv(await api.conversation(demandId, dataset))
    } catch (e) {
      setError(e)
    }
  }, [demandId, dataset])

  useEffect(() => {
    void load()
  }, [load])

  const submit = async () => {
    const text = instruction.trim()
    if (!text) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.followup(demandId, { instruction: text, dataset })
      setLast(r)
      setInstruction('')
      await load()
      onDone?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const turns = conv?.turns || []

  return (
    <>
      <p className="muted">
        {conv?.context_policy ||
          '上下文 = 需求原文（追加不覆盖）+ 历轮分析 + 历版结构化需求，只增不改、任一轮可回放。'}
      </p>

      <ErrorBox error={error} />
      {!conv && !error ? <Loading tip="正在读取会话轮次…" /> : null}

      {turns.length > 0 ? (
        <>
          <h4 className="sec">会话轮次（{conv?.turn_count ?? turns.length} 轮）</h4>
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>轮次</th>
                  <th>类型</th>
                  <th>追问内容 / 意图</th>
                  <th>分析轮次</th>
                  <th>结构化版本</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {turns.map((t) => (
                  <tr key={t.turn} className={t.kind === '原始需求' ? 'row-ok' : ''}>
                    <td className="mono">第 {t.turn} 轮</td>
                    <td>
                      {t.kind === '原始需求' ? (
                        <span className="chip chip-green">原始需求</span>
                      ) : (
                        <span className="chip chip-blue">多轮追问</span>
                      )}
                    </td>
                    <td className="snippet">
                      {t.instruction || '原始需求，无追问内容'}
                      {(t.intents || []).length > 0
                        ? (t.intents || []).map((it, i) => (
                            <span className="chip chip-muted" key={i}>
                              {it.kind}
                            </span>
                          ))
                        : null}
                    </td>
                    <td className="mono">{t.round_no ?? '-'}</td>
                    <td className="mono">{t.requirement_version ?? '-'}</td>
                    <td className="mono">{t.created_at || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">
            追问不新建需求单：同一条需求下每轮各留一个分析轮次与结构化版本，旧轮次不会被覆盖，可并排比对。
          </p>
        </>
      ) : null}

      <h4 className="sec">再加一轮追问</h4>
      <textarea
        className="sqlinput"
        rows={3}
        value={instruction}
        placeholder="例如：再加一个维度：按渠道拆分；或 换个时间范围：近 7 天"
        onChange={(e) => setInstruction(e.target.value)}
      />
      <div className="followup-actions">
        <button className="primary" onClick={submit} disabled={busy || !instruction.trim()}>
          {busy ? '追问中…' : '提交追问并重跑分析'}
        </button>
        <span className="muted">
          提交后：原文追加 → 重跑语义分析（新轮次）→ 重新合成结构化需求（新版本）。
        </span>
      </div>

      {last?.ok ? (
        <div className="okbox">
          <div className="okbox-title">第 {last.turn} 轮追问已入链</div>
          <div className="okbox-msg">
            {last.context_retained}
            分析轮次 {last.round_no ?? '-'} · 结构化版本 {last.requirement_version ?? '-'}。
          </div>
        </div>
      ) : null}
    </>
  )
}
