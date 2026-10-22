import { useState, type ReactNode } from 'react'

const PREFIX = 'askoda.collapse.'

function readOpen(key: string, defaultOpen: boolean): boolean {
  try {
    const v = window.localStorage.getItem(PREFIX + key)
    if (v === '0') return false
    if (v === '1') return true
  } catch {
    /* 隐私模式忽略 */
  }
  return defaultOpen
}

/**
 * 折叠状态 hook：按 key 持久化到 localStorage，刷新后保留用户的收起偏好。
 * 用于「卡片整块」「页面二级分区」「单个槽位卡」三种折叠粒度。
 */
export function useCollapse(key: string, defaultOpen = true) {
  const [open, setOpen] = useState(() => readOpen(key, defaultOpen))
  const toggle = () => {
    setOpen((o) => {
      const n = !o
      try {
        window.localStorage.setItem(PREFIX + key, n ? '1' : '0')
      } catch {
        /* 忽略 */
      }
      return n
    })
  }
  return { open, toggle }
}

/**
 * 页面内二级分区（六槽位 / 证据链 / 门禁逐层 / 结果集 …），带折叠/展开。
 *
 * 为什么做：长页面（证据链动辄 20+ 行）把真正要看的东西挤出视口。
 * 给每个分区一个可折叠头，用户能把不关注的收起来，只看当前要处理的。
 */
export function Section({
  title,
  count,
  desc,
  id,
  extra,
  defaultOpen = true,
  children,
}: {
  title: string
  count?: number | string
  desc?: string
  id?: string
  extra?: ReactNode
  defaultOpen?: boolean
  children: ReactNode
}) {
  const key = id || `sec:${title}`
  const { open, toggle } = useCollapse(key, defaultOpen)
  return (
    <div className={`sec-block${open ? '' : ' collapsed'}`}>
      <div className="sec-head">
        <button
          type="button"
          className="sec-toggle"
          onClick={toggle}
          aria-expanded={open}
          title={open ? '收起此区域' : '展开此区域'}
        >
          <span className="sec-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
          <span className="sec-title">{title}</span>
          {count !== undefined && count !== '' ? <span className="sec-count">{count}</span> : null}
        </button>
        <div className="sec-head-r">
          {desc ? <span className="sec-desc">{desc}</span> : null}
          {extra}
        </div>
      </div>
      {open ? <div className="sec-body">{children}</div> : null}
    </div>
  )
}

/**
 * 六槽位里的单条口径卡（subject / granularity / time / scope / fields / risks）。
 * 红色框圈住的就是这里的「槽位名」——每条都可单独折叠，
 * 用户只看当下要核的口径，其余一键收起，整页不拉得老长。
 */
export function SlotCard({
  name,
  needConfirm,
  answered,
  resolved,
  defaultOpen = true,
  children,
}: {
  name: string
  needConfirm?: boolean
  /** 已答复但系统还没回填（后端未自动判定）——避免与「对一对关键说法」页状态打架 */
  answered?: boolean
  /** 该口径已定稿（needs_confirmation=false 且有候选）——不再显示红色待确认 */
  resolved?: boolean
  defaultOpen?: boolean
  children: ReactNode
}) {
  const key = `slot:${name}`
  const { open, toggle } = useCollapse(key, defaultOpen)
  // 状态优先级：已定稿 > 已答复(未回填) > 待确认
  const chip = resolved ? (
    <span className="chip chip-green">已确认</span>
  ) : answered ? (
    <span className="chip chip-amber">已答复</span>
  ) : needConfirm ? (
    <span className="chip chip-red">待确认</span>
  ) : null
  const flag = !!needConfirm && !answered && !resolved
  return (
    <div className={`slot${flag ? ' slot-confirm' : ''}${open ? '' : ' collapsed'}`}>
      <button
        type="button"
        className="slot-toggle"
        onClick={toggle}
        aria-expanded={open}
        title={open ? '收起此口径' : '展开此口径'}
      >
        <span className="slot-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="slot-name">{name}</span>
        {chip}
      </button>
      {open ? <div className="slot-body">{children}</div> : null}
    </div>
  )
}
