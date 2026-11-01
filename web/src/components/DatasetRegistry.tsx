import { useCallback, useEffect, useState } from 'react'
import { api, type DatasetProbeResp, type DatasetRegistryResp } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import RichText from '../components/RichText'

/**
 * 取数通道 · 数据集登记
 *
 * 解决什么问题
 * ------------
 * 以前"再接一个业务库"= 改 compose 加一个 Wren 引擎 + 生成 MDL 打进镜像 +
 * 重建网关。私有化交付时客户做不了，演示时又恰恰最常遇到"再给我接个库"。
 *
 * 现在：填主机 / 库名 / 账号 → 贴 MDL → 测试 → 登记，几秒后顶部数据集下拉里就有它。
 *
 * 三条界面纪律
 * ------------
 * 1. **先探测再登记**：连接、引擎、MDL 三条判据分开显示，不允许合成一个"成功"——
 *    "通了但查不了"这种状态必须能被表达出来，否则问题会一路漏到取数环节。
 * 2. **只回显库名**：地址、账号、口令一概不出网关（R2）。所以编辑已有数据集时
 *    口令是空的，留空表示不改。
 * 3. **删除只是取消登记**：不动业务库、不删数据，按钮文案必须写清楚。
 */

const ENGINE_HINT: Record<string, string> = {
  A: '复用 A 引擎（电商域）',
  B: '复用 B 引擎（零售域）',
  C: '专用信贷引擎（独立容器）',
}

const CHECK_LABEL: Record<string, string> = {
  pg: '数据库连接',
  wren: '语义引擎',
  mdl: 'MDL 语义层',
}

export default function DatasetRegistry({
  actor,
  onChanged,
  onNotify,
}: {
  actor: string
  onChanged?: () => void
  onNotify?: (msg: string) => void
}) {
  const [data, setData] = useState<DatasetRegistryResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState('')

  const [form, setForm] = useState({
    key: '',
    label: '',
    domain: '',
    engine: 'C',
    port: '15432',
    dbname: '',
    user: 'test',
    password: '',
    mdl_json: '',
  })
  const [probe, setProbe] = useState<DatasetProbeResp | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.datasetRegistry())
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const set = (k: keyof typeof form, v: string) => {
    setForm((f) => ({ ...f, [k]: v }))
    if (k !== 'mdl_json') setProbe(null)
  }

  const payload = () => ({
    key: form.key.trim().toUpperCase(),
    label: form.label.trim(),
    domain: form.domain.trim(),
    engine: form.engine.trim().toUpperCase(),
    port: Number(form.port) || 0,
    dbname: form.dbname.trim(),
    user: form.user.trim(),
    password: form.password,
    mdl_json: form.mdl_json.trim(),
  })

  const doProbe = async () => {
    setBusy('probe')
    setError(null)
    setProbe(null)
    try {
      setProbe(await api.probeDataset(payload()))
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const doRegister = async () => {
    setBusy('save')
    setError(null)
    try {
      const r = await api.registerDataset({ ...payload(), actor })
      onNotify?.(String(r.notice || '已登记'))
      await load()
      onChanged?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const doRemove = async (key: string) => {
    if (!window.confirm(`取消登记数据集 ${key}？\n\n只取消登记，不删除业务库、不删除任何数据。`)) return
    setBusy('del-' + key)
    setError(null)
    try {
      const r = await api.unregisterDataset(key, actor)
      onNotify?.(String(r.notice || '已移除'))
      await load()
      onChanged?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const fillFor = (key: string) => {
    const it = (data?.registered || []).find((x) => x.key === key)
    setForm((f) => ({
      ...f,
      key,
      label: it?.label || '',
      domain: it?.domain || '',
      engine: it?.engine || key,
      dbname: it?.database || '',
      password: '',
      mdl_json: '',
    }))
    setProbe(null)
  }

  const checks = probe ? Object.entries(probe.checks || {}) : []
  const canSubmit = !!form.key.trim() && !!form.dbname.trim() && !!form.mdl_json.trim()

  return (
    <Card
      title="数据集登记（新库接入）"
      collapsible
      id="ds-registry"
      desc="建成新库后在这里登记，几秒后即可在顶部数据集下拉里选中"
      extra={
        <button onClick={load} disabled={loading || !!busy}>
          {loading ? '刷新中…' : '刷新清单'}
        </button>
      }
    >
      <ErrorBox error={error} />
      {loading ? <Loading tip="正在读取数据集登记…" /> : null}

      <p className="muted">
        登记只解决"系统知道有这个库"。真正能查数还要求：该库的语义引擎已装载同一份 MDL。
        下面的「语义引擎」判据会比对两边模型数，不一致会直接告诉你差在哪。
      </p>

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>编码</th>
              <th>名称 / 领域</th>
              <th>引擎</th>
              <th>库名</th>
              <th>模型数</th>
              <th>来源</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {(data?.usable || []).map((d) => (
              <tr key={d.key}>
                <td className="mono">{d.key}</td>
                <td>
                  {d.label || '-'}
                  <div className="hint-inline">{d.domain || ''}</div>
                </td>
                <td className="mono">{d.key}</td>
                <td className="mono">-</td>
                <td>{d.models ?? (d.error ? '读不到' : '-')}</td>
                <td>部署配置</td>
                <td className="hint-inline">内置</td>
              </tr>
            ))}
            {(data?.registered || []).map((d) => (
              <tr key={'r-' + d.key}>
                <td className="mono">{d.key}</td>
                <td>
                  {d.label || '-'}
                  <div className="hint-inline">{d.domain || ''}</div>
                </td>
                <td className="mono">{d.engine || '-'}</td>
                <td className="mono">{d.database || '-'}</td>
                <td>{d.models ?? '-'}</td>
                <td>界面登记</td>
                <td>
                  <button onClick={() => fillFor(d.key)} disabled={!!busy}>
                    编辑
                  </button>
                  <button onClick={() => doRemove(d.key)} disabled={!!busy}>
                    {busy === 'del-' + d.key ? '移除中…' : '取消登记'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="subhead">登记 / 修改</h3>
      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="ds-key">
            数据集编码
          </label>
          <input
            id="ds-key"
            className="textinput"
            placeholder="C"
            value={form.key}
            onChange={(e) => set('key', e.target.value)}
          />
          <span className="hint-inline">建议单个大写字母，别与 A/B 重复。</span>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="ds-label">
            显示名称
          </label>
          <input
            id="ds-label"
            className="textinput"
            placeholder="C 库 · 企业信贷域"
            value={form.label}
            onChange={(e) => set('label', e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="ds-domain">
            业务领域
          </label>
          <input
            id="ds-domain"
            className="textinput"
            placeholder="授信 / 放款 / 还款 / 五级分类"
            value={form.domain}
            onChange={(e) => set('domain', e.target.value)}
          />
        </div>
      </div>

      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="ds-engine">
            语义引擎
          </label>
          <select
            id="ds-engine"
            className="textinput"
            value={form.engine}
            onChange={(e) => set('engine', e.target.value)}
          >
            {['A', 'B', 'C'].map((k) => (
              <option key={k} value={k}>
                {k} · {ENGINE_HINT[k] || '自定义引擎'}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="ds-port">
            端口
          </label>
          <input
            id="ds-port"
            className="textinput"
            type="number"
            value={form.port}
            onChange={(e) => set('port', e.target.value)}
          />
          <span className="hint-inline">留 0 表示用服务端默认端口；主机地址也由服务端统一提供。</span>
        </div>
      </div>

      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="ds-dbname">
            库名
          </label>
          <input
            id="ds-dbname"
            className="textinput"
            placeholder="credit"
            value={form.dbname}
            onChange={(e) => set('dbname', e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="ds-user">
            账号
          </label>
          <input
            id="ds-user"
            className="textinput"
            value={form.user}
            onChange={(e) => set('user', e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="ds-pwd">
            口令
          </label>
          <input
            id="ds-pwd"
            className="textinput"
            type="password"
            autoComplete="off"
            placeholder="留空表示不修改已保存的"
            value={form.password}
            onChange={(e) => set('password', e.target.value)}
          />
          <span className="hint-inline">口令只进不出，登记后页面不回显。</span>
        </div>
      </div>

      <div className="field">
        <label className="field-label" htmlFor="ds-mdl">
          MDL 语义层（JSON）
        </label>
        <textarea
          id="ds-mdl"
          className="textinput"
          rows={6}
          placeholder='{"models":[{"name":"dwd_loan","columns":[{"name":"loan_id","type":"int"}]}]}'
          value={form.mdl_json}
          onChange={(e) => set('mdl_json', e.target.value)}
        />
        <span className="hint-inline">
          新库第一次登记必须贴 MDL；编辑已有数据集时可留空 = 沿用上次的。
        </span>
      </div>

      <div className="pager">
        <button onClick={doProbe} disabled={!!busy || !form.dbname.trim()}>
          {busy === 'probe' ? '探测中…' : '测试三项判据'}
        </button>
        <button className="primary" onClick={doRegister} disabled={!!busy || !canSubmit}>
          {busy === 'save' ? '登记中…' : '登记并加载'}
        </button>
        {busy ? null : canSubmit ? null : (
          <span className="hint-inline">编码、库名、MDL 三项是必填</span>
        )}
      </div>

      {probe ? (
        <div className={probe.ok ? 'okbox' : 'warnbox'}>
          <div className={probe.ok ? 'okbox-title' : 'warnbox-title'}>{probe.notice}</div>
          <ul className="notes">
            {checks.map(([k, v]) => (
              <li key={k}>
                <b>{CHECK_LABEL[k] || k}</b>：{v.ok ? '通过' : '不通过'}
                {v.ok && v.models ? `（模型 ${v.models} 个、字段 ${v.fields} 个）` : ''}
                {v.ok && v.engine_models != null ? `，引擎侧 ${v.engine_models} 个模型` : ''}
                {v.ok && v.models_match === false ? '——引擎侧还没装载这份 MDL' : ''}
                {!v.ok && v.error ? (
                  <div className="hint-inline">
                    <RichText text={String(v.error)} />
                  </div>
                ) : null}
                {!v.ok && v.hint ? (
                  <div className="hint-inline">
                    <RichText text={String(v.hint)} />
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  )
}
