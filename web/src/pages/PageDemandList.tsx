import { useCallback, useEffect, useState } from 'react'
import { api, DEMAND_STATUSES, type DemandListItem } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 需求列表（找回需求）
 *
 * 为什么必须有这一页：需求编号原先只存在 React useState 里，刷新即丢，
 * 业务人员离开页面后自己提交过的需求一条都找不回来。
 * 本页把服务端已有的 demand_list 暴露成入口，点击任意一条即把
 * demandId 交给上层，后续四页流程（分析 / 口径确认 / SQL / 交付）复用同一条需求。
 */
const PAGE_SIZE = 20

export default function PageDemandList({
  currentId,
  onPick,
}: {
  currentId: string | null
  onPick: (id: string) => void
}) {
  const [items, setItems] = useState<DemandListItem[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.demandList({
        status: status || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setItems(r.items || [])
      setTotal(r.total ?? 0)
    } catch (e) {
      setItems([])
      setTotal(0)
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [status, offset])

  useEffect(() => {
    load()
  }, [load])

  const from = total === 0 ? 0 : offset + 1
  const to = offset + items.length

  return (
    <div className="page">
      <Card
        title="我的需求"
        extra={
          <>
            <label className="hint-inline">
              状态筛选
              <select
                className="status-pick"
                value={status}
                onChange={(e) => {
                  setOffset(0)
                  setStatus(e.target.value)
                }}
              >
                <option value="">全部</option>
                {DEMAND_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <button onClick={load} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />

        {loading ? (
          <Loading tip="正在取需求列表…" />
        ) : items.length === 0 ? (
          <div className="emptybox">
            <div className="emptybox-title">
              {status ? `没有状态为「${status}」的需求` : '还没有任何需求'}
            </div>
            <div className="emptybox-hint">
              {status
                ? '试试把筛选切回「全部」，或换一个状态。'
                : '请到「提交需求」页填写需求表单；提交成功后需求编号会出现在这里，刷新页面也不会丢。'}
            </div>
          </div>
        ) : (
          <>
            <p className="muted">
              共 {total} 条，当前显示第 {from}–{to} 条。点击任意一条即可继续它的分析流程。
            </p>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>需求编号</th>
                    <th>标题</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.demand_id} className={it.demand_id === currentId ? 'row-cur' : undefined}>
                      <td className="mono">
                        {it.demand_id}
                        {it.demand_id === currentId ? <span className="chip chip-muted">当前</span> : null}
                      </td>
                      <td className="ev-content">{it.title || '-'}</td>
                      <td>
                        <span className={`chip ${statusChip(it.status)}`}>{it.status || '-'}</span>
                      </td>
                      <td className="mono">{it.created_at || '-'}</td>
                      <td>
                        <button className="primary" onClick={() => onPick(it.demand_id)}>
                          继续处理
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="pager">
              <button disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                上一页
              </button>
              <span className="hint-inline">
                第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(total / PAGE_SIZE))} 页
              </span>
              <button
                disabled={to >= total || loading}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                下一页
              </button>
            </div>
          </>
        )}
      </Card>
    </div>
  )
}

/** 状态配色：终态（已通过）绿、退回红、其余按流程阶段给中性色。 */
function statusChip(status?: string): string {
  if (status === '已通过') return 'chip-green'
  if (status === '已退回') return 'chip-red'
  return 'chip-muted'
}