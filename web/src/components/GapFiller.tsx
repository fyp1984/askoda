import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Loading } from './ui'
import RichText from './RichText'

export default function GapFiller({
  risks,
  block,
  dataset,
  onRetried,
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
  } | null
  dataset: string
  onRetried: () => void | Promise<void>
}) {
  const [assets, setAssets] = useState<{ tables: string[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'what' | 'gap' | 'how'>('gap')
  const [realigning, setRealigning] = useState(false)
  const [realignMsg, setRealignMsg] = useState('')

  const title = block?.title || '需要补全知识后才能出数'
  const kind = block?.needs?.kind || '口径未建模'
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

  // 知识补全指引（CR009）：客户端不设知识上传/检索入口，知识收集与治理全部在
  // WeKnora 知识库平台完成后，回这里「重新对齐」——分析链路经网关 MCP 重新读取知识。

  // 重新对齐：先重跑分析读取最新知识，再重新生成。此前静默回调、无任何反馈，
  // 用户感知「点了没反应」（2026-10-22 修复）。
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

  return (
    <div className="gapcard">
      <div className="gaphead">
        <div className="gaphead-l">
          <div className="gapkind"><RichText text={kind} /></div>
          <div className="gaptitle"><RichText text={title} /></div>
        </div>
        <div className="gapowner">
          <span className="lbl">责任方</span>
          <span className="val"><RichText text={owner} /></span>
        </div>
      </div>

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
                {(!assets?.tables || !assets.tables.length) && <li className="muted">读取失败</li>}
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
              <span className="lineval"><RichText text={block.supported} /></span>
            </div>
          ) : null}
          <div className="line">
            <span className="linelbl no">当前差距</span>
            <span className="lineval"><RichText text={gapText} /></span>
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
          <ol className="fillsteps">
            {steps.map((s, i) => (
              <li key={i}><RichText text={s} /></li>
            ))}
            {!steps.length ? (
              <li>请技术方在语义层登记该意图，并把业务方确认的口径写入知识库。</li>
            ) : null}
          </ol>

          <div className="upbox">
            <div className="colhead">知识补全（在 WeKnora 知识库平台完成）</div>
            <p className="tip">
              本系统客户端不提供知识上传入口（CR009）。请到 <b>WeKnora 知识库平台</b>
              上传或更新对应口径文档（建议命名《XX-口径补充说明-数据域》），平台完成解析上架后，
              回到这里点「重新对齐」——分析链路会经知识底座 MCP 自动读取最新知识。
            </p>
          </div>

          <div className="donebox">
            <div className="colhead">验收标准</div>
            <ul>
              <li>点「生成 SQL」能成功产出 SQL</li>
              <li>门禁全部通过</li>
              <li>执行后拿到的数据与业务方确认的口径一致</li>
            </ul>
            <p className="tip">在此之前不必反复试，也不必修改业务代码。</p>
          </div>
        </div>
      ) : null}

      <div className="gapfoot">
        <button className="primary" onClick={handleRetry} disabled={realigning}>
          {realigning ? '重新对齐中…' : '重新对齐'}
        </button>
        <span className="tip">补完后点此验证 · 急用可在下方粘贴候选 SQL 兜底</span>
      </div>
      {realignMsg ? (
        <p className={`upmsg ${realignMsg.startsWith('✅') ? 'ok' : 'err'}`}>
          <RichText text={realignMsg} />
        </p>
      ) : null}
    </div>
  )
}