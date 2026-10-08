import { useCallback, useEffect, useState } from 'react'
import { api, type E2eStatus } from './api/client'
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

/**
 * 工作台五个一级菜单（PRD §12.7），按「语义先行、知识准入、受控生成、联调交付」动线组织。
 * 菜单① 语义层 · MDL 字典 为默认首页。
 */
const MENUS = [
  { key: 'mdl', label: '① 语义层 · MDL 字典' },
  { key: 'knowledge', label: '② 知识储备' },
  { key: 'datasource', label: '③ 数据源接入' },
  { key: 'demand', label: '④ 需求分析 · SQL 生成' },
  { key: 'delivery', label: '⑤ SQL 联调 · 交付' },
] as const

/** 菜单④ 内部的链路分步（列表 → 提交 → 分析 → 确认 → 生成）。 */
const STEPS = [
  { key: 'list', label: '需求列表' },
  { key: 'submit', label: '提交需求' },
  { key: 'analysis', label: '语义分析' },
  { key: 'confirm', label: '口径确认' },
  { key: 'sql', label: 'SQL 生成' },
] as const

type MenuKey = (typeof MENUS)[number]['key']
type StepKey = (typeof STEPS)[number]['key']

/**
 * 当前需求编号的持久化键。
 *
 * 为什么必须落盘：demandId 原先只活在 useState 里，刷新页面即丢，
 * 业务人员离开页面后自己提交过的需求一条都找不回来。
 * 现在双写：URL query（可分享/收藏）+ localStorage（同浏览器刷新兜底）。
 */
const DEMAND_KEY = 'askoda.currentDemandId'

function readDemandFromLocation(): { id: string | null; step: StepKey | null } {
  const params = new URLSearchParams(window.location.search)
  const id = params.get('demand') || ''
  const step = params.get('step')
  const valid = STEPS.some((s) => s.key === step) ? (step as StepKey) : null
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

export default function App() {
  const restored = readDemandFromLocation()
  const [menu, setMenu] = useState<MenuKey>('mdl')
  // URL 带 step 时优先按 URL 定位，否则回落到「需求列表」——刷新后不该把人丢回提交页
  const [step, setStep] = useState<StepKey>(restored.step || 'list')
  // 根因修复：demandId 不再只存内存，初始值来自 URL / localStorage
  const [demandId, setDemandId] = useState<string | null>(restored.id || readStoredDemand())
  const [dataset, setDataset] = useState('B')
  const [status, setStatus] = useState<E2eStatus | null>(null)
  const [health, setHealth] = useState<string>('检查中…')
  const [token, setToken] = useState(0)
  const [notice, setNotice] = useState('')

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

  // 任何 demandId / step 变化都落盘，保证刷新与分享链接都能回到当前需求
  useEffect(() => {
    persistDemand(demandId, step)
  }, [demandId, step])

  const onSubmitted = (id: string) => {
    setDemandId(id)
    setMenu('demand')
    setStep('analysis')
    refresh()
  }

  // 从列表页选中一条需求：沿用同一条需求继续走后续流程
  const onPickDemand = useCallback((id: string) => {
    setDemandId(id)
    setMenu('demand')
    setStep('analysis')
    setNotice(`已切换到需求 ${id}`)
    refresh()
  }, [refresh])

  const blocking = (status?.analysis?.rule_check?.blocking_risks || []).length
  const openConf = status?.confirmations?.open_count ?? 0
  const demandStatus = status?.demand?.status

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>数据需求智能分析助手</h1>
          <span className="tagline">语义先行 · 知识准入 · 受控生成 · 联调交付</span>
        </div>
        <div className="top-right">
          {notice ? <span className="notice">{notice}</span> : null}
          <span className={`health ${health === '后端正常' ? 'ok' : 'bad'}`}>{health}</span>
          {demandId ? (
            <button
              className="linklike"
              title="回到需求列表，换一条需求继续处理"
              onClick={() => {
                setMenu('demand')
                setStep('list')
              }}
            >
              <code className="did">{demandId}</code>
            </button>
          ) : null}
        </div>
      </header>

      <nav className="tabs">
        {MENUS.map((m) => (
          <button
            key={m.key}
            className={`tab${menu === m.key ? ' active' : ''}`}
            onClick={() => setMenu(m.key)}
          >
            {m.label}
            {m.key === 'demand' && openConf > 0 ? <span className="badge-n">{openConf}</span> : null}
            {m.key === 'demand' && blocking > 0 ? <span className="badge-r">{blocking}</span> : null}
          </button>
        ))}
        <label className="ds-pick">
          <span className="ds-lbl">数据集</span>
          <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
            <option value="B">B 库 · 零售会员域</option>
            <option value="A">A 库 · 电商域</option>
          </select>
        </label>
      </nav>

      {menu === 'demand' ? (
        <nav className="tabs sub">
          {STEPS.map((s) => (
            <button
              key={s.key}
              className={`tab small${step === s.key ? ' active' : ''}`}
              onClick={() => setStep(s.key)}
            >
              {s.label}
              {s.key === 'confirm' && openConf > 0 ? <span className="badge-n">{openConf}</span> : null}
              {s.key === 'analysis' && blocking > 0 ? <span className="badge-r">{blocking}</span> : null}
            </button>
          ))}
        </nav>
      ) : null}

      <main className="main">
        {menu === 'mdl' ? <PageMdl dataset={dataset} onNotify={notify} /> : null}
        {menu === 'knowledge' ? <PageKnowledge demandId={demandId} /> : null}
        {menu === 'datasource' ? (
          <PageDatasource dataset={dataset} demandId={demandId} onNotify={notify} />
        ) : null}
        {menu === 'demand' && step === 'list' ? (
          <PageDemandList currentId={demandId} onPick={onPickDemand} />
        ) : null}
        {menu === 'demand' && step === 'submit' ? (
          <PageSubmit onSubmitted={onSubmitted} demandId={demandId} />
        ) : null}
        {menu === 'demand' && step === 'analysis' ? (
          <>
            <PageAnalysis demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
            {demandId ? (
              <DemandStatusActions
                demandId={demandId}
                currentStatus={demandStatus}
                onDone={refresh}
              />
            ) : null}
          </>
        ) : null}
        {menu === 'demand' && step === 'confirm' ? (
          <PageConfirm demandId={demandId} status={status} onRefresh={refresh} />
        ) : null}
        {menu === 'demand' && step === 'sql' ? (
          <PageSql demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
        ) : null}
        {menu === 'delivery' ? (
          <PageDelivery demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
        ) : null}
      </main>

      <footer className="foot">
        统一前端是业务用户唯一入口；所有取数能力经由后端 BFF 转发，分析与取数全程留痕。
      </footer>
    </div>
  )
}
