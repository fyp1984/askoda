import React from 'react'
import type { PartialError } from '../api/client'
import { ApiError } from '../api/client'
import RichText from './RichText'
import { useCollapse } from './Collapsible'

/** 统一错误展示：必须显式呈现 hint（「下一步该干什么」），不允许只报错不指路。
 *  ★ message / hint 多数直接来自 BFF 与网关的错误串，可能夹带 markdown 强调或
 *    HTML 片段（RAGFlow 类组件常用 `<code>` 包裹字段名）→ 一律走 RichText。 */
export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  const e = error as ApiError
  const hint = (e as any).hint
  const code = (e as any).code
  return (
    <div className="errbox">
      <div className="errbox-title">
        <RichText text={e.message || '出错了'} />
        {code ? <code className="errbox-code">{code}</code> : null}
      </div>
      {hint ? <div className="errbox-hint">怎么办：<RichText text={hint} /></div> : null}
    </div>
  )
}

/** 聚合接口的部分失败提示：缺哪一块说清楚，并给出该块的指引。 */
export function PartialErrorsBox({ errors }: { errors: PartialError[] }) {
  if (!errors || errors.length === 0) return null
  return (
    <div className="warnbox">
      <div className="warnbox-title">以下 {errors.length} 个环节暂时没有数据</div>
      <ul className="warnbox-list">
        {errors.map((e, i) => (
          <li key={i}>
            <strong>{BLOCK_LABEL[e.block] || e.block}</strong>
            <span className="warnbox-msg"><RichText text={e.message} /></span>
            {e.hint ? <div className="warnbox-hint">怎么办：<RichText text={e.hint} /></div> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

const BLOCK_LABEL: Record<string, string> = {
  demand: '需求详情',
  analysis: '分析结果',
  confirmations: '待确认口径',
  requirement: '结构化需求',
  context_pack: '取数上下文',
  runs: '取数记录',
}

export function Loading({ tip = '处理中，请稍候…' }: { tip?: string }) {
  return <div className="loading">{tip}</div>
}

export function Card({
  title,
  extra,
  children,
  tone,
  collapsible = false,
  defaultOpen = true,
  id,
  count,
  desc,
}: {
  title?: string
  extra?: React.ReactNode
  children: React.ReactNode
  tone?: 'default' | 'muted'
  /** 头部加折叠按钮：页面分区过长时，把不关注的分区收起，避免整页过长 */
  collapsible?: boolean
  defaultOpen?: boolean
  /** 折叠态持久化 key；不传则按 title 派生（同名跨页面会共享折叠偏好） */
  id?: string
  count?: number | string
  desc?: string
}) {
  const key = id || (title ? `card:${title}` : 'card:unnamed')
  const { open, toggle } = useCollapse(key, defaultOpen)
  const cls = [
    'card',
    tone === 'muted' ? 'card-muted' : '',
    collapsible ? 'card-collapsible' : '',
    open ? '' : 'collapsed',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <section className={cls}>
      {title ? (
        <header className="card-head">
          {collapsible ? (
            <button
              type="button"
              className="card-toggle"
              onClick={toggle}
              aria-expanded={open}
              title={open ? '收起此卡片' : '展开此卡片'}
            >
              <span className="card-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
              <span className="card-title">{title}</span>
              {count !== undefined && count !== '' ? <span className="card-count">{count}</span> : null}
            </button>
          ) : (
            <h3>{title}</h3>
          )}
          {extra ? <div className="card-extra">{extra}</div> : null}
        </header>
      ) : null}
      {open ? <div className="card-body">{children}</div> : null}
    </section>
  )
}

/** 规则分级徽标：blocking 标红、warning 标黄。 */
export function RuleBadge({ severity, ruleId }: { severity: string; ruleId?: string }) {
  const cls = severity === 'blocking' ? 'badge badge-blocking' : 'badge badge-warning'
  return (
    <span className={cls}>
      {severity === 'blocking' ? '阻断' : '警告'}
      {ruleId ? ` · ${ruleId}` : ''}
    </span>
  )
}