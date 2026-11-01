import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type LlmProvider, type LlmSettings, type LlmTestResp } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import RichText from '../components/RichText'
import KnowledgeBinding from '../components/KnowledgeBinding'

/**
 * 菜单④ · 系统设置 · 大模型连接
 *
 * 为什么这一页是产品能力而不是部署参数
 * ------------------------------------
 * 私有化交付时，客户换模型（自建 vLLM、甲方指定的供应商）不该走「改 compose +
 * 重建镜像 + 重启」。做成界面后：选供应商 → 填密钥 → 点「测试连接」→ 保存，
 * 十秒闭环，且不需要我们到场。
 *
 * 交互上的三条硬约定（与网关侧一致，改之前先看 `gateway/llm_settings.py`）：
 *  1. **密钥只进不出**：回显永远是掩码。输入框留空 = 不改动原值，绝不把掩码回传；
 *  2. **先测后存**：测试用的是「界面上的草稿」，成功与否都不改动生效配置；
 *  3. **没配齐就说清缺什么**：`missing` 直接翻译成人话，不含内部字段名。
 */

const ACTION_LABEL: Record<string, string> = {
  save: '保存配置',
  reset: '恢复系统默认',
}

function sourceLabel(s: LlmSettings | null): string {
  if (!s) return '-'
  if (s.source === 'db') return '界面保存的配置'
  if (s.source === 'env') return '环境变量（部署时配置）'
  return '未配置'
}

export default function PageSettings({
  readonly,
  actor,
  onNotify,
}: {
  readonly?: boolean
  actor: string
  onNotify?: (msg: string) => void
}) {
  const [data, setData] = useState<LlmSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const [provider, setProvider] = useState('deepseek')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [temperature, setTemperature] = useState('0')
  const [timeoutSec, setTimeoutSec] = useState('60')
  const [maxRepair, setMaxRepair] = useState('1')

  const [busy, setBusy] = useState('')
  const [tested, setTested] = useState<LlmTestResp | null>(null)
  const [saved, setSaved] = useState<{ notice: string; warnings: string[] } | null>(null)

  const hydrate = useCallback((s: LlmSettings) => {
    setProvider(s.provider || 'deepseek')
    setBaseUrl(s.base_url || '')
    setModel(s.model || '')
    setApiKey('')
    setEnabled(s.enabled !== false)
    setTemperature(String(s.temperature ?? 0))
    setTimeoutSec(String(s.timeout ?? 60))
    setMaxRepair(String(s.max_repair ?? 1))
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await api.llmSettings()
      setData(s)
      hydrate(s)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [hydrate])

  useEffect(() => {
    load()
  }, [load])

  const presets: LlmProvider[] = useMemo(() => data?.providers || [], [data])
  const currentPreset = useMemo(
    () => presets.find((p) => p.key === provider),
    [presets, provider],
  )

  /** 换供应商：把地址与模型名带到该家预设上。
   *  只覆盖「还是上一家预设地址」或「空着」的情况——用户手填的代理地址不能被悄悄改掉。 */
  const onProviderChange = (next: string) => {
    const from = presets.find((p) => p.key === provider)
    const to = presets.find((p) => p.key === next)
    setProvider(next)
    if (to && to.base_url && (!baseUrl || (from && baseUrl === from.base_url))) {
      setBaseUrl(to.base_url)
    }
    if (to && to.models.length && (!model || (from && !from.models.includes(model)))) {
      setModel(to.models[0])
    }
    setTested(null)
  }

  const draft = () => ({
    provider,
    base_url: baseUrl,
    model,
    api_key: apiKey,
  })

  const doTest = async () => {
    setBusy('test')
    setTested(null)
    setError(null)
    try {
      const r = await api.testLlm({ ...draft(), timeout: 20 })
      setTested(r)
      onNotify?.(r.ok ? '连接测试通过' : '连接测试未通过，按提示检查')
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const doSave = async () => {
    setBusy('save')
    setError(null)
    setSaved(null)
    try {
      const r = await api.saveLlmSettings({
        ...draft(),
        enabled,
        temperature: Number(temperature) || 0,
        timeout: Number(timeoutSec) || 60,
        max_repair: Number(maxRepair) || 0,
        actor,
      })
      setSaved({ notice: r.notice || '已保存', warnings: r.warnings || [] })
      onNotify?.(r.notice || '已保存')
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const doReset = async () => {
    if (!window.confirm('确定恢复为系统默认（改用环境变量里的大模型配置）？界面里保存的配置会被删除。')) return
    setBusy('reset')
    setError(null)
    setSaved(null)
    try {
      const r = await api.saveLlmSettings({ reset: true, actor })
      setSaved({ notice: r.notice || '已恢复系统默认', warnings: [] })
      onNotify?.(r.notice || '已恢复系统默认')
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const doClearCredential = async () => {
    if (!window.confirm('确定清除已保存的 API Key？清除后大模型会立即停用（SQL 生成回落确定性规划器）。')) return
    setBusy('clear')
    setError(null)
    setSaved(null)
    try {
      const r = await api.saveLlmSettings({
        enabled: false,
        clear_credential: true,
        actor,
      })
      setSaved({ notice: r.notice || '密钥已清除', warnings: r.warnings || [] })
      onNotify?.('密钥已清除')
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const ro = !!readonly
  const credHint = data?.has_credential
    ? `已保存（${data.credential_masked || '不回显'}），留空表示不改`
    : '尚未设置'

  return (
    <div className="page">
      <KnowledgeBinding readonly={readonly} actor={actor} onNotify={onNotify} />

      <Card
        title="大模型连接"
        collapsible
        id="settings-llm"
        desc="配在这里，换模型不用重建镜像"
        extra={
          <>
            <button onClick={load} disabled={loading || !!busy}>
              {loading ? '读取中…' : '刷新'}
            </button>
            {!ro ? (
              <button onClick={doTest} disabled={!!busy || !baseUrl || !model}>
                {busy === 'test' ? '测试中…' : '测试连接'}
              </button>
            ) : null}
            {!ro ? (
              <button
                className="primary"
                onClick={doSave}
                disabled={!!busy || !baseUrl.trim() || !model.trim()}
                title={
                  !baseUrl.trim() || !model.trim()
                    ? '接口地址与模型名是必填项，填好才能保存'
                    : '保存并立即生效'
                }
              >
                {busy === 'save' ? '保存中…' : '保存并生效'}
              </button>
            ) : null}
          </>
        }
      >
        <ErrorBox error={error} />
        {loading ? <Loading tip="正在读取当前配置…" /> : null}

        {data ? (
          <div className="trace">
            <div>
              <span className="trace-k">当前来源</span>
              <code>{sourceLabel(data)}</code>
            </div>
            <div>
              <span className="trace-k">是否生效</span>
              <code>{data.effective ? '已生效 · 大模型参与生成' : '未生效 · 走确定性规则'}</code>
            </div>
            <div>
              <span className="trace-k">当前模型</span>
              <code>{data.model || '-'}</code>
            </div>
            <div>
              <span className="trace-k">最近修改</span>
              <code>
                {data.updated_at ? `${data.updated_at}${data.updated_by ? ` · ${data.updated_by}` : ''}` : '-'}
              </code>
            </div>
          </div>
        ) : null}

        {data?.db_error ? (
          <div className="warnbox">
            <div className="warnbox-title">配置存储暂不可用</div>
            <div className="warnbox-hint">
              <RichText text={`读取配置库失败，当前按环境变量运行。${data.db_error}`} />
            </div>
          </div>
        ) : null}

        {saved ? (
          <div className="warnbox">
            <div className="warnbox-title">{saved.notice}</div>
            {saved.warnings.length ? (
              <ul className="warnbox-list">
                {saved.warnings.map((w, i) => (
                  <li key={i}>
                    <span className="warnbox-msg">
                      <RichText text={w} />
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        <div className="field">
          <label className="field-label" htmlFor="llm-provider">
            供应商
          </label>
          <select
            id="llm-provider"
            className="textinput"
            value={provider}
            disabled={ro || !!busy}
            onChange={(e) => onProviderChange(e.target.value)}
          >
            {presets.length === 0 ? <option value={provider}>{provider || '默认'}</option> : null}
            {presets.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
          {currentPreset?.note ? <span className="hint-inline">{currentPreset.note}</span> : null}
        </div>

        <div className="field-row">
          <div className="field">
            <label className="field-label" htmlFor="llm-base-url">
              接口地址
            </label>
            <input
              id="llm-base-url"
              className="textinput"
              placeholder="https://api.example.com/v1"
              value={baseUrl}
              disabled={ro || !!busy}
              onChange={(e) => {
                setBaseUrl(e.target.value)
                setTested(null)
              }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="llm-model">
              模型名
            </label>
            <input
              id="llm-model"
              className="textinput"
              list="llm-model-options"
              placeholder="deepseek-chat"
              value={model}
              disabled={ro || !!busy}
              onChange={(e) => {
                setModel(e.target.value)
                setTested(null)
              }}
            />
            <datalist id="llm-model-options">
              {(currentPreset?.models || []).map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="llm-api-key">
            API Key
          </label>
          <input
            id="llm-api-key"
            className="textinput"
            type="password"
            autoComplete="off"
            placeholder={data?.has_credential ? '留空表示不修改已保存的密钥' : '粘贴服务商给的密钥'}
            value={apiKey}
            disabled={ro || !!busy}
            onChange={(e) => {
              setApiKey(e.target.value)
              setTested(null)
            }}
          />
          <span className="hint-inline">凭据：{credHint}</span>
        </div>

        <div className="field-row">
          <div className="field">
            <label className="field-label" htmlFor="llm-temperature">
              采样温度（0 最稳，建议 0）
            </label>
            <input
              id="llm-temperature"
              className="textinput"
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={temperature}
              disabled={ro || !!busy}
              onChange={(e) => setTemperature(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="llm-timeout">
              单次调用超时（秒）
            </label>
            <input
              id="llm-timeout"
              className="textinput"
              type="number"
              min="5"
              max="600"
              value={timeoutSec}
              disabled={ro || !!busy}
              onChange={(e) => setTimeoutSec(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="llm-max-repair">
              门禁不过时最多修几轮
            </label>
            <input
              id="llm-max-repair"
              className="textinput"
              type="number"
              min="0"
              max="3"
              value={maxRepair}
              disabled={ro || !!busy}
              onChange={(e) => setMaxRepair(e.target.value)}
            />
          </div>
        </div>

        <label className="checkline">
          <input
            type="checkbox"
            checked={enabled}
            disabled={ro || !!busy}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span>启用大模型（关闭后立即回落确定性规则生成，不影响其他功能）</span>
        </label>

        {tested ? (
          <div className={tested.ok ? 'warnbox' : 'errbox'}>
            <div className={tested.ok ? 'warnbox-title' : 'errbox-title'}>
              {tested.ok ? `连接正常（${tested.latency_ms ?? '-'} ms）` : '连接失败'}
            </div>
            <div className={tested.ok ? 'warnbox-hint' : 'errbox-hint'}>
              {tested.notice ? <RichText text={tested.notice} /> : null}
            </div>
            {tested.ok ? (
              <div className="hint-inline">
                被测配置：{tested.config?.model || '-'} @ {tested.config?.base_url || '-'}
                {tested.reply ? (
                  <>
                    　模型回话：<RichText text={tested.reply} />
                  </>
                ) : null}
              </div>
            ) : tested.error ? (
              <pre className="sqlbox small">{tested.error}</pre>
            ) : null}
          </div>
        ) : null}

        {!ro ? (
          <div className="pager">
            <button onClick={doReset} disabled={!!busy || busy === 'reset'}>
              {busy === 'reset' ? '处理中…' : '恢复系统默认（改用环境变量配置）'}
            </button>
            {data?.has_credential ? (
              <button onClick={doClearCredential} disabled={!!busy}>
                清除已保存的密钥
              </button>
            ) : null}
          </div>
        ) : null}
      </Card>

      {data?.recent_changes?.length ? (
        <Card title="配置变更记录" collapsible id="settings-llm-history" defaultOpen={false}>
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>动作</th>
                  <th>操作人</th>
                  <th>模型</th>
                  <th>换过密钥</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_changes.map((c, i) => (
                  <tr key={i}>
                    <td className="mono">{c.at || '-'}</td>
                    <td>{ACTION_LABEL[c.action] || c.action}</td>
                    <td>{c.actor || '-'}</td>
                    <td className="mono">{c.model || '-'}</td>
                    <td>{c.credential_changed ? '是' : '否'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  )
}
