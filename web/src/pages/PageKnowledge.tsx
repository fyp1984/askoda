import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 菜单② · 知识储备
 *
 * PRD §12.7：知识收集（手动上传 / 自动获取）、知识准入工作台、知识库资产。
 *
 * 现状说明：上传与准入依赖 RAGFlow 独立栈（B4 未复位），故本页先交付
 * 「知识库资产 + 检索引用 + 引用溯源」三项已可用能力；上传准入入口保留并标注受限。
 */
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
        <div className="warnbox">
          <div className="warnbox-title">上传 / 自动获取 · 当前受限</div>
          <div className="warnbox-hint">
            知识上传与准入工作台依赖 RAGFlow 独立栈（B4 未复位完成）。
            检索、资产查看与引用溯源不受影响，可正常使用。
          </div>
        </div>
      </Card>

      <Card title="知识库资产">
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
                </tr>
              </thead>
              <tbody>
                {items.map((d: any, i: number) => (
                  <tr key={i}>
                    <td>{d.name || d.document_name || d.title || '-'}</td>
                    <td>
                      {d.status === 'done' || d.parsed ? (
                        <span className="chip chip-green">{d.status || '已解析'}</span>
                      ) : (
                        <span className="chip chip-muted">{d.status || '未知'}</span>
                      )}
                    </td>
                    <td>{d.chunk_count ?? d.chunks ?? '-'}</td>
                    <td className="mono">{d.updated_at || d.created_at || '-'}</td>
                  </tr>
                ))}
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
                    <td className="mono">{c.created_at || '-'}</td>
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
