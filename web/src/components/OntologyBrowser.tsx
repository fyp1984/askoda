import { useCallback, useEffect, useState } from 'react'
import { api, type OntologyBrowseResp } from '../api/client'
import { ErrorBox, Loading } from './ui'

/**
 * 本体可视化浏览（FR-2.6 · P2）
 *
 * 导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条在 Demo 中照常呈现**。
 * 验收要点是「从业务域可下钻到实体 → 表 → 字段 → 引用该字段的 CASE，**路径可点击**」。
 *
 * 因此本组件刻意不做成四棵静态树，而是**一条可点击的面包屑 + 当前层的下钻列表**：
 * 每次点击都把选中层级传给 `ontology_browse`，由后端返回该层的下一层；
 * 路径段（业务域 / 实体 / 字段）自身也是按钮，点任意一段即回退到那一层——
 * 「路径可点击」是字面要求，不是装饰。
 */
export default function OntologyBrowser({ dataset }: { dataset: string }) {
  const [domain, setDomain] = useState('')
  const [table, setTable] = useState('')
  const [column, setColumn] = useState('')
  const [data, setData] = useState<OntologyBrowseResp | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      setData(await api.ontologyBrowse({ dataset, domain, table, column }))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [dataset, domain, table, column])

  // 换数据集时回到根层，避免上一库的选中项挂在新库名下
  useEffect(() => {
    setDomain('')
    setTable('')
    setColumn('')
  }, [dataset])

  useEffect(() => {
    void load()
  }, [load])

  /** 点击面包屑的某一段：清掉该段之后的选中项，回到那一层。 */
  const jump = (level: string) => {
    if (level === 'dataset') {
      setDomain('')
      setTable('')
      setColumn('')
    } else if (level === 'domain') {
      setTable('')
      setColumn('')
    } else if (level === 'entity') {
      setColumn('')
    }
  }

  const pick = (node: { key: string; table_name?: string; column_name?: string }) => {
    if (!domain) {
      setDomain(node.key)
      setTable('')
      setColumn('')
    } else if (!table) {
      setTable(node.table_name || node.key)
      setColumn('')
    } else {
      setColumn(node.column_name || node.key)
    }
  }

  const children = data?.children || []
  const cases = data?.cases || []
  const levelLabel: Record<string, string> = {
    domain: '业务域',
    entity: '实体 / 表',
    column: '字段',
  }

  return (
    <>
      <p className="muted">
        从业务域逐层下钻到实体（表）→ 字段 → 引用该字段的 CASE；路径每一段都可点击回退。
        数据来自本系统元数据字典，不额外接数据源。
      </p>

      <ErrorBox error={error} />

      <div className="ontology-path">
        {(data?.path || []).map((p, i) => (
          <span key={`${p.level}-${i}`}>
            {i > 0 ? <span className="ontology-sep">›</span> : null}
            <button
              type="button"
              className="linklike"
              onClick={() => jump(p.level)}
              disabled={i === (data?.path || []).length - 1}
            >
              {p.level === 'dataset' ? p.label : `${levelLabel[p.level] || ''} ${p.label}`.trim()}
            </button>
          </span>
        ))}
      </div>

      {busy ? <Loading tip="正在读取本体层级…" /> : null}

      {!busy && column && cases.length >= 0 ? (
        <>
          <h4 className="sec">引用字段「{column}」的 CASE（{cases.length} 条）</h4>
          {cases.length === 0 ? (
            <p className="muted">{data?.case_note}</p>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>执行号</th>
                    <th>需求</th>
                    <th>审查结论</th>
                    <th>是否采用</th>
                    <th>生成路径</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {cases.map((c) => (
                    <tr key={c.key} className={c.adopted ? 'row-ok' : ''}>
                      <td className="mono">{c.sql_run_id}</td>
                      <td className="snippet">
                        {c.demand_title || c.demand_id}
                        <span className="chip chip-muted">{c.demand_id}</span>
                      </td>
                      <td>
                        {c.review_status === '不通过' ? (
                          <span className="chip chip-red">{c.review_status}</span>
                        ) : (
                          <span className="chip chip-green">{c.review_status || '-'}</span>
                        )}
                      </td>
                      <td>{c.adopted ? '已交付' : '未采用'}</td>
                      <td className="mono">{c.generator_model || '-'}</td>
                      <td className="mono">{c.created_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : null}

      {!busy && !column ? (
        <>
          <h4 className="sec">
            {data?.level ? levelLabel[data.level] || data.level : '业务域'}（{children.length} 项）
          </h4>
          {children.length === 0 ? (
            <p className="muted">
              该层暂无可下钻项。元数据字典采集完成后，此处会逐层展开业务域 / 实体 / 字段。
            </p>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>{data?.level === 'domain' ? '业务域' : data?.level === 'entity' ? '实体 / 表' : '字段'}</th>
                    <th>{data?.level === 'domain' ? '挂载实体数' : '中文名 / 说明'}</th>
                    <th>颗粒度 / 类型</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {children.map((n) => (
                    <tr key={n.key} className={n.is_sensitive ? 'row-bad' : ''}>
                      <td className="mono">
                        {n.label}
                        {n.table_name ? <span className="chip chip-muted">{n.table_name}</span> : null}
                        {n.is_primary_key ? <span className="chip chip-muted">PK</span> : null}
                        {n.is_sensitive ? <span className="chip chip-red">敏感</span> : null}
                      </td>
                      <td className="snippet">
                        {n.table_count != null
                          ? `挂载 ${n.table_count} 张表`
                          : n.description || '-'}
                      </td>
                      <td className="snippet">{n.grain || n.data_type || '-'}</td>
                      <td>
                        <button type="button" onClick={() => pick(n)}>
                          下钻 →
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : null}
    </>
  )
}
