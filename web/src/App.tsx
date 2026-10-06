import { useCallback, useEffect, useState } from 'react'
import { api, type E2eStatus } from './api/client'
import PageMdl from './pages/PageMdl'
import PageKnowledge from './pages/PageKnowledge'
import PageDatasource from './pages/PageDatasource'
import PageSubmit from './pages/PageSubmit'
import PageAnalysis from './pages/PageAnalysis'
import PageConfirm from './pages/PageConfirm'
import PageSql from './pages/PageSql'
import PageDelivery from './pages/PageDelivery'

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

/** 菜单④ 内部的八步链路分步（提交 → 分析 → 确认 → 生成）。 */
const STEPS = [
  { key: 'submit', label: '提交需求' },
  { key: 'analysis', label: '语义分析' },
  { key: 'confirm', label: '口径确认' },
  { key: 'sql', label: 'SQL 生成' },
] as const

type MenuKey = (typeof MENUS)[number]['key']
type StepKey = (typeof STEPS)[number]['key']

export default function App() {
  const [menu, setMenu] = useState<MenuKey>('mdl')
  const [step, setStep] = useState<StepKey>('submit')
  const [demandId, setDemandId] = useState<string | null>(null)
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

  const onSubmitted = (id: string) => {
    setDemandId(id)
    setMenu('demand')
    setStep('analysis')
    refresh()
  }

  const blocking = (status?.analysis?.rule_check?.blocking_risks || []).length
  const openConf = status?.confirmations?.open_count ?? 0

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
          {demandId ? <code className="did">{demandId}</code> : null}
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
          数据集
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
        {menu === 'demand' && step === 'submit' ? (
          <PageSubmit onSubmitted={onSubmitted} demandId={demandId} />
        ) : null}
        {menu === 'demand' && step === 'analysis' ? (
          <PageAnalysis demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
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
