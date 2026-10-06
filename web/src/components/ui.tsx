import React from 'react'
import type { PartialError } from '../api/client'
import { ApiError } from '../api/client'

/** 统一错误展示：必须显式呈现 hint（「下一步该干什么」），不允许只报错不指路。 */
export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  const e = error as ApiError
  const hint = (e as any).hint
  const code = (e as any).code
  return (
    <div className="errbox">
      <div className="errbox-title">
        {e.message || '出错了'}
        {code ? <code className="errbox-code">{code}</code> : null}
      </div>
      {hint ? <div className="errbox-hint">怎么办：{hint}</div> : null}
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
            <span className="warnbox-msg">{e.message}</span>
            {e.hint ? <div className="warnbox-hint">怎么办：{e.hint}</div> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

const BLOCK_LABEL: Record<string, string> = {
  demand: '需求详情',
  analysis: '分析结果',
  confirmations: '口径确认',
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
}: {
  title?: string
  extra?: React.ReactNode
  children: React.ReactNode
  tone?: 'default' | 'muted'
}) {
  return (
    <section className={`card${tone === 'muted' ? ' card-muted' : ''}`}>
      {title ? (
        <header className="card-head">
          <h3>{title}</h3>
          {extra ? <div className="card-extra">{extra}</div> : null}
        </header>
      ) : null}
      <div className="card-body">{children}</div>
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