import { useCallback, useEffect, useState } from 'react'
import {
  api,
  type GateLayer,
  type ReplaySnapshot,
  type ReplayVersion,
  type SqlRunReplayResp,
} from '../api/client'
import { Card, ErrorBox, Loading } from './ui'

/**
 * 审计回放（技术人员视角的「完整判断链」）
 *
 * 为什么需要这一块：页面上原有「迭代记录」只是一张留痕表——告诉你"跑过、
 * 审过、什么时候"，但**看不到当时依据什么做出判断**。技术人员的真实疑问是
 * "这条数为什么长这样"，那需要把当次的输入 → 版本 → SQL → 门禁 → 结果
 * 一次性摊开。网关 `sql_run_replay` 本来就能按版本精确复原这条链，
 * 但此前 BFF 没转发、前端零入口。
 *
 * 两个刻意的设计：
 * 1. **版本号由后端算**。网关的 version 是「第 N 次执行（从 1 起）」，
 *    而执行记录列表按时间倒序返回——方向相反。这里不复用列表下标，
 *    而是回传后端算好的 `version_idx`，避免选第 3 行却回放成第 20 次。
 * 2. **快照 ≠ 当前态**。回放内容来自历史留痕，不受此后元数据/知识库变动影响。
 *    这一点必须写在界面上，否则会被误读成"现在再跑一次的结果"。
 */
export default function AuditReplay({ demandId }: { demandId: string }) {
  const [data, setData] = useState<SqlRunReplayResp | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [snapshot, setSnapshot] = useState<ReplaySnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  // 切换需求时清空，避免上一个需求的快照挂在新需求名下（串档很难看出来）
  useEffect(() => {
    setData(null)
    setSelected(null)
    setSnapshot(null)
    setError(null)
  }, [demandId])

  const load = useCallback(
    async (version = 0) => {
      setBusy(true)
      setError(null)
      try {
        const r = await api.sqlRunReplay(demandId, version)
        setData(r)
        // 首次加载（version=0）直接展示返回的那一次；
        // 后续点选由调用方传入明确版本号。
        if (r.replay) {
          setSelected(r.replay.version_idx)
          setSnapshot(r.replay)
        } else {
          setSelected(null)
          setSnapshot(null)
        }
      } catch (e) {
        setError(e)
      } finally {
        setBusy(false)
      }
    },
    [demandId],
  )

  // 进入页面自动拉最近一次：技术人员的默认问题就是"最新那条数是怎么来的"
  useEffect(() => {
    void load(0)
  }, [load])

  const pick = async (v: ReplayVersion) => {
    if (v.version_idx === selected) return
    await load(v.version_idx)
  }

  const versions = data?.versions || []
  const layers: GateLayer[] = (snapshot?.review?.detail?.layers || []) as GateLayer[]
  const structReq = snapshot?.input?.structured_requirement

  return (
    <Card
      title={`审计回放 · ${demandId}`}
      tone="muted"
      extra={
        <button onClick={() => load(0)} disabled={busy}>
          {busy ? '回放中…' : '刷新'}
        </button>
      }
    >
      <ErrorBox error={error} />
      {busy && !data ? <Loading tip="正在复原执行快照…" /> : null}

      <p className="muted">
        按版本精确复原当次运行的完整判断链：当时的结构化需求 → SQL → 五层门禁 → 结果 → 知识引用。
        快照来自历史留痕，<strong>不受此后元数据或知识库变动影响</strong>。
      </p>

      {!busy && versions.length === 0 ? (
        <p className="muted">{data?.notice || '该需求还没有任何 SQL 执行记录，无可回放。'}</p>
      ) : null}

      {versions.length > 0 ? (
        <>
          <h4 className="sec">执行记录（{data?.total} 次）</h4>
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>版本</th>
                  <th>sql_run_id</th>
                  <th>审查结论</th>
                  <th>是否采用</th>
                  <th>时间</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => {
                  const active = v.version_idx === selected
                  const failed = v.review_status === '不通过'
                  return (
                    <tr
                      key={v.sql_run_id}
                      className={active ? 'row-ok' : failed ? 'row-bad' : ''}
                    >
                      <td className="mono">第 {v.version_idx} 次</td>
                      <td className="mono">{v.sql_run_id}</td>
                      <td>
                        {failed ? (
                          <span className="chip chip-red">{v.review_status}</span>
                        ) : (
                          <span className="chip chip-green">{v.review_status || '-'}</span>
                        )}
                      </td>
                      <td>{v.adopted ? '已采用' : '未采用'}</td>
                      <td className="mono">{v.created_at}</td>
                      <td>
                        <button onClick={() => pick(v)} disabled={busy || active}>
                          {active ? '正在看' : '回放'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {snapshot ? (
        <>
          <h4 className="sec">
            第 {snapshot.version_idx} 次的完整判断链
            <span className="chip chip-muted">{snapshot.sql_run_id}</span>
          </h4>
          <p className="muted">
            执行时间 {snapshot.created_at || '-'} · 操作人 {snapshot.actor || '-'} ·
            门禁总状态{' '}
            {snapshot.review?.status === '不通过' ? (
              <span className="chip chip-red">{snapshot.review?.status}</span>
            ) : (
              <span className="chip chip-green">{snapshot.review?.status || '-'}</span>
            )}
          </p>

          {/* 复原链条的顺序即判断顺序，逐步给证据 */}
          <div className="trace">
            <div>
              <span className="trace-k">pack_version</span>
              <span className="mono">{snapshot.input?.pack_version || '-'}</span>
            </div>
            <div>
              <span className="trace-k">schema_version</span>
              <span className="mono">{snapshot.input?.schema_version || '-'}</span>
            </div>
            <div>
              <span className="trace-k">requirement_version</span>
              <span className="mono">{snapshot.input?.requirement_version ?? '-'}</span>
            </div>
            <div>
              <span className="trace-k">结果行数</span>
              <span className="mono">{snapshot.result?.row_count ?? '-'}</span>
            </div>
          </div>

          <p className="sec">① 当时的结构化需求</p>
          {structReq ? (
            <details className="fold">
              <summary>展开查看（这决定了系统当时"以为"用户要什么）</summary>
              <pre className="sqlbox small">{JSON.stringify(structReq, null, 2)}</pre>
            </details>
          ) : (
            <p className="muted">
              当次运行没有关联到结构化需求快照——通常是该次由调试夹具直接写入，
              未经「需求结构化」环节。
            </p>
          )}

          <p className="sec">② 当时执行的 SQL</p>
          <pre className="sqlbox">{snapshot.sql?.final_delivery_sql || snapshot.sql?.generated_sql || '（无）'}</pre>
          {snapshot.sql?.adopted === false ? (
            <p className="muted">该版本未被采用为交付脚本（通常是门禁未通过）。</p>
          ) : null}

          <p className="sec">③ 门禁各层结论（{layers.length} 层）</p>
          {layers.length === 0 ? (
            <p className="muted">当次没有留下门禁层记录。</p>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>层</th>
                    <th>检查项</th>
                    <th>结论</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {layers.map((l, i) => (
                    <tr key={`${l.layer}-${i}`} className={l.ok && !l.skipped ? 'row-ok' : 'row-bad'}>
                      <td className="mono">{l.layer}</td>
                      <td>{l.name}</td>
                      <td>
                        {l.skipped ? (
                          <span className="chip chip-muted">跳过</span>
                        ) : l.ok ? (
                          <span className="chip chip-green">通过</span>
                        ) : (
                          <span className="chip chip-red">未通过</span>
                        )}
                      </td>
                      <td className="snippet">{l.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="sec">④ 结果断言</p>
          {snapshot.result?.suspicious_signals && snapshot.result.suspicious_signals.length > 0 ? (
            <div className="backfill">
              <p className="bad-text">
                检出 {snapshot.result.suspicious_signals.length} 项可疑信号：
              </p>
              <pre className="sqlbox small">
                {JSON.stringify(snapshot.result.suspicious_signals, null, 2)}
              </pre>
            </div>
          ) : (
            <p className="ok-text">未检出可疑信号。</p>
          )}
          <details className="fold">
            <summary>展开查看结果校验原始记录</summary>
            <pre className="sqlbox small">
              {JSON.stringify(snapshot.result?.validation_result || {}, null, 2)}
            </pre>
          </details>

          <p className="sec">
            ⑤ 知识引用（{(snapshot.knowledge_citations || []).length} 条）
          </p>
          {(snapshot.knowledge_citations || []).length === 0 ? (
            <p className="muted">当次没有登记知识引用留痕。</p>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>文档</th>
                    <th>片段</th>
                    <th>检索问题</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {(snapshot.knowledge_citations || []).map((c) => (
                    <tr key={c.citation_id}>
                      <td>{c.document_name || <span className="muted">（无文档名）</span>}</td>
                      <td className="mono">{c.chunk_id || '-'}</td>
                      <td className="snippet">{c.question || '-'}</td>
                      <td>
                        {c.retired_at ? (
                          <span className="chip chip-muted">已失效</span>
                        ) : (
                          <span className="chip chip-green">有效</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {(snapshot.gaps || []).length > 0 ? (
            <>
              <p className="sec">快照缺口</p>
              <div className="warnbox">
                <div className="warnbox-title">以下内容当次未能复原</div>
                <ul className="warnbox-list">
                  {(snapshot.gaps || []).map((g, i) => (
                    <li key={i}>
                      <span className="warnbox-msg">{g}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </>
          ) : null}
        </>
      ) : null}
    </Card>
  )
}
