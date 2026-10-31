import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import RichText from '../components/RichText'
// 缺因是 {code, detail} 对象，原样 String() 会渲染成 [object Object]；
// 统一走 display 层转成人话（含未知错误码的去码兜底）。
import { missReasonText } from '../lib/display'

/**
 * 菜单③ · 数据源接入
 *
 * PRD §12.7：数据库接入（Wren Engine 只读探测）、Schema 差异与 MDL 候选、表结构与数据字典。
 * 关键交互：差异自动生成 MDL 修改候选；审核 deploy 前不可被 SQL 引用。
 */
export default function PageDatasource({
  dataset,
  demandId,
  onNotify,
}: {
  dataset: string
  demandId: string | null
  onNotify?: (msg: string) => void
}) {
  const [version, setVersion] = useState<Record<string, any> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const [scan, setScan] = useState<Record<string, any> | null>(null)
  const [scanBusy, setScanBusy] = useState(false)

  const [cand, setCand] = useState<Record<string, any> | null>(null)
  const [candBusy, setCandBusy] = useState(false)
  const [candDemand, setCandDemand] = useState('')

  const loadVersion = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setVersion(await api.datasourceVersion(dataset))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [dataset])

  useEffect(() => {
    loadVersion()
  }, [loadVersion])

  useEffect(() => {
    if (demandId) setCandDemand(demandId)
  }, [demandId])

  const doScan = async () => {
    setScanBusy(true)
    setError(null)
    try {
      const r = await api.datasourceScan(dataset, true)
      setScan(r)
      onNotify?.(`扫描完成，schema_version=${r?.schema_version ?? '-'}`)
      await loadVersion()
    } catch (e) {
      setError(e)
    } finally {
      setScanBusy(false)
    }
  }

  const loadCandidates = async () => {
    if (!candDemand.trim()) return
    setCandBusy(true)
    setError(null)
    try {
      setCand(await api.datasourceCandidates(candDemand.trim(), dataset))
    } catch (e) {
      setError(e)
    } finally {
      setCandBusy(false)
    }
  }

  const tables: any[] = (scan?.tables || []) as any[]

  // 候选命中判定：以 matched 为准，缺该键（旧版网关）时退回「有没有主题表」。
  // 不能用 miss_reason 非空来判未命中 —— 它承载的是「说明」而非「结论」，
  // 旧逻辑会让「命中 6 张表」的需求照样弹出「未命中候选」告警。
  const candTables: any[] = (cand?.subject_tables || []) as any[]
  const candMatched: boolean = cand ? (cand.matched ?? candTables.length > 0) : false
  const candNotes: string[] = cand ? ((cand.notes || []) as any[]).map(String) : []
  const candMiss: string = cand ? String(cand.miss_reason || '') : ''

  return (
    <div className="page">
      <Card
        title={`数据源接入 · ${dataset} 库`}
        collapsible
        id="ds-status"
        extra={
          <>
            <button onClick={loadVersion} disabled={loading}>
              {loading ? '刷新中…' : '刷新版本'}
            </button>
            <button className="primary" onClick={doScan} disabled={scanBusy}>
              {scanBusy ? '扫描中…' : '扫描 Schema'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        {loading ? <Loading tip="正在读取 Schema 快照…" /> : null}
        {scanBusy ? <Loading tip="正在只读探测数据源结构…" /> : null}

        <div className="trace">
          <div>
            <span className="trace-k">schema_version</span>
            <code>{version?.schema_version || scan?.schema_version || '-'}</code>
          </div>
          <div>
            <span className="trace-k">表数</span>
            <code>{version?.table_count ?? scan?.table_count ?? tables.length ?? '-'}</code>
          </div>
          <div>
            <span className="trace-k">字段数</span>
            <code>{version?.column_count ?? scan?.column_count ?? '-'}</code>
          </div>
        </div>
        <p className="muted">
          版本号对应这次扫描的字段清单；库结构没变，版本号就不变。
        </p>
      </Card>

      <Card title="表结构与数据字典（扫描结果）" collapsible id="ds-tables">
        {tables.length === 0 ? (
          <p className="muted">还没有扫描结果，点上方「扫描 Schema」。</p>
        ) : (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>表</th>
                  <th>字段数</th>
                  <th>行数估计</th>
                  <th>字段清单</th>
                </tr>
              </thead>
              <tbody>
                {tables.map((t: any, i: number) => (
                  <tr key={i}>
                    <td className="mono">{t.table_name || t.name}</td>
                    <td>{(t.columns || []).length}</td>
                    <td>{t.row_estimate ?? '-'}</td>
                    <td className="snippet">
                      {(t.columns || []).map((c: any) => c.name || c.column_name).join('、')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {scan ? (
          <details className="fold">
            <summary>扫描结果原文（排障用）</summary>
            <pre className="sqlbox small">{JSON.stringify(scan, null, 2)}</pre>
          </details>
        ) : null}
      </Card>

      <Card
        title="Schema 差异与 MDL 候选"
        collapsible
        id="ds-candidates"
        extra={
          <button onClick={loadCandidates} disabled={candBusy || !candDemand.trim()}>
            {candBusy ? '生成中…' : '生成候选'}
          </button>
        }
      >
        <p className="muted">
          候选只在 MDL 闭集内产生，不连物理库猜字段；未命中会给 miss_reason，不猜不填。
        </p>
        <label className="ds-pick">
          <span className="ds-lbl">需求单号</span>
          <input
            className="textinput"
            placeholder="DR-…"
            value={candDemand}
            onChange={(e) => setCandDemand(e.target.value)}
          />
        </label>
        {candBusy ? <Loading tip="正在生成主题表/关联/时间字段候选…" /> : null}
        {cand ? (
          <>
            {cand?.ok === false ? (
              <div className="errbox">
                <div className="errbox-title">{<RichText text={String(cand.error || '生成失败')} />}</div>
                {cand.hint ? (
                  <div className="errbox-hint">{<RichText text={String(cand.hint)} />}</div>
                ) : null}
              </div>
            ) : null}

            {cand?.ok === false ? null : candMatched ? (
              <div className="okbox">
                <div className="okbox-title">
                  已匹配到 {candTables.length} 张主题表
                  {candTables[0]?.table ? `（首张 ${candTables[0].table}）` : ''}
                </div>
                <div className="okbox-msg">
                  {candTables.map((t: any) => t.table || t.model).filter(Boolean).join('、')}
                </div>
              </div>
            ) : (
              <div className="warnbox">
                <div className="warnbox-title">未命中候选</div>
                <div className="warnbox-hint">
                  {<RichText text={missReasonText(candMiss) || '未命中候选'} />}
                </div>
              </div>
            )}

            {/* 未命中时 miss_reason 已经把输入缺失原因拼进正文，notes 再列一遍就是重复 */}
            {candMatched && candNotes.length > 0 ? (
              <p className="muted">
                另需注意：{candNotes.join('；')}
              </p>
            ) : null}

            <details className="fold">
              <summary>候选原文（排障用）</summary>
              <pre className="sqlbox small">{JSON.stringify(cand, null, 2)}</pre>
            </details>
          </>
        ) : null}
      </Card>
    </div>
  )
}
