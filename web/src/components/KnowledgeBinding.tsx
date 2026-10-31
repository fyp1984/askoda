import { useCallback, useEffect, useState } from 'react'
import { api, type KnowledgeSettings, type KnowledgeTestResp } from '../api/client'
import { Card, ErrorBox, Loading } from '../components/ui'
import RichText from '../components/RichText'

/**
 * 系统设置 · 知识库接入
 *
 * 为什么这一页必须存在
 * --------------------
 * 知识作用域原先只有一个开关：WeKnora 底座里 mcp_endpoints 的那一列数组。
 * 换业务域时要去改底座的库表，业务侧既看不到"现在用哪个库"，也没法自己切。
 * 做进界面之后：勾选 → 保存 → 下一次分析立即生效，不用重启、不用进后台。
 *
 * 两层作用域必须同时展示（只给一层就成了黑箱）
 * ------------------------------------------
 *  · 可见范围 = 底座 MCP 端点绑定的库，是**硬边界**，本页只能看不能改；
 *  · 生效范围 = 本页勾选的库，必须落在可见范围内。
 * 只显示"生效"时，用户遇到"库里明明有文档却搜不到"会以为功能坏了；
 * 只显示"可见"时，又不知道自己刚才的勾选到底生效没有。
 */

const SOURCE_LABEL: Record<string, string> = {
  db: '界面保存的配置',
  env: '环境变量（部署时配置）',
  none: '未接入知识库',
}

export default function KnowledgeBinding({
  readonly,
  actor,
  onNotify,
}: {
  readonly?: boolean
  actor: string
  onNotify?: (msg: string) => void
}) {
  const [data, setData] = useState<KnowledgeSettings | null>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<unknown>(null)

  const [question, setQuestion] = useState('')
  const [tested, setTested] = useState<KnowledgeTestResp | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await api.knowledgeSettings()
      setData(s)
      setPicked(s.kb_ids || [])
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const visible = data?.visible || []
  const ro = !!readonly

  const toggle = (id: string) => {
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  }

  const save = async () => {
    setBusy('save')
    setError(null)
    try {
      const r = await api.saveKnowledge({ kb_ids: picked, actor })
      setData(r)
      setPicked(r.kb_ids || [])
      onNotify?.(r.notice || '已保存')
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const resetToEnv = async () => {
    if (!window.confirm('确定删除界面保存的配置、回落环境变量？')) return
    setBusy('reset')
    setError(null)
    try {
      const r = await api.saveKnowledge({ kb_ids: [], actor, reset: true })
      setData(r)
      setPicked(r.kb_ids || [])
      onNotify?.(r.notice || '已回落环境变量')
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const runTest = async () => {
    setBusy('test')
    setTested(null)
    setError(null)
    try {
      const r = await api.testKnowledge({ question: question.trim() })
      setTested(r)
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const dirty = JSON.stringify([...picked].sort()) !== JSON.stringify([...(data?.kb_ids || [])].sort())

  return (
    <Card
      title="知识库接入"
      collapsible
      id="settings-knowledge"
      desc="选哪些库参与问答，保存即生效"
      extra={
        <>
          <button onClick={load} disabled={loading || !!busy}>
            {loading ? '读取中…' : '刷新'}
          </button>
          {!ro ? (
            <button className="primary" onClick={save} disabled={!!busy || !dirty}>
              {busy === 'save' ? '保存中…' : dirty ? '保存并生效' : '已是当前配置'}
            </button>
          ) : null}
        </>
      }
    >
      <ErrorBox error={error} />
      {loading ? <Loading tip="正在读取知识库配置…" /> : null}

      <div className="trace">
        <div>
          <span className="trace-k">生效来源</span>
          <code>{SOURCE_LABEL[data?.source || 'none'] || data?.source}</code>
        </div>
        <div>
          <span className="trace-k">生效库数</span>
          <code>{(data?.kb_ids || []).length}</code>
        </div>
        <div>
          <span className="trace-k">底座可见库</span>
          <code>{visible.length}</code>
        </div>
        <div>
          <span className="trace-k">最近修改</span>
          <code>
            {data?.updated_at ? `${data.updated_at}${data.updated_by ? ` · ${data.updated_by}` : ''}` : '-'}
          </code>
        </div>
      </div>

      {data?.db_error ? (
        <div className="warnbox">
          <div className="warnbox-title">配置存储暂不可用</div>
          <div className="warnbox-hint">
            <RichText text={`读取配置库失败，当前按环境变量执行。${data.db_error}`} />
          </div>
        </div>
      ) : null}

      {data?.visible === null ? (
        <div className="warnbox">
          <div className="warnbox-title">底座不可达，看不到知识库清单</div>
          <div className="warnbox-hint">
            <RichText text={data?.visible_error || '请确认知识库平台在线后点「刷新」。'} />
          </div>
        </div>
      ) : null}

      {visible.length > 0 ? (
        <>
          <p className="muted">
            下面是知识底座对该应用**可见**的全部库；勾选表示参与问答。不勾也可以保存——表示明确不接知识库。
          </p>
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>生效</th>
                  <th>知识库</th>
                  <th>能力</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((kb) => (
                  <tr key={kb.id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`启用 ${kb.name}`}
                        checked={picked.includes(kb.id)}
                        disabled={ro || !!busy}
                        onChange={() => toggle(kb.id)}
                      />
                    </td>
                    <td>
                      <div>{kb.name}</div>
                      <div className="mono hint-inline">{kb.id}</div>
                    </td>
                    <td className="snippet">
                      {(kb.capabilities || []).length
                        ? (kb.capabilities || []).join('、')
                        : kb.is_env_default
                          ? '环境变量默认'
                          : '-'}
                    </td>
                    <td className="snippet">{kb.description || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      <div className="field">
        <label className="field-label" htmlFor="kb-test-q">
          试检索（确认这个库里确实有内容）
        </label>
        <div className="field-row">
          <input
            id="kb-test-q"
            className="textinput"
            placeholder="例如：什么是逾期贷款？"
            value={question}
            disabled={!!busy}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <button onClick={runTest} disabled={!!busy || visible.length === 0}>
            {busy === 'test' ? '检索中…' : '试一下'}
          </button>
        </div>
        <span className="hint-inline">
          用**已保存**的配置检索（改了勾选先保存，再试）。只回显命中条数与来源文档名。
        </span>
      </div>

      {tested ? (
        <div className={tested.hits > 0 ? 'okbox' : 'warnbox'}>
          <div className={tested.hits > 0 ? 'okbox-title' : 'warnbox-title'}>
            命中 {tested.hits} 条
          </div>
          <div className={tested.hits > 0 ? 'okbox-msg' : 'warnbox-hint'}>
            <RichText text={tested.notice || ''} />
          </div>
          {tested.sources.length ? (
            <ul className="notes">
              {tested.sources.map((s, i) => (
                <li key={i}>
                  {s.document_name || '未知文档'}
                  {typeof s.similarity === 'number' ? `（相似度 ${s.similarity.toFixed(3)}）` : ''}
                  {s.snippet ? <div className="hint-inline">{s.snippet}</div> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {!ro ? (
        <div className="pager">
          <button onClick={resetToEnv} disabled={!!busy}>
            {busy === 'reset' ? '处理中…' : '删除界面配置（回落环境变量）'}
          </button>
        </div>
      ) : null}

      <ul className="notes">
        <li>
          可见范围由底座决定：在知识库平台把库绑定到本应用的 MCP 端点后，这里才会出现。
          这是**硬边界**，勾选改不了它。
        </li>
        <li>
          保存后<b>下一次检索即生效</b>，不用重启服务；正在跑的需求不受影响。
        </li>
        <li>
          引用的文档名与片段会随每次取数一起留痕，可在「出数与交付」的回放里逐条核对。
        </li>
      </ul>
    </Card>
  )
}
