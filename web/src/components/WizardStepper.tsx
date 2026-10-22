import type { E2eStatus } from '../api/client'

/**
 * 向导式步骤条：把「需求全链路」从一堆平铺子页签，改成用户一眼能看懂的进度条。
 *
 * 五步（业务术语，见 UI 方案权威表）：
 *   ① 提个需求 → ② 读懂你的需求 → ③ 对一对关键说法 → ④ 让系统写出 SQL → ⑤ 出数与交付
 *
 * 每一步四态之一：done（已完成）/ current（进行中，点我去办）/ todo（待办）/ blocked（被阻断）。
 * 状态由 e2eStatus 的真实信号驱动，而非写死：
 *   · 有没有需求           → demandId 是否存在
 *   · 读懂没读懂           → status.analysis 是否存在
 *   · 有没有未确认的口径   → status.confirmations.open_count
 *   · SQL 写出来没         → status.runs.items 是否非空
 *   · 出数交付收口没       → status.demand.status 终态
 * 阻断信号：analysis 存在但 rule_check.blocking_risks 非空 → 第②步变红，下游锁死。
 */

export interface WizardStep {
  index: number
  label: string
  /** 对应 App 的 step key（点击后跳转） */
  stepKey: string
  desc: string
}

export const WIZARD_STEPS: WizardStep[] = [
  { index: 0, label: '提个需求', stepKey: 'submit', desc: '填写需求，建立一条取数任务' },
  { index: 1, label: '读懂你的需求', stepKey: 'analysis', desc: '系统解析口径与要素' },
  { index: 2, label: '对一对关键说法', stepKey: 'confirm', desc: '业务方确认歧义口径' },
  { index: 3, label: '让系统写出 SQL', stepKey: 'sql', desc: '生成并执行 SQL' },
  { index: 4, label: '出数与交付', stepKey: 'delivery', desc: '核查结果，收口交付' },
]

export type StepState = 'done' | 'current' | 'todo' | 'blocked'

/** 由 e2eStatus 推导出五步各自的状态。 */
export function deriveStepStates(status: E2eStatus | null, demandId: string | null): StepState[] {
  const hasDemand = !!demandId
  const hasAnalysis = !!status?.analysis
  const blocking = (status?.analysis?.rule_check?.blocking_risks as unknown[] | undefined)?.length ?? 0
  const openConf = (status?.confirmations?.open_count as number | undefined) ?? 0
  const hasConfirm = !!status?.confirmations && openConf === 0
  const hasRuns = ((status?.runs?.items as unknown[] | undefined)?.length ?? 0) > 0
  const passed = status?.demand?.status === '已通过' || status?.demand?.status === '已退回'

  const done: boolean[] = [hasDemand, hasAnalysis, hasConfirm, hasRuns, passed]
  const states: StepState[] = done.map((d) => (d ? 'done' : 'todo'))

  // 第②步被阻断：分析已出但有标红口径，必须先清掉才能往下走
  const blocked1 = hasAnalysis && blocking > 0
  if (blocked1) states[1] = 'blocked'

  if (!done[0]) states[0] = 'current'

  // 进行中 = 第一个「未完成且上游已就绪、且没有被上游阻断」的步骤
  let assigned = false
  for (let i = 0; i < states.length; i++) {
    if (states[i] === 'done' || states[i] === 'blocked') continue
    const prevAllDone = i === 0 || done.slice(0, i).every(Boolean)
    if (!assigned && prevAllDone && !blocked1) {
      states[i] = 'current'
      assigned = true
    }
  }
  return states
}

export default function WizardStepper({
  demandId,
  status,
  activeStepKey,
  onStep,
  blockedReason,
}: {
  demandId: string | null
  status: E2eStatus | null
  /** 当前 App 的 step key（用于高亮用户所在节点） */
  activeStepKey: string
  onStep: (stepKey: string) => void
  /** 阻断时的说明，挂在第②步下方 */
  blockedReason?: string
}) {
  const states = deriveStepStates(status, demandId)
  const activeIdx = WIZARD_STEPS.findIndex((s) => s.stepKey === activeStepKey)
  const stepNo = activeIdx >= 0 ? activeIdx + 1 : 0

  return (
    <nav className="wizardwrap" aria-label="需求处理进度">
      <ol className="wizard">
        {stepNo > 0 ? (
          <li className="wizmeta" aria-hidden="true">第 {stepNo} / {WIZARD_STEPS.length} 步</li>
        ) : null}
        {WIZARD_STEPS.map((s, i) => {
        const st = states[i]
        const isActiveSel = activeStepKey === s.stepKey
        return (
          <li
            key={s.stepKey}
            className={`wizstep wiz-${st}${isActiveSel ? ' sel' : ''}`}
            aria-current={isActiveSel ? 'step' : undefined}
          >
            <button
              type="button"
              className="wizbtn"
              onClick={() => onStep(s.stepKey)}
              title={s.desc}
            >
              <span className="wizdot" aria-hidden="true">
                {st === 'done' ? '✓' : st === 'blocked' ? '!' : String(s.index + 1)}
              </span>
              <span className="wizlabel">{s.label}</span>
            </button>
            {st === 'blocked' && blockedReason ? (
              <span className="wizblock">{blockedReason}</span>
            ) : null}
          </li>
        )
      })}
      </ol>
    </nav>
  )
}
