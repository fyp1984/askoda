import { useCallback, useEffect, useState } from 'react'
import { api, DEMAND_STATUSES, type DemandListItem } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'

/**
 * 需求列表（找回需求）
 *
 * 为什么必须有这一页：需求编号原先只存在 React useState 里，刷新即丢，
 * 业务人员离开页面后自己提交过的需求一条都找不回来。
 * 本页把服务端已有的 demand_list 暴露成入口，点击任意一条即把
 * demandId 交给上层，后续四页流程（读懂你的需求 / 对一对关键说法 / 让系统写出 SQL / 出数与交付）复用同一条需求。
 */
const PAGE_SIZE = 20

export default function PageDemandList({
  currentId,
  onPick,
  actor = '',
  onNotify,
  onPurged,
}: {
  currentId: string | null
  onPick: (id: string) => void
  actor?: string
  onNotify?: (msg: string) => void
  onPurged?: (ids: string[]) => void
}) {
  const [items, setItems] = useState<DemandListItem[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [purging, setPurging] = useState(false)

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

  const toggle = (id: string) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  const allPicked = items.length > 0 && items.every((it) => picked.includes(it.demand_id))
  const toggleAll = () =>
    setPicked(allPicked ? [] : items.map((it) => it.demand_id))

  /** 两步清理：先 dry-run 把影响面摊开，用户确认后才真删。
   *  「删除」是唯一不可逆动作，不允许一次点击完成——连弹窗都不止一层。 */
  const purge = async () => {
    if (!picked.length) return
    setPurging(true)
    setError(null)
    try {
      const preview = await api.purgeDemands({ demand_ids: picked, dry_run: true })
      const w = preview.would_delete || {}
      const lines = Object.entries(w)
        .filter(([, n]) => Number(n) > 0)
        .map(([t, n]) => `${TABLE_LABEL[t] || t} ${n} 行`)
        .join('\n')
      const ok = window.confirm(
        `将永久删除 ${w.demand_requests ?? picked.length} 条需求及其过程数据：\n\n${lines || '（无关联数据）'}\n\n` +
          '删除后不可恢复，附件对象不在此处清理。确定继续？',
      )
      if (!ok) return
      const r = await api.purgeDemands({ demand_ids: picked, actor })
      onNotify?.(r.notice || '已清理')
      onPurged?.(picked)
      setPicked([])
      setOffset(0)
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setPurging(false)
    }
  }

  return (
    <div className="page">
      <Card
        title="我的需求"
        collapsible
        id="list-main"
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
                : '请到「提个需求」页填写需求表单；提交成功后需求编号会出现在这里，刷新页面也不会丢。'}
            </div>
          </div>
        ) : (
          <>
            <p className="muted">
              共 {total} 条，当前显示第 {from}–{to} 条。点击任意一条即可继续它的分析流程。
            </p>
            {picked.length > 0 ? (
              <div className="warnbox">
                <div className="warnbox-title">已勾选 {picked.length} 条</div>
                <div className="warnbox-hint">
                  清理会同时删除这些需求的分析轮次、口径确认、结构化需求、取数记录与知识引用，且不可恢复。
                </div>
                <div className="pager">
                  <button onClick={purge} disabled={purging}>
                    {purging ? '清理中…' : `清理这 ${picked.length} 条`}
                  </button>
                  <button onClick={() => setPicked([])} disabled={purging}>
                    取消选择
                  </button>
                </div>
              </div>
            ) : null}
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>
                      <input
                        type="checkbox"
                        aria-label="全选本页"
                        checked={allPicked}
                        onChange={toggleAll}
                      />
                    </th>
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
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择 ${it.demand_id}`}
                          checked={picked.includes(it.demand_id)}
                          onChange={() => toggle(it.demand_id)}
                        />
                      </td>
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

/** 清理预览里的表名 → 人话（用户在确认框里看到的不能是表名）。 */
const TABLE_LABEL: Record<string, string> = {
  demand_requests: '需求单',
  demand_events: '流转事件',
  analysis_rounds: '分析轮次',
  confirmations: '口径确认',
  structured_requirements: '结构化需求',
  sql_runs: '取数记录',
  knowledge_citations: '知识引用',
}