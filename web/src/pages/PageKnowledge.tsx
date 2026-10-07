import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 菜单② · 知识储备
 *
 * PRD §12.7：知识收集（手动上传 / 自动获取）、知识准入工作台、知识库资产。
 *
 * 关于「B4 未复位」的说明（2026-10-07 实测更正）
 * ---------------------------------------------
 * 本页此前把上传入口标成「依赖 RAGFlow 独立栈、B4 未复位完成」，**该说法与实测不符**：
 *  ① 网关此前**只有检索侧工具**（search/health/documents/retire/citation_list），
 *     没有任何写入工具——上传功能不是被底座限制，是后端从未实现；
 *  ② 底层 19380 入口的检索 / 文档列表 / 解析接口实测全部 `code=0` 可用，
 *     bge-m3 embedding 链路也通（容器内实测 0.19~0.35s、维度 1024）。
 * 现已补齐 `knowledge_upload` / `knowledge_document_status` / `knowledge_delete`
 * 三个工具与对应 BFF 路由，上传 → 解析 → 检索命中 → 撤库已端到端验证通过。
 *
 * 仍未完成的是**部署形态独立**（19380 背后是 nginx 反代，backend 仍是旧绑定栈
 * 的 ragflow-cpu，见 `gateway/README.md`）。那影响的是"底座归属"，
 * 不影响本功能可用性，因此这里不再以它为理由限制功能。
 */

/** 允许入库的扩展名，与网关侧 `knowledge.ALLOWED_SUFFIXES` 保持一致。 */
const ALLOWED = ['.md', '.markdown', '.txt', '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.csv', '.html', '.htm']
/** 单文件上限，与网关侧 `KNOWLEDGE_MAX_UPLOAD_MB` 默认值一致。 */
const MAX_MB = 32

/** File → base64。用浏览器内置 FileReader，不引任何依赖。 */
function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const fr = new FileReader()
    fr.onload = () => {
      const s = String(fr.result || '')
      const b64 = s.includes(',') ? s.slice(s.indexOf(',') + 1) : s
      resolve(b64)
    }
    fr.onerror = () => reject(fr.error || new Error('读取文件失败'))
    fr.readAsDataURL(file)
  })
}

/**
 * 毫秒时间戳 → `YYYY-MM-DD HH:mm:ss`。
 * RAGFlow 的 `created_at` / `updated_at` 是 13 位毫秒数，直接渲染成人看不懂
 * （页面上会出现 1798744473591 这种裸数字）。秒级以下单位也一并兜住。
 */
function fmtTime(v: unknown): string {
  const n = Number(v)
  if (!v || !Number.isFinite(n) || n <= 0) return '-'
  const ms = n < 1e12 ? n * 1000 : n
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return String(v)
  const p = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 解析状态 → 中文 + 样式类。RAGFlow 的 run 取值：UNSTART/RUNNING/DONE/FAIL/CANCELLED。 */
function runView(run: unknown, progress: unknown) {
  const r = String(run || '').toUpperCase()
  const pct = typeof progress === 'number' ? Math.round(progress * 100) : null
  if (r === 'DONE') return { label: '已解析', cls: 'chip chip-green' }
  if (r === 'RUNNING') return { label: pct != null ? `解析中 ${pct}%` : '解析中', cls: 'chip chip-blue' }
  if (r === 'FAIL') return { label: '解析失败', cls: 'chip chip-red' }
  if (r === 'UNSTART') return { label: '待解析', cls: 'chip chip-muted' }
  if (r === 'CANCELLED') return { label: '已取消', cls: 'chip chip-muted' }
  return { label: r || '未知', cls: 'chip chip-muted' }
}

export default function PageKnowledge({ demandId }: { demandId: string | null }) {
  const [health, setHealth] = useState<Record<string, any> | null>(null)
  const [docs, setDocs] = useState<Record<string, any> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const [q, setQ] = useState('')
  const [search, setSearch] = useState<Record<string, any> | null>(null)
  const [searchBusy, setSearchBusy] = useState(false)

  const [cites, setCites] = useState<Record<string, any> | null>(null)
  const [citeBusy, setCiteBusy] = useState(false)

  // 知识准入
  const fileRef = useRef<HTMLInputElement>(null)
  // 记住本次要传的文件：撤掉重名那份后要能一键重传，
  // 而 input 在 doUpload 结尾已被清空，拿不到 files 了。
  const pendingFileRef = useRef<File | null>(null)
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadMsg, setUploadMsg] = useState<{
    kind: 'ok' | 'err'
    text: string
    action?: { label: string; onClick: () => void }
  } | null>(null)
  const [polling, setPolling] = useState(false)
  const pollRef = useRef<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [h, d] = await Promise.all([api.knowledgeHealth(), api.knowledgeDocuments(50)])
      setHealth(h)
      setDocs(d)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  /** 轮询解析进度：有RUNNING/UNSTART 就继续查，全部 DONE 后停。 */
  const pollOnce = useCallback(async () => {
    try {
      const st = await api.knowledgeStatus('', 50)
      const list: any[] = (st?.documents || []) as any[]
      setDocs((prev: any) => ({ ...(prev || {}), documents: list, total: list.length }))
      const pending = list.some((d) => ['RUNNING', 'UNSTART', ''].includes(String(d.run || '').toUpperCase()))
      return pending
    } catch {
      return false
    }
  }, [])

  const stopPoll = useCallback(() => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    setPolling(false)
  }, [])

  const startPoll = useCallback(() => {
    if (pollRef.current != null) window.clearInterval(pollRef.current)
    setPolling(true)
    pollRef.current = window.setInterval(async () => {
      const stillPending = await pollOnce()
      if (!stillPending) {
        stopPoll()
        load()
      }
    }, 2500)
  }, [pollOnce, stopPoll, load])

  useEffect(() => () => stopPoll(), [stopPoll])

  const doUpload = async (file: File) => {
    const name = file.name || ''
    pendingFileRef.current = file
    const lower = name.toLowerCase()
    const suffix = lower.slice(lower.lastIndexOf('.'))
    if (!ALLOWED.includes(suffix)) {
      setUploadMsg({ kind: 'err', text: `不支持的类型 ${suffix || '(无扩展名)'}；允许：${ALLOWED.join('、')}` })
      return
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      setUploadMsg({ kind: 'err', text: `文件 ${(file.size / 1024 / 1024).toFixed(1)}MB 超过上限 ${MAX_MB}MB` })
      return
    }
    setUploadBusy(true)
    setUploadMsg(null)
    setError(null)
    try {
      const b64 = await readAsBase64(file)
      const r: any = await api.knowledgeUpload({
        filename: name,
        content_base64: b64,
        content_type: file.type || '',
        actor: 'web-ui',
      })
      if (r?.ok === false) {
        // 重名：库里已有同一份口径，后端会拦下（否则 RAGFlow 静默改名成
        // `x(1).md`，两版口径并存、检索时互相矛盾）。这里直接给一键撤库入口。
        if (r.duplicate) {
          setUploadMsg({
            kind: 'err',
            text: `${String(r.error || '库中已有同名文档')}${
              r.document_id ? `（文档 id ${r.document_id}）` : ''
            }`,
            action: r.document_id
              ? {
                  label: '撤掉库中那份，再重新上传',
                  onClick: () => {
                    const f = pendingFileRef.current
                    if (!f) return
                    void doDelete(r.document_id, name).then(() => doUpload(f))
                  },
                }
              : undefined,
          })
        } else {
          setUploadMsg({ kind: 'err', text: String(r.error || '上传失败') })
        }
      } else {
        setUploadMsg({
          kind: 'ok',
          text: `已入库《${r?.name || name}》，正在解析（约需数秒到数十秒，完成后即可被检索命中）`,
        })
        startPoll()
      }
    } catch (e) {
      setUploadMsg({ kind: 'err', text: `上传失败：${String((e as any)?.message || e)}` })
    } finally {
      setUploadBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const doDelete = async (documentId: string, name: string) => {
    if (!window.confirm(`确认从知识库撤库《${name}》？\n撤库后该文档不再被检索引用（需求单与附件不受影响）。`)) return
    setUploadMsg(null)
    try {
      const r: any = await api.knowledgeDelete({ document_id: documentId, actor: 'web-ui' })
      if (r?.ok === false) {
        setUploadMsg({ kind: 'err', text: String(r.error || '撤库失败') })
      } else {
        setUploadMsg({ kind: 'ok', text: `已撤库《${name}》` })
        load()
      }
    } catch (e) {
      setUploadMsg({ kind: 'err', text: `撤库失败：${String((e as any)?.message || e)}` })
    }
  }

  const doSearch = async () => {
    if (!q.trim()) return
    setSearchBusy(true)
    setError(null)
    try {
      const r = await api.knowledgeSearch({ question: q.trim(), top_k: 5 })
      setSearch(r)
    } catch (e) {
      setError(e)
    } finally {
      setSearchBusy(false)
    }
  }

  const loadCites = async () => {
    setCiteBusy(true)
    setError(null)
    try {
      const r = await api.knowledgeCitations({ demand_id: demandId || '', limit: 30 })
      setCites(r)
    } catch (e) {
      setError(e)
    } finally {
      setCiteBusy(false)
    }
  }

  const items: any[] = (docs?.documents || docs?.items || []) as any[]
  const hits: any[] = (search?.citations || []) as any[]
  const citeItems: any[] = (cites?.items || []) as any[]

  return (
    <div className="page">
      <Card
        title="知识库状态"
        extra={
          <button onClick={load} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
        }
      >
        <ErrorBox error={error} />
        {loading ? <Loading tip="正在检查知识库连通性…" /> : null}
        <div className="trace">
          <div>
            <span className="trace-k">连通性</span>
            <code>{health?.ok === false ? '异常' : health?.status || (health ? '正常' : '-')}</code>
          </div>
          <div>
            <span className="trace-k">已解析文档</span>
            <code>{health?.document_count ?? items.length ?? '-'}</code>
          </div>
        </div>
        {health ? (
          <details className="fold">
            <summary>健康详情原文（排障用）</summary>
            <pre className="sqlbox small">{JSON.stringify(health, null, 2)}</pre>
          </details>
        ) : null}
      </Card>

      <Card title="知识收集">
        <div className="uploadbar">
          <input
            ref={fileRef}
            type="file"
            accept={ALLOWED.join(',')}
            disabled={uploadBusy}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) doUpload(f)
            }}
          />
          <button className="primary" disabled={uploadBusy} onClick={() => fileRef.current?.click()}>
            {uploadBusy ? '上传中…' : '选择文件并上传'}
          </button>
          {polling ? <span className="muted">正在轮询解析进度…</span> : null}
        </div>
        <p className="muted">
          支持 {ALLOWED.join('、')}，单个文件 ≤ {MAX_MB}MB。上传后自动触发解析，
          解析完成（状态显示「已解析」）即可被知识检索命中。
        </p>
        {uploadMsg ? (
          <div className={uploadMsg.kind === 'ok' ? 'okbox' : 'errbox'}>
            {uploadMsg.kind === 'ok' ? (
              <div className="okbox-title">已提交</div>
            ) : (
              <div className="errbox-title">{uploadMsg.text}</div>
            )}
            {uploadMsg.kind === 'ok' ? <div>{uploadMsg.text}</div> : null}
            {uploadMsg.action ? (
              <div style={{ marginTop: 6 }}>
                <button onClick={uploadMsg.action.onClick}>{uploadMsg.action.label}</button>
              </div>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card
        title="知识库资产"
        extra={
          loading ? (
            <span className="muted">加载中…</span>
          ) : (
            <button onClick={load}>刷新</button>
          )
        }
      >
        {items.length === 0 && !loading ? <p className="muted">暂无已入库文档。</p> : null}
        {items.length > 0 ? (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>文档</th>
                  <th>解析状态</th>
                  <th>分块数</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((d: any, i: number) => {
                  const rv = runView(d.run, d.progress)
                  const docId = d.id || d.document_id || ''
                  const docName = d.name || d.document_name || d.title || '-'
                  return (
                    <tr key={d.id || i}>
                      <td>{docName}</td>
                      <td>
                        <span className={rv.cls}>{rv.label}</span>
                      </td>
                      <td>{d.chunk_count ?? d.chunks ?? '-'}</td>
                      <td className="mono">{fmtTime(d.updated_at || d.created_at)}</td>
                      <td>
                        {docId ? (
                          <button className="linkbtn" onClick={() => doDelete(docId, docName)}>
                            撤库
                          </button>
                        ) : (
                          <span className="muted">-</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </Card>

      <Card
        title="知识检索"
        extra={
          <button className="primary" onClick={doSearch} disabled={searchBusy || !q.trim()}>
            {searchBusy ? '检索中…' : '检索'}
          </button>
        }
      >
        <input
          className="textinput"
          placeholder="输入要查的口径问题，例如：销量和销售额有什么区别"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') doSearch()
          }}
        />
        {searchBusy ? <Loading tip="正在检索知识库…" /> : null}
        {search?.error ? <div className="errbox"><div className="errbox-title">{String(search.error)}</div></div> : null}

        {hits.length > 0 ? (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>来源文档</th>
                  <th>相似度</th>
                  <th>片段</th>
                </tr>
              </thead>
              <tbody>
                {hits.map((c: any, i: number) => (
                  <tr key={i}>
                    <td>{c.document_name || '-'}</td>
                    <td className="mono">{c.similarity ?? '-'}</td>
                    <td className="snippet">{c.content || c.snippet || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {search && hits.length === 0 && !search?.error ? (
          <p className="muted">没有命中，试着换个说法或降低相似度要求。</p>
        ) : null}
      </Card>

      <Card
        title="引用溯源（知识引用留痕）"
        extra={
          <button onClick={loadCites} disabled={citeBusy}>
            {citeBusy ? '加载中…' : '加载'}
          </button>
        }
      >
        <p className="muted">
          {demandId ? `当前需求：${demandId}` : '未选择需求，将返回跨需求的最近引用记录。'}
        </p>
        {citeItems.length > 0 ? (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>需求</th>
                  <th>文档</th>
                  <th>相似度</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {citeItems.map((c: any, i: number) => (
                  <tr key={i}>
                    <td className="mono">{c.demand_id || '-'}</td>
                    <td>{c.document_name || '-'}</td>
                    <td className="mono">{c.similarity ?? '-'}</td>
                    <td className="mono">{fmtTime(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">暂无引用记录，点「加载」获取。</p>
        )}
      </Card>
    </div>
  )
}
