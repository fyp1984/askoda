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
  onRetried: () => void
}) {
  const [assets, setAssets] = useState<{ tables: string[]; knowledge: string[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'what' | 'gap' | 'how'>('gap')
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')

  const title = block?.title || '需要补全知识后才能出数'
  const kind = block?.needs?.kind || '口径未建模'
  const owner = block?.needs?.owner || '业务方 + 技术方'
  const gapText = block?.gap || risks.join('；') || '该问题尚未对齐任何已建模口径'
  const steps = block?.guide_steps || []

  useEffect(() => {
    let alive = true
    ;(async () => {
      setLoading(true)
      const [mdl, docs] = await Promise.all([
        api.semanticMdl({ dataset }).catch(() => null),
        api.knowledgeDocuments(100).catch(() => null),
      ])
      if (!alive) return
      const tables: string[] = []
      const md = (mdl as any)?.tables || []
      for (const t of md) {
        if (t?.table_name) tables.push(`${t.table_name}（${(t.columns || []).length} 字段）`)
      }
      const knowledge: string[] = ((docs as any)?.documents || []).map((d: any) => {
        const nm = d.name || d.document_name || '未命名'
        return `${nm}${d.chunk_count ? `（${d.chunk_count} 块）` : ''}`
      })
      setAssets({ tables, knowledge })
      setLoading(false)
    })()
    return () => {
      alive = false
    }
  }, [dataset])

  // 知识文档补写：直接把界面输入送进知识库，避免「知道要补却无处可补」
  const onUpload = async (file: File) => {
    setUploading(true)
    setUploadMsg('上传中…')
    try {
      const b64 = await new Promise<string>((resolve, reject) => {
        const fr = new FileReader()
        fr.onload = () => {
          const s = String(fr.result || '')
          resolve(s.includes(',') ? s.split(',')[1] : s)
        }
        fr.onerror = () => reject(new Error('读取文件失败'))
        fr.readAsDataURL(file)
      })
      const r = await api.knowledgeUpload({
        filename: file.name,
        content_base64: b64,
        content_type: 'text/markdown',
        actor: '业务/技术补全',
      })
      setUploadMsg(`✅ 已上传《${r?.name || file.name}》，入库需 1–3 分钟；完成后点「重新对齐」验证。`)
    } catch (e: any) {
      setUploadMsg(`❌ 上传失败：${e?.message || e}`)
    } finally {
      setUploading(false)
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
            <Loading tip="正在读取语义层与知识库现状…" />
          ) : (
            <div className="two-col">
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
              <div className="colcard">
                <div className="colhead">知识库已有文档（{assets?.knowledge.length || 0}）</div>
                <ul className="asslist">
                  {(assets?.knowledge || []).map((k) => (
                    <li key={k}>{k}</li>
                  ))}
                  {(!assets?.knowledge || !assets.knowledge.length) && (
                    <li className="muted">知识库为空或读取失败</li>
                  )}
                </ul>
                <p className="tip">若所需口径已在此列内，直接引用，无需重写。</p>
              </div>
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
            <div className="colhead">直接补写知识（随时可用）</div>
            <p className="tip">
              选中补写好的 Markdown（建议命名《XX-口径补充说明-数据域》），上传进知识库：
            </p>
            <input
              type="file"
              accept=".md,.markdown,.txt"
              disabled={uploading}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) onUpload(f)
              }}
            />
            {uploadMsg ? <p className="upmsg"><RichText text={uploadMsg} /></p> : null}
          </div>

          <div className="donebox">
            <div className="colhead">验收标准</div>
            <ul>
              <li>点「生成 SQL」能成功产出 SQL</li>
              <li>七层门禁全部通过</li>
              <li>执行后拿到的数据与业务方确认的口径一致</li>
            </ul>
            <p className="tip">在此之前不必反复试，也不必修改业务代码。</p>
          </div>
        </div>
      ) : null}

      <div className="gapfoot">
        <button className="primary" onClick={onRetried}>
          重新对齐
        </button>
        <span className="tip">补完后点此验证 · 急用可在下方粘贴候选 SQL 兜底</span>
      </div>
    </div>
  )
}