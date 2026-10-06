import { useCallback, useEffect, useState } from 'react'
import { api, type E2eStatus } from './api/client'
import PageSubmit from './pages/PageSubmit'
import PageAnalysis from './pages/PageAnalysis'
import PageConfirm from './pages/PageConfirm'
import PageSql from './pages/PageSql'

const TABS = [
  { key: 'submit', label: '① 提交需求' },
  { key: 'analysis', label: '② 查看分析' },
  { key: 'confirm', label: '③ 口径确认' },
  { key: 'sql', label: '④ 看 SQL 与结果' },
] as const

type TabKey = (typeof TABS)[number]['key']

export default function App() {
  const [tab, setTab] = useState<TabKey>('submit')
  const [demandId, setDemandId] = useState<string | null>(null)
  const [dataset, setDataset] = useState('B')
  const [status, setStatus] = useState<E2eStatus | null>(null)
  const [health, setHealth] = useState<string>('检查中…')
  const [token, setToken] = useState(0)

  useEffect(() => {
    api
      .health()
      .then((h: any) => setHealth(h?.status === 'ok' ? '后端正常' : `后端状态：${h?.status ?? '未知'}`))
      .catch(() => setHealth('后端不可达'))
  }, [])

  const refresh = useCallback(() => setToken((t) => t + 1), [])

  // 核心聚合接口：四个页面共用一次状态快照，缺哪块由 partial_errors 说明
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
    setTab('analysis')
    refresh()
  }

  const blocking = (status?.analysis?.rule_check?.blocking_risks || []).length
  const openConf = status?.confirmations?.open_count ?? 0

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>数据需求智能分析助手</h1>
          <span className="tagline">提交 · 分析 · 口径确认 · 取数结果</span>
        </div>
        <div className="top-right">
          <span className={`health ${health === '后端正常' ? 'ok' : 'bad'}`}>{health}</span>
          {demandId ? <code className="did">{demandId}</code> : null}
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab${tab === t.key ? ' active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === 'confirm' && openConf > 0 ? <span className="badge-n">{openConf}</span> : null}
            {t.key === 'analysis' && blocking > 0 ? <span className="badge-r">{blocking}</span> : null}
          </button>
        ))}
        {demandId ? (
          <label className="ds-pick">
            数据集
            <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
              <option value="B">B 库 · 零售会员域</option>
              <option value="A">A 库 · 电商域</option>
            </select>
          </label>
        ) : null}
      </nav>

      <main className="main">
        {tab === 'submit' ? <PageSubmit onSubmitted={onSubmitted} demandId={demandId} /> : null}
        {tab === 'analysis' ? (
          <PageAnalysis demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} />
        ) : null}
        {tab === 'confirm' ? <PageConfirm demandId={demandId} status={status} onRefresh={refresh} /> : null}
        {tab === 'sql' ? <PageSql demandId={demandId} dataset={dataset} status={status} onRefresh={refresh} /> : null}
      </main>

      <footer className="foot">
        统一前端是业务用户唯一入口；所有取数能力经由后端 BFF 转发，分析与取数全程留痕。
      </footer>
    </div>
  )
}