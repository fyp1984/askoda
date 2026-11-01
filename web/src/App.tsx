import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type E2eStatus } from './api/client'
import { RoleProvider, useRole, ROLE_META, ROLES, type Role } from './role'
import SideNav from './components/SideNav'
import WizardStepper from './components/WizardStepper'
import PageMdl from './pages/PageMdl'
import PageDatasource from './pages/PageDatasource'
import PageDemandList from './pages/PageDemandList'
import PageSubmit from './pages/PageSubmit'
import PageAnalysis from './pages/PageAnalysis'
import PageConfirm from './pages/PageConfirm'
import PageSql from './pages/PageSql'
import PageDelivery from './pages/PageDelivery'
import PageSettings from './pages/PageSettings'
import DemandStatusActions from './components/DemandStatusActions'
import { Card } from './components/ui'

/** 模块 key（左侧导航）。知识管理入口已按 CR009 剔除——知识收集/治理全部在 WeKnora 平台完成，Askoda 经 MCP 消费。 */
type MenuKey = 'demand' | 'mdl' | 'datasource' | 'audit' | 'system'
/** 需求链路内的步骤 key。'list' 是需求列表枢纽页，不计入向导五步。 */
type StepKey = 'list' | 'submit' | 'analysis' | 'confirm' | 'sql' | 'delivery'

const DEMAND_KEY = 'askoda.currentDemandId'
const THEME_KEY = 'askoda.theme'
const DATASET_KEY = 'askoda.dataset'

/** 顶部数据集下拉的选项。原先写死 A/B 两项，新库接入后永远选不到；
 *  改为从 `/datasets` 拉——**下拉里有什么，取决于网关实际注册了什么**。 */
type DatasetOption = { key: string; label: string; domain?: string }

function readStoredDataset(fallback: string): string {
  try {
    return window.localStorage.getItem(DATASET_KEY) || fallback
  } catch {
    return fallback
  }
}

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

function AppShell() {
  const restored = readDemandFromLocation()
  const { role, setRole, isReadonly, actor } = useRole()

  const [menu, setMenuState] = useState<MenuKey>('demand')
  const [step, setStep] = useState<StepKey>(restored.step || 'list')
  const [demandId, setDemandId] = useState<string | null>(restored.id || readStoredDemand())
  const [dataset, setDatasetState] = useState(() => readStoredDataset('B'))
  const [dsOptions, setDsOptions] = useState<DatasetOption[]>([
    { key: 'B', label: 'B 库 · 零售会员域' },
    { key: 'A', label: 'A 库 · 电商域' },
  ])
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

  // 数据集清单：从网关拉，登记新库后调用 loadDatasets() 即可出现在下拉里。
  // 拿不到时保留内置两项——下拉空掉会让整条链路"没法选库"，比显示旧清单更糟。
  const loadDatasets = useCallback(async () => {
    try {
      const r = await api.datasets()
      const list = (r.datasets || [])
        .filter((d) => d && d.key)
        .map((d) => ({ key: d.key, label: d.label || d.key, domain: d.domain }))
      if (list.length) setDsOptions(list)
      return list
    } catch {
      return []
    }
  }, [])

  useEffect(() => {
    loadDatasets()
  }, [loadDatasets])

  const setDataset = useCallback((key: string) => {
    setDatasetState(key)
    try {
      window.localStorage.setItem(DATASET_KEY, key)
    } catch {
      /* 隐私模式下忽略：至少当前会话仍可用 */
    }
  }, [])

  // 选中的库被移除/改名后，下拉会显示空白且查询全错——自动落到第一个可用库并提示。
  useEffect(() => {
    if (!dsOptions.length) return
    if (!dsOptions.some((d) => d.key === dataset)) {
      const first = dsOptions[0].key
      setDataset(first)
      notify(`原数据集已不可用，已切换到「${dsOptions[0].label}」`)
    }
  }, [dsOptions, dataset, setDataset, notify])

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
        if (!alive) return
        // 僵尸需求单：页面从 localStorage / URL 恢复的单号可能已被清理，
        // 需求块取不到且原因明确是「不存在」时，必须退回列表——
        // 否则用户可以在这条并不存在的需求上继续点「生成候选 / 生成 SQL」，
        // 每一步都报错，看起来像系统坏了（2026-10-30 实测 DR-20261021-A1AL）。
        const gone =
          !s?.demand &&
          (s?.partial_errors || []).some(
            (e: any) => e?.block === 'demand' && /不存在/.test(String(e?.detail || '')),
          )
        if (gone) {
          const stale = demandId
          persistDemand(null, 'list')
          setDemandId(null)
          setStep('list')
          notify(`需求 ${stale} 已不存在（可能已被清理），已回到需求列表，请重新选择`)
          return
        }
        setStatus(s)
      })
      .catch(() => {
        if (alive) setStatus(null)
      })
    return () => {
      alive = false
    }
  }, [demandId, dataset, token, notify])

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
              {dsOptions.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.label}
                </option>
              ))}
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
                <PageDemandList
                  currentId={demandId}
                  onPick={onPickDemand}
                  actor={actor}
                  onNotify={notify}
                  onPurged={(ids) => {
                    // 当前打开的需求被清掉后必须回到列表并解除引用，
                    // 否则页面会继续拿一个不存在的单号反复请求（表现为"每一步都报错"）。
                    if (demandId && ids.includes(demandId)) {
                      persistDemand(null, 'list')
                      setDemandId(null)
                      setStatus(null)
                    }
                    refresh()
                  }}
                />
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
          {menu === 'datasource' ? (
            <PageDatasource
              dataset={dataset}
              demandId={demandId}
              actor={actor}
              onNotify={notify}
              onRegistryChanged={loadDatasets}
            />
          ) : null}

          {menu === 'audit' ? (
            <div className="page">
              <Card title="复盘留痕" collapsible id="audit">
                <p className="muted">
                  审计回放按「需求」维度展开：打开一条需求，进入「出数与交付」，页面底部有一块
                  <strong>「审计回放」</strong>（默认收起，点标题展开），可看到完整判断链回放
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
            <PageSettings readonly={readonlyModule} actor={actor} onNotify={notify} />
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
