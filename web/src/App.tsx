import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type E2eStatus } from './api/client'
import { RoleProvider, useRole, ROLE_META, ROLES, type Role } from './role'
import SideNav from './components/SideNav'
import WizardStepper from './components/WizardStepper'
import PageMdl from './pages/PageMdl'
import PageKnowledge from './pages/PageKnowledge'
import PageDatasource from './pages/PageDatasource'
import PageDemandList from './pages/PageDemandList'
import PageSubmit from './pages/PageSubmit'
import PageAnalysis from './pages/PageAnalysis'
import PageConfirm from './pages/PageConfirm'
import PageSql from './pages/PageSql'
import PageDelivery from './pages/PageDelivery'
import DemandStatusActions from './components/DemandStatusActions'
import { Card } from './components/ui'

/** 模块 key（左侧导航）。 */
type MenuKey = 'demand' | 'mdl' | 'knowledge' | 'datasource' | 'audit' | 'system'
/** 需求链路内的步骤 key。'list' 是需求列表枢纽页，不计入向导五步。 */
type StepKey = 'list' | 'submit' | 'analysis' | 'confirm' | 'sql' | 'delivery'

const DEMAND_KEY = 'askoda.currentDemandId'
const THEME_KEY = 'askoda.theme'

function readDemandFromLocation(): { id: string | null; step: StepKey | null } {
  const params = new URLSearchParams(window.location.search)
  const id = params.get('demand') || ''
  const step = params.get('step')
  const valid = step === 'list' || step === 'submit' || step === 'analysis' || step === 'confirm' || step === 'sql' || step === 'delivery' ? (step as StepKey) : null
  return { id: id || null, step: valid }
}

function readStoredDemand(): string | null {
  try {
    return window.localStorage.getItem(DEMAND_KEY)
  } catch {
    return null
  }
}

function persistDemand(id: string | null, step: StepKey) {
  try {
    if (id) window.localStorage.setItem(DEMAND_KEY, id)
    else window.localStorage.removeItem(DEMAND_KEY)
  } catch {
    /* localStorage 不可用（隐私模式）时静默降级：URL 仍可找回 */
  }
  const params = new URLSearchParams(window.location.search)
  if (id) {
    params.set('demand', id)
    params.set('step', step)
  } else {
    params.delete('demand')
    params.delete('step')
  }
  const qs = params.toString()
  window.history.replaceState(null, '', `${window.location.pathname}${qs ? `?${qs}` : ''}`)
}

/** 向导五步 stepKey → 节点序号（'list' 不在向导内）。 */
const STEP_TO_NODE: Record<string, number> = {
  submit: 0,
  analysis: 1,
  confirm: 2,
  sql: 3,
  delivery: 4,
}

function AppShell() {
  const restored = readDemandFromLocation()
  const { role, setRole, isReadonly, actor } = useRole()

  const [menu, setMenuState] = useState<MenuKey>('demand')
  const [step, setStep] = useState<StepKey>(restored.step || 'list')
  const [demandId, setDemandId] = useState<string | null>(restored.id || readStoredDemand())
  const [dataset, setDataset] = useState('B')
  const [status, setStatus] = useState<E2eStatus | null>(null)
  const [health, setHealth] = useState<string>('检查中…')
  const [token, setToken] = useState(0)
  const [notice, setNotice] = useState('')
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      const v = window.localStorage.getItem(THEME_KEY)
      if (v === 'dark' || v === 'light') return v
    } catch {
      /* 忽略 */
    }
    return 'light'
  })

  // 主题：只切 html[data-theme]，CSS 层零 JS（见 semantic.css 暗色块）
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      window.localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* 忽略 */
    }
  }, [theme])

  useEffect(() => {
    api
      .health()
      .then((h: any) => setHealth(h?.status === 'ok' ? '后端正常' : `后端状态：${h?.status ?? '未知'}`))
      .catch(() => setHealth('后端不可达'))
  }, [])

  const refresh = useCallback(() => setToken((t) => t + 1), [])
  const notify = useCallback((msg: string) => {
    setNotice(msg)
    window.setTimeout(() => setNotice(''), 4000)
  }, [])

  // 数据集自动对齐：打开一条需求时，若它只在「某一个」数据集上做过分析，
  // 就自动对齐到该数据集，避免电商域(A)需求被默认 B 库语境持续「口径未定义」。
  // 若两个数据集都做过分析（歧义），则尊重手动选择并提示切换，绝不擅自跳。
  // 每条需求仅在对齐过的瞬间标记一次，之后用户的手动切换不会被覆盖。
  const autoDatasetDemand = useRef<string | null>(null)
  useEffect(() => {
    if (!demandId || !status?.datasets_with_analysis) return
    if (demandId === autoDatasetDemand.current) return
    const dw = status.datasets_with_analysis
    if (dw.length === 1 && dw[0] !== dataset) {
      autoDatasetDemand.current = demandId
      setDataset(dw[0])
    } else if (dw.length >= 1) {
      // 已确认过此需求的数据集范围，标记一次以免后续手动切换被覆盖
      autoDatasetDemand.current = demandId
    }
  }, [demandId, status?.datasets_with_analysis, dataset])

  // 核心聚合接口：需求链路各页共用一次状态快照，缺哪块由 partial_errors 说明
  useEffect(() => {
    if (!demandId) {
      setStatus(null)
      return
    }
    let alive = true
    api
      .e2eStatus(demandId, dataset)
      .then((s) => {
        if (alive) setStatus(s)
      })
      .catch(() => {
        if (alive) setStatus(null)
      })
    return () => {
      alive = false
    }
  }, [demandId, dataset, token])

  useEffect(() => {
    persistDemand(demandId, step)
  }, [demandId, step])

  const onSubmitted = (id: string) => {
    setDemandId(id)
    setMenuState('demand')
    setStep('analysis')
    refresh()
  }

  const onPickDemand = useCallback(
    (id: string) => {
      setDemandId(id)
      setMenuState('demand')
      setStep('analysis')
      setNotice(`已切换到需求 ${id}`)
      refresh()
    },
    [refresh],
  )

  const onSelectMenu = useCallback((m: string) => {
    setMenuState(m as MenuKey)
  }, [])

  const onWizardStep = useCallback((stepKey: string) => {
    setMenuState('demand')
    setStep(stepKey as StepKey)
  }, [])

  const blocking = (status?.analysis?.rule_check?.blocking_risks as unknown[] | undefined)?.length ?? 0
  const openConf = (status?.confirmations?.open_count as number | undefined) ?? 0
  const demandStatus = status?.demand?.status

  const readonlyModule = isReadonly(menu)
  const blockedReason = blocking > 0 ? `有 ${blocking} 条标红口径未处理，先到「对一对关键说法」清掉` : undefined

  return (
    <div className="appshell">
      <header className="top">
        <div className="brand">
          <h1>数据需求智能分析助手</h1>
          <span className="tagline">语义先行 · 知识准入 · 受控生成 · 联调交付</span>
        </div>
        <div className="top-right">
          {notice ? <span className="notice">{notice}</span> : null}
          <span className={`health ${health === '后端正常' ? 'ok' : 'bad'}`}>{health}</span>
          <label className="ds-pick">
            <span className="ds-lbl">数据集</span>
            <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
              <option value="B">B 库 · 零售会员域</option>
              <option value="A">A 库 · 电商域</option>
            </select>
          </label>
          <label className="roleswitch" title="SSO 就绪前为本地演示切换；切换后页面按角色隔离">
            <span className="roleswitch-lbl">身份</span>
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r} · {ROLE_META[r].label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="dark-toggle"
            onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
            title={theme === 'light' ? '切换到暗色' : '切换到亮色'}
          >
            {theme === 'light' ? '🌙' : '☀️'}
          </button>
          {demandId ? (
            <button
              type="button"
              className="linklike"
              title="回到需求列表，换一条需求继续处理"
              onClick={() => {
                setMenuState('demand')
                setStep('list')
              }}
            >
              <code className="did">{demandId}</code>
            </button>
          ) : null}
        </div>
      </header>

      <div className="appbody">
        <SideNav active={menu} onSelect={onSelectMenu} />

        <main className="appmain">
          {/* 弱鉴权过渡提示：SSO 未就绪，角色为本地演示态 */}
          <div className="weakauth" role="note">
            <span className="weakauth-tag">演示态</span>
            <span>
              当前为演示环境，可切换身份体验不同角色的权限。正式上线由企业账号统一登录，真实权限以后台校验为准。
            </span>
          </div>

          {readonlyModule ? (
            <div className="readonly-banner" role="note">
              当前身份「{ROLE_META[role].label}」对该模块<b>只读</b>，仅可查看，不可编辑。
            </div>
          ) : null}

          {menu === 'demand' ? (
            <>
              <WizardStepper
                demandId={demandId}
                status={status}
                activeStepKey={step}
                onStep={onWizardStep}
                blockedReason={blockedReason}
              />
              {/* 需求列表枢纽：选已有需求，或跳去「提个需求」新建 */}
              {step === 'list' ? (
                <PageDemandList currentId={demandId} onPick={onPickDemand} />
              ) : null}
              {step === 'submit' ? (
                <PageSubmit onSubmitted={onSubmitted} demandId={demandId} actor={actor} readonly={readonlyModule} />
              ) : null}
              {step === 'analysis' ? (
                <>
                  <PageAnalysis
                    demandId={demandId}
                    dataset={dataset}
                    status={status}
                    onRefresh={refresh}
                    onGoConfirm={() => onWizardStep('confirm')}
                  />
                  {demandId ? (
                    <DemandStatusActions demandId={demandId} currentStatus={demandStatus} onDone={refresh} />
                  ) : null}
                </>
              ) : null}
              {step === 'confirm' ? (
                <PageConfirm
                  demandId={demandId}
                  dataset={dataset}
                  status={status}
                  onRefresh={refresh}
                  onGoSql={() => onWizardStep('sql')}
                />
              ) : null}
              {step === 'sql' ? (
                <PageSql demandId={demandId} dataset={dataset} actor={actor} status={status} onRefresh={refresh} />
              ) : null}
              {step === 'delivery' ? (
                <PageDelivery demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
              ) : null}
            </>
          ) : null}

          {menu === 'mdl' ? <PageMdl dataset={dataset} onNotify={notify} /> : null}
          {menu === 'knowledge' ? <PageKnowledge demandId={demandId} /> : null}
          {menu === 'datasource' ? (
            <PageDatasource dataset={dataset} demandId={demandId} onNotify={notify} />
          ) : null}

          {menu === 'audit' ? (
            <div className="page">
              <Card title="复盘留痕" collapsible id="audit">
                <p className="muted">
                  审计回放按「需求」维度展开：打开一条需求，进入「出数与交付」即可看到完整判断链回放
                  （每次执行的 SQL、门禁结论、知识引用与责任人）。
                </p>
                <p>
                  {demandId ? (
                    <button className="primary" onClick={() => onWizardStep('delivery')}>
                      前往当前需求的回放
                    </button>
                  ) : (
                    '先选一条需求，再回放它的处理记录。'
                  )}
                </p>
              </Card>
            </div>
          ) : null}

          {menu === 'system' ? (
            <div className="page">
              <Card title="系统设置" collapsible id="system">
                <p className="muted">
                  数据空间（租户）与全局配置在此管理。当前为演示态，相关能力建设中。
                </p>
              </Card>
            </div>
          ) : null}
        </main>
      </div>

      <footer className="foot">
        所有取数与分析操作都会自动留痕，便于随时追溯与复核。
      </footer>
    </div>
  )
}

export default function App() {
  return (
    <RoleProvider>
      <AppShell />
    </RoleProvider>
  )
}
