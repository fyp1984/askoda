import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { Loading } from './ui'
import RichText from './RichText'

/**
 * 生成失败后的缺口处置卡片。
 *
 * 产品判断（CR010，2026-10-30 西北人）
 * ------------------------------------
 * 缺口处置的**第一顺位是退回需求方补充**，不是去知识库查内容。
 * 需求说不清时，让提出人补一句话是分钟级、一个人的事；补知识库口径是
 * 小时/天级、跨部门的事。以前的界面只有"去 WeKnora 补知识"一条路，
 * 等于把最便宜的路径藏起来了。
 *
 * 所以本卡片分三档处置，按成本从低到高排：
 *   ① 退回需求方补充（最快）——缺的是需求描述本身
 *   ② 去知识库补口径（一次投入·长期复用）——需求说清了但标准没登记
 *   ③ 技术侧接数据源——所需数据本库不存在
 *
 * 退回不是打回重做：卡片就地变成补充表单，答完即回流重跑——
 * 因为提需求的人和答问题的人往往就是同一个业务方，多跳一次页面就是多一次放弃。
 */

export type ClarifyItem = {
  id?: string
  slot?: string
  question?: string
  why?: string
  options?: string[]
  example?: string
  affects?: string
  anchor?: string
  required?: boolean
  term?: string
  answer?: string
}

export type ClarifyPack = {
  route?: 'demand' | 'knowledge' | 'engineering'
  route_reason?: string
  headline?: string
  required_count?: number
  items?: ClarifyItem[]
  already_known?: { label: string; value: string }[]
  suggested_description?: string
  note?: string
  status?: string
  gap_kind?: string
}

export default function GapFiller({
  risks,
  block,
  dataset,
  demandId,
  demandStatus,
  onRetried,
  onReturned,
}: {
  risks: string[]
  block?: {
    title?: string
    reason?: string
    guide?: string
    supported?: string
    gap?: string
    guide_steps?: string[]
    needs?: { kind?: string; owner?: string }
    route?: string
    clarify?: ClarifyPack
  } | null
  dataset: string
  demandId?: string | null
  /** 需求当前状态；为「待补充修改」时直接进补充态 */
  demandStatus?: string
  onRetried: () => void | Promise<void>
  /** 退回/补充成功后通知外层刷新 */
  onReturned?: () => void | Promise<void>
}) {
  const [assets, setAssets] = useState<{ tables: string[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'what' | 'gap' | 'how'>('how')
  const [realigning, setRealigning] = useState(false)
  const [realignMsg, setRealignMsg] = useState('')
  const [pack, setPack] = useState<ClarifyPack | null>(block?.clarify || null)
  const [phase, setPhase] = useState<'decide' | 'supplement'>(
    demandStatus === '待补充修改' ? 'supplement' : 'decide',
  )
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [extra, setExtra] = useState('')
  const [busy, setBusy] = useState<'pack' | 'return' | 'submit' | null>(null)
  const [flash, setFlash] = useState('')

  const title = block?.title || '当前需求还出不了数'
  const kind = block?.needs?.kind || pack?.gap_kind || '口径未建模'
  const owner = block?.needs?.owner || '业务方 + 技术方'
  const gapText = block?.gap || risks.join('；') || '该问题尚未对齐任何已建模口径'
  const steps = block?.guide_steps || []

  useEffect(() => {
    let alive = true
    ;(async () => {
      setLoading(true)
      const mdl = await api.semanticMdl({ dataset }).catch(() => null)
      if (!alive) return
      const tables: string[] = []
      const md = (mdl as any)?.tables || []
      for (const t of md) {
        if (t?.table_name) tables.push(`${t.table_name}（${(t.columns || []).length} 字段）`)
      }
      setAssets({ tables })
      setLoading(false)
    })()
    return () => {
      alive = false
    }
  }, [dataset])

  // 老响应里没有 clarify（网关未升级 / 缓存）时补拉一次，保证界面不空转
  useEffect(() => {
    if (pack || !demandId) return
    let alive = true
    ;(async () => {
      setBusy('pack')
      try {
        const p = await api.clarifyPack(demandId, {
          dataset,
          gap_kind: block?.needs?.kind || '',
          gap_text: block?.gap || '',
        })
        if (alive) setPack(p as ClarifyPack)
      } catch {
        /* 拉不到就退化为静态说明，不阻塞 */
      } finally {
        if (alive) setBusy(null)
      }
    })()
    return () => {
      alive = false
    }
  }, [pack, demandId, dataset, block])

  const items = pack?.items || []
  const route = block?.route || pack?.route || 'knowledge'

  /** 已答的项（提交时只送有答案的） */
  const answered = useMemo(
    () =>
      items
        .map((i) => ({ ...i, answer: (answers[i.id || i.question || ''] || '').trim() }))
        .filter((i) => i.answer),
    [items, answers],
  )

  const keyOf = (i: ClarifyItem) => i.id || i.question || ''
  const setAns = (key: string, v: string) =>
    setAnswers((prev) => ({ ...prev, [key]: v }))

  // 重新对齐：先重跑分析读取最新知识，再重新生成
  const handleRetry = async () => {
    setRealigning(true)
    setRealignMsg('正在重新分析并读取最新知识…')
    try {
      await onRetried()
      setRealignMsg('✅ 已重新对齐并重新生成，请查看上方 SQL 与门禁结果')
    } catch (e: any) {
      setRealignMsg(`❌ 重新对齐失败：${e?.message || e}`)
    } finally {
      setRealigning(false)
    }
  }

  const doReturn = async () => {
    if (!demandId) return
    setBusy('return')
    setFlash('')
    try {
      const r = await api.returnDemand(demandId, {
        dataset,
        gap_kind: block?.needs?.kind || pack?.gap_kind || '',
        gap_text: block?.gap || '',
        items: items.map((i) => ({
          id: i.id,
          slot: i.slot,
          question: i.question,
          answer: '',
        })),
        note: `退回补充：${pack?.headline || '需求描述需细化'}`,
      })
      setPack({ ...(pack || {}), ...(r as ClarifyPack) })
      setPhase('supplement')
      setFlash('已退回。补充下面几项后自动重新分析——不必先去知识库。')
      // ⚠️ 不自动预填「示例草稿」：示例是系统给的写法示范，直接填成答案
      //    等于替业务方编口径。改为由用户点「填入示例」显式采纳（见下方按钮）。
      await onReturned?.()
    } catch (e: any) {
      setFlash(`❌ 退回失败：${e?.message || e}`)
    } finally {
      setBusy(null)
    }
  }

  const doSupplement = async () => {
    if (!demandId) return
    if (!answered.length && !extra.trim()) {
      setFlash('❌ 至少回答一项，或直接写补充说明')
      return
    }
    setBusy('submit')
    setFlash('')
    try {
      await api.supplementDemand(demandId, {
        answers: answered.map((i) => ({
          id: i.id,
          slot: i.slot,
          question: i.question,
          answer: i.answer,
        })),
        extra_description: extra,
      })
      setFlash('✅ 已写回需求，正在重新分析…')
      await onRetried()
      setFlash('✅ 补充已生效并重新生成，请看上方结果')
      await onReturned?.()
    } catch (e: any) {
      setFlash(`❌ 补充提交失败：${e?.message || e}`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="gapcard">
      <div className="gaphead">
        <div className="gaphead-l">
          <div className="gapkind">
            <RichText text={kind} />
          </div>
          <div className="gaptitle">
            <RichText text={phase === 'supplement' ? '已退回 · 等待补充需求' : title} />
          </div>
        </div>
        <div className="gapowner">
          <span className="lbl">责任方</span>
          <span className="val">
            <RichText text={phase === 'supplement' ? '需求提出方' : owner} />
          </span>
        </div>
      </div>

      {phase === 'decide' ? (
        <>
          <div className="gaptabs">
            <button className={tab === 'what' ? 'on' : ''} onClick={() => setTab('what')}>
              1. 系统里有什么
            </button>
            <button className={tab === 'gap' ? 'on' : ''} onClick={() => setTab('gap')}>
              2. 差在哪里
            </button>
            <button className={tab === 'how' ? 'on' : ''} onClick={() => setTab('how')}>
              3. 怎么补
            </button>
          </div>

          {tab === 'what' ? (
            <div className="gapbody">
              {loading ? (
                <Loading tip="正在读取语义层现状…" />
              ) : (
                <div className="colcard">
                  <div className="colhead">可见数据表（{assets?.tables.length || 0}）</div>
                  <ul className="asslist">
                    {(assets?.tables || []).map((t) => (
                      <li key={t}>{t}</li>
                    ))}
                    {(!assets?.tables || !assets.tables.length) && (
                      <li className="muted">读取失败</li>
                    )}
                  </ul>
                  <p className="tip">
                    口径只能建立在这些表之上。若所需数据不在此列，属<b>数据缺失</b>，须先接入数据源。
                  </p>
                </div>
              )}
            </div>
          ) : null}

          {tab === 'gap' ? (
            <div className="gapbody">
              {block?.supported ? (
                <div className="line">
                  <span className="linelbl ok">已有相近口径</span>
                  <span className="lineval">
                    <RichText text={block.supported} />
                  </span>
                </div>
              ) : null}
              <div className="line">
                <span className="linelbl no">当前差距</span>
                <span className="lineval">
                  <RichText text={gapText} />
                </span>
              </div>
              {risks.length > 1 ? (
                <details className="rawdetail">
                  <summary>查看原始返回（排障用）</summary>
                  <ul>
                    {risks.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ) : null}

          {tab === 'how' ? (
            <div className="gapbody">
              {busy === 'pack' ? <Loading tip="正在分析该补什么…" /> : null}

              {/* 三档处置：成本从低到高排，第一档默认推荐 */}
              <div className="routebox">
                <div className={`routeitem ${route === 'demand' ? 'rec' : ''}`}>
                  <div className="routehead">
                    <span className="routeno">①</span>
                    <span className="routename">退回需求方补充</span>
                    <span className="routetag fast">最快 · 分钟级</span>
                    {route === 'demand' ? <span className="routestar">推荐</span> : null}
                  </div>
                  <p className="routedesc">
                    {route === 'demand' && pack?.headline
                      ? pack.headline
                      : '需求描述缺关键信息、口径说不清时用它'}
                    <span className="routemeta">
                      （一个人、改一段话，不必跨部门推进）
                    </span>
                  </p>
                </div>
                <div className={`routeitem ${route === 'knowledge' ? 'rec' : ''}`}>
                  <div className="routehead">
                    <span className="routeno">②</span>
                    <span className="routename">补知识库口径</span>
                    <span className="routetag slow">一次投入 · 长期复用</span>
                  </div>
                  <p className="routedesc">
                    需求已经说清、只是标准口径没登记时用它。
                    <span className="routemeta">
                      要走知识治理流程，单条需求急用时别等这条。
                    </span>
                  </p>
                </div>
                <div className={`routeitem ${route === 'engineering' ? 'rec' : ''}`}>
                  <div className="routehead">
                    <span className="routeno">③</span>
                    <span className="routename">技术侧接数据源</span>
                    <span className="routetag hard">仅当数据缺失</span>
                  </div>
                  <p className="routedesc">
                    所需表/字段本库没有，补需求与补知识都解决不了。
                  </p>
                </div>
              </div>

              {/* 待明确项：可勾选、可一键填示例、可手写 */}
              {items.length ? (
                <div className="claritbox">
                  <div className="clarithead">
                    待明确 {items.length} 项
                    {pack?.required_count ? (
                      <span className="claritmust">其中必答 {pack.required_count} 项</span>
                    ) : null}
                  </div>
                  {items.map((i) => {
                    const key = keyOf(i)
                    return (
                      <div className={`clarit ${i.required ? 'must' : ''}`} key={key}>
                        <div className="claritq">
                          <span className="claritslot">{i.slot}</span>
                          <RichText text={i.question || ''} />
                          {i.required ? <span className="clarittag">必答</span> : null}
                        </div>
                        {i.why ? <div className="claritwhy">{i.why}</div> : null}
                        <div className="claritopts">
                          {(i.options || []).map((o) => (
                            <button
                              key={o}
                              className="optbtn"
                              onClick={() => setAns(key, o)}
                              type="button"
                            >
                              {o}
                            </button>
                          ))}
                          {i.example ? (
                            <button
                              className="optbtn ghost"
                              onClick={() => setAns(key, i.example || '')}
                              type="button"
                            >
                              用示例口径
                            </button>
                          ) : null}
                        </div>
                        <input
                          className="claritinput"
                          placeholder="或直接写你的口径…"
                          value={answers[key] || ''}
                          onChange={(e) => setAns(key, e.target.value)}
                        />
                      </div>
                    )
                  })}
                  {pack?.already_known?.length ? (
                    <details className="knownbox">
                      <summary>这些已经对过，不用再填（{pack.already_known.length}）</summary>
                      <ul>
                        {pack.already_known.map((k, idx) => (
                          <li key={idx}>
                            <b>{k.label}</b>：{k.value}
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </div>
              ) : null}

              {/* 退回动作：带着待明确项一起退回，不是丢一句"补一下"。
                  缺口不在需求侧时弱化为次要动作——否则等于劝业务方做无用功。 */}
              <div className="returnbar">
                <button
                  className={route === 'demand' ? 'primary' : 'secondary'}
                  onClick={doReturn}
                  disabled={!demandId || busy !== null}
                >
                  {busy === 'return'
                    ? '退回中…'
                    : route === 'demand'
                      ? '退回需求方补充'
                      : route === 'knowledge'
                        ? '退回需求方确认口径'
                        : '仍要退回需求方补充'}
                </button>
                <span className="tip">
                  {route === 'engineering'
                    ? '这类缺口问需求方也解决不了（数据本库没有）；退回仅用于「其实需求本身提错了」的情形。'
                    : `退回会把上面 ${items.length} 项一并发给对方；若你就是需求方，退回后可直接在下方作答。`}
                </span>
              </div>

              <div className="upbox">
                <div className="colhead">② 补知识库口径（可选路径）</div>
                <p className="tip">
                  当需求已说清、缺的只是「标准口径没登记」时走这条。到 <b>WeKnora 知识库平台</b>
                  上传或更新口径文档（建议命名《XX-口径补充说明-数据域》），平台解析上架后，
                  回这里点「重新对齐」——分析链路会经 MCP 自动读取最新知识。
                </p>
              </div>

              {/* 技术侧处置细节：对业务方是噪音，折起来给排障的人看 */}
              {steps.length ? (
                <details className="donebox">
                  <summary>技术侧处置细节（排障用，业务方无需操作）</summary>
                  <ol className="fillsteps">
                    {steps.map((s, i) => (
                      <li key={i}>
                        <RichText text={s} />
                      </li>
                    ))}
                  </ol>
                </details>
              ) : null}
            </div>
          ) : null}
        </>
      ) : (
        /* —— 补充态：退回后就地作答，答完自动回流 —— */
        <div className="gapbody">
          <div className="supintro">
            <RichText text={pack?.note || '退回不是打回重做——补充下面几项即可继续。'} />
          </div>
          {items.map((i) => {
            const key = keyOf(i)
            return (
              <div className={`clarit ${i.required ? 'must' : ''}`} key={key}>
                <div className="claritq">
                  <span className="claritslot">{i.slot}</span>
                  <RichText text={i.question || ''} />
                  {i.required ? <span className="clarittag">必答</span> : null}
                </div>
                {i.why ? <div className="claritwhy">{i.why}</div> : null}
                <div className="claritopts">
                  {(i.options || []).map((o) => (
                    <button
                      key={o}
                      className={`optbtn ${answers[key] === o ? 'on' : ''}`}
                      onClick={() => setAns(key, o)}
                      type="button"
                    >
                      {o}
                    </button>
                  ))}
                </div>
                <input
                  className="claritinput"
                  placeholder={i.example ? `示例：${i.example}` : '写你的口径…'}
                  value={answers[key] || ''}
                  onChange={(e) => setAns(key, e.target.value)}
                />
              </div>
            )
          })}
          <div className="claritsupp">
            <div className="colhead">
              补充说明（会追加到需求描述，不覆盖原文）
              {pack?.suggested_description ? (
                <button
                  className="linklike"
                  type="button"
                  onClick={() => setExtra(pack.suggested_description || '')}
                  style={{ marginLeft: 10 }}
                >
                  填入示例口径（请核对后修改）
                </button>
              ) : null}
            </div>
            <textarea
              rows={4}
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              placeholder="也可以在这里直接改写/补充需求描述…"
            />
          </div>
          <div className="returnbar">
            <button className="primary" onClick={doSupplement} disabled={busy !== null}>
              {busy === 'submit' ? '提交中…' : '提交补充并重新分析'}
            </button>
            <button
              className="linklike"
              onClick={handleRetry}
              disabled={realigning || busy !== null}
              type="button"
            >
              {realigning ? '重新对齐中…' : '先不补，直接重新对齐试试'}
            </button>
          </div>
        </div>
      )}

      {phase === 'decide' ? (
        <div className="gapfoot">
          <button className="primary" onClick={handleRetry} disabled={realigning}>
            {realigning ? '重新对齐中…' : '重新对齐'}
          </button>
          <span className="tip">补完后点此验证 · 急用可在下方粘贴候选 SQL 兜底</span>
        </div>
      ) : null}
      {flash ? (
        <p className={`upmsg ${flash.startsWith('✅') ? 'ok' : 'err'}`}>
          <RichText text={flash} />
        </p>
      ) : null}
      {realignMsg ? (
        <p className={`upmsg ${realignMsg.startsWith('✅') ? 'ok' : 'err'}`}>
          <RichText text={realignMsg} />
        </p>
      ) : null}
    </div>
  )
}
