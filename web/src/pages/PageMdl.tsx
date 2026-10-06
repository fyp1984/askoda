import { useCallback, useEffect, useState } from 'react'
import { api, type MdlResp, type MdlTable } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 菜单① · 语义层 · MDL 字典（工作台默认首页）
 *
 * PRD §12.7：语义引擎状态（引擎版本 / dry-run 自检 / 重新部署）、MDL 版本与变更历史、
 * 模型资产（字段可见性 / 计算口径 / 关系与枚举）。
 *
 * 界面原则 1：口径与字段的准入状态用三色标识（已发布 / 待审核 / 不可见），
 * 让「AI 能看懂什么」一眼可见。
 */
export default function PageMdl({ dataset, onNotify }: { dataset: string; onNotify?: (msg: string) => void }) {
  const [manifest, setManifest] = useState<Record<string, any> | null>(null)
  const [mdl, setMdl] = useState<MdlResp | null>(null)
  const [glossary, setGlossary] = useState<Record<string, any> | null>(null)
  const [includeHidden, setIncludeHidden] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [picked, setPicked] = useState<string>('')

  const [sql, setSql] = useState(
    'SELECT d.store_name, SUM(s.sales_qty) AS total_qty FROM dws_store_daily_agg s JOIN dim_store d ON d.store_id = s.store_id GROUP BY d.store_name',
  )
  const [dry, setDry] = useState<Record<string, any> | null>(null)
  const [dryBusy, setDryBusy] = useState(false)
  const [collectBusy, setCollectBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [m, d, g] = await Promise.all([
        api.semanticManifest(dataset),
        api.semanticMdl({ dataset, include_hidden: includeHidden }),
        api.semanticGlossary({}),
      ])
      setManifest(m)
      setMdl(d)
      setGlossary(g)
      const tables = (d as MdlResp)?.tables || []
      if (tables.length > 0) {
        setPicked((p) => (tables.some((t) => t.table_name === p) ? p : tables[0].table_name))
      }
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [dataset, includeHidden])

  useEffect(() => {
    load()
  }, [load])

  const runDry = async () => {
    setDryBusy(true)
    setError(null)
    try {
      const r = await api.semanticDryRun(sql, dataset)
      setDry(r)
    } catch (e) {
      setError(e)
    } finally {
      setDryBusy(false)
    }
  }

  const recollect = async () => {
    setCollectBusy(true)
    setError(null)
    try {
      const r = await api.semanticCollect(dataset, 'native')
      onNotify?.(`已重采集：MDL ${r?.mdl_seed?.columns ?? '-'} 列 / 物理 ${r?.import?.columns ?? '-'} 列`)
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setCollectBusy(false)
    }
  }

  const tables: MdlTable[] = mdl?.tables || []
  const current = tables.find((t) => t.table_name === picked) || null
  const cols = current?.columns || []

  return (
    <div className="page">
      <Card
        title={`语义引擎状态 · ${dataset} 库`}
        extra={
          <>
            <button onClick={load} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
            <button onClick={recollect} disabled={collectBusy}>
              {collectBusy ? '重采集中…' : '重新采集元数据'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        {loading ? <Loading tip="正在读取语义层清单与模型资产…" /> : null}

        <div className="trace">
          <div>
            <span className="trace-k">数据集</span>
            <code>{dataset}</code>
          </div>
          <div>
            <span className="trace-k">模型数</span>
            <code>{manifest?.models ?? manifest?.model_count ?? tables.length ?? '-'}</code>
          </div>
          <div>
            <span className="trace-k">可见字段</span>
            <code>
              {tables.reduce((n, t) => n + (t.columns || []).filter((c) => c.ai_visible !== false).length, 0) || '-'}
            </code>
          </div>
          <div>
            <span className="trace-k">关系数</span>
            <code>{manifest?.relationships ?? '-'}</code>
          </div>
        </div>

        {manifest ? (
          <details className="fold">
            <summary>引擎清单原文（排障用）</summary>
            <pre className="sqlbox small">{JSON.stringify(manifest, null, 2)}</pre>
          </details>
        ) : null}
      </Card>

      <Card
        title="MDL 模型资产"
        extra={
          <label className="ds-pick">
            <input
              type="checkbox"
              checked={includeHidden}
              onChange={(e) => setIncludeHidden(e.target.checked)}
            />
            显示未建模列（AI 不可见）
          </label>
        }
      >
        {tables.length === 0 && !loading ? <p className="muted">暂无模型资产。</p> : null}

        {tables.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>表名</th>
                    <th>中文名 / 域</th>
                    <th>颗粒度</th>
                    <th>可见列</th>
                    <th>未建模列</th>
                    <th>行数估计</th>
                  </tr>
                </thead>
                <tbody>
                  {tables.map((t) => (
                    <tr key={t.table_name} className={t.table_name === picked ? 'row-ok' : ''}>
                      <td className="mono">{t.table_name}</td>
                      <td>{t.table_label || t.domain || '-'}</td>
                      <td className="snippet">{t.grain || '-'}</td>
                      <td>{t.column_count ?? (t.columns || []).length}</td>
                      <td>{t.hidden_column_count ?? 0}</td>
                      <td>{t.row_estimate ?? '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h4 className="sec">字段清单</h4>
            <label className="ds-pick">
              选择表
              <select value={picked} onChange={(e) => setPicked(e.target.value)}>
                {tables.map((t) => (
                  <option key={t.table_name} value={t.table_name}>
                    {t.table_name}
                  </option>
                ))}
              </select>
            </label>

            {cols.length > 0 ? (
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      <th>字段</th>
                      <th>中文名</th>
                      <th>类型</th>
                      <th>准入状态</th>
                      <th>口径 / 说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cols.map((c) => {
                      const visible = c.ai_visible !== false
                      return (
                        <tr key={c.column_name} className={visible ? 'row-ok' : 'row-bad'}>
                          <td className="mono">
                            {c.column_name}
                            {c.is_primary_key ? <span className="chip chip-muted">PK</span> : null}
                          </td>
                          <td>{c.column_label || '-'}</td>
                          <td className="mono">{c.data_type || '-'}</td>
                          <td>
                            {visible ? (
                              <span className="chip chip-green">已发布 · AI 可见</span>
                            ) : (
                              <span className="chip chip-muted">不可见 · 未建模</span>
                            )}
                            {c.is_sensitive ? <span className="chip chip-red">敏感</span> : null}
                          </td>
                          <td className="snippet">{c.description || '-'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">该表暂无字段。</p>
            )}
          </>
        ) : null}
      </Card>

      <Card
        title="语义层自检（dry-run，不执行）"
        extra={
          <button className="primary" onClick={runDry} disabled={dryBusy}>
            {dryBusy ? '预演中…' : '预演'}
          </button>
        }
      >
        <p className="muted">
          预演只校验「SQL 引用的对象是否都在 MDL 可见闭集内」，不会真正执行、不返回数据。
        </p>
        <textarea className="sqlinput" rows={4} value={sql} onChange={(e) => setSql(e.target.value)} />
        {dry ? (
          <>
            <p>
              预演结果：
              {dry?.ok === false ? (
                <span className="chip chip-red">未通过</span>
              ) : (
                <span className="chip chip-green">通过</span>
              )}
            </p>
            {dry?.error ? <div className="errbox"><div className="errbox-title">{String(dry.error)}</div></div> : null}
            <pre className="sqlbox small">{JSON.stringify(dry, null, 2)}</pre>
          </>
        ) : null}
      </Card>

      <Card title="业务术语与口径">
        {glossary ? (
          <pre className="sqlbox small">{JSON.stringify(glossary, null, 2)}</pre>
        ) : (
          <p className="muted">暂无术语条目。</p>
        )}
      </Card>
    </div>
  )
}
