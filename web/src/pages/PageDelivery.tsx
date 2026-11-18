import { useState } from 'react'
import {
  api,
  type ClassificationPoliciesResp,
  type E2eStatus,
  type SqlExecResp,
  type SqlResp,
} from '../api/client'
import { Card, ErrorBox, Loading, PartialErrorsBox } from '../components/ui'
import { Section } from '../components/Collapsible'
import RichText from '../components/RichText'
import DemandStatusActions from '../components/DemandStatusActions'
import AuditReplay from '../components/AuditReplay'
import AuditExportCard from '../components/AuditExportCard'
import ResultChart from '../components/ResultChart'
import { fmtReqVer, genLabel } from '../lib/display'

/**
 * 菜单⑤ · SQL 联调 · 交付
 *
 * PRD §12.7：测试库联调、迭代记录、结果可视化核查、生产脚本导出。
 * 关键交互：联调通过且门禁全过后才可导出；导出脚本自带口径依据与门禁记录头注。
 */
export default function PageDelivery({
  demandId,
  dataset,
  status,
  onRefresh,
}: {
  demandId: string | null
  dataset: string
  status: E2eStatus | null
  onRefresh: () => void
}) {
  const [candidate, setCandidate] = useState('')
  const [sql, setSql] = useState<SqlResp | null>(null)
  const [exec, setExec] = useState<SqlExecResp | null>(null)
  const [busy, setBusy] = useState<'gen' | 'exec' | null>(null)
  const [error, setError] = useState<unknown>(null)
  // 结果区视图：图表 / 表格。部分用户习惯直接看数字，所以两个视图都保留，且可一键切换。
  const [view, setView] = useState<'chart' | 'table'>('chart')
  // 分级分类策略清单（数据管家视角）：按需拉取
  const [policies, setPolicies] = useState<ClassificationPoliciesResp | null>(null)
  const [policyBusy, setPolicyBusy] = useState(false)

  if (!demandId) {
    return (
      <div className="page">
        <Card title="出数与交付">
          <p className="muted">还没有需求。请先到「提个需求」页提交一条。</p>
        </Card>
      </div>
    )
  }

  const sqlText = sql?.revised_sql || sql?.sql_draft || ''

  const gen = async () => {
    setBusy('gen')
    setError(null)
    try {
      const r = await api.sqlGenerate(demandId, dataset, candidate.trim() || undefined)
      setSql(r)
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const run = async () => {
    setBusy('exec')
    setError(null)
    try {
      const r = await api.sqlExecute(demandId, dataset, sqlText || candidate.trim() || undefined)
      setExec(r)
      onRefresh()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const cols: string[] = exec?.validation_result?.columns || []
  const rows: unknown[][] = exec?.sample_rows || []
  const runs: any[] = status?.runs?.items || []
  const masking = exec?.masking
  const reviewSummary = status?.review_summary

  const loadPolicies = async () => {
    setPolicyBusy(true)
    try {
      setPolicies(await api.classificationPolicies(dataset))
    } catch (e) {
      setError(e)
    } finally {
      setPolicyBusy(false)
    }
  }

  // 后端只回传前若干行样本（实测真实 9 行时 sample_rows 仅 5 行）。
  // 这里如实告知用户「图上画的是样本，不是全量」，不谎称已展示全部数据。
  const totalRows = exec?.row_count ?? exec?.validation_result?.row_count ?? rows.length
  const truncated = rows.length > 0 && totalRows > rows.length

  // ---- 生产脚本导出（头注：口径依据 + 门禁记录 + 溯源三件套）----
  const buildExport = () => {
    const layers = (exec?.review?.layers || sql?.review?.layers || []) as any[]
    const gateLines = layers.map(
      (l) => `--   ${l.layer} ${l.name}: ${l.skipped ? '跳过' : l.ok ? '通过' : '未通过'} — ${l.detail}`,
    )
    const ctx: any = status?.context_pack || {}
    const analysis: any = status?.analysis || {}
    const head = [
      '-- ============================================================',
      '-- Askoda 生产脚本导出',
      `-- demand_id      : ${demandId}`,
      `-- dataset        : ${dataset}`,
      `-- sql_run_id     : ${exec?.sql_run_id || '-'}`,
      `-- schema_version : ${exec?.schema_version || ctx?.schema_version || '-'}`,
      `-- requirement_ver: ${exec?.requirement_version ?? '-'}`,
      `-- pack_version   : ${exec?.pack_version || ctx?.pack_version || '-'}`,
      `-- 导出时间       : ${new Date().toISOString()}`,
      '--',
      '-- 门禁结论（各层逐项如下，标注「跳过」者为本期未启用）：',
      ...(gateLines.length > 0 ? gateLines : ['--   （无门禁记录）']),
      `-- 门禁总状态       : ${exec?.review?.status || sql?.review?.status || '-'}`,
      '--',
      '-- 口径依据：',
      `--   需求标题  : ${status?.demand?.title || '-'}`,
      `--   需求说明  : ${status?.demand?.description || '-'}`,
      `--   语义置信  : ${analysis?.confidence_overall ?? '-'}`,
      '--',
      '-- 脱敏说明：本脚本是「复制到自有客户端执行」的交付路径，**不携带脱敏逻辑、不得宣称已脱敏**；',
      '--           系统内的展示与导出结果才按分级分类在应用端（网关/BFF）自动脱敏。',
      '-- 人工复核：全量人工复核、不看置信度分流；未经人工在真实环境预测试确认，不得标注「可执行」。',
      '--',
      '-- 注意：本脚本由受控链路生成并通过门禁，导出后请在生产库复核后再执行。',
      '-- ============================================================',
      '',
    ].join('\n')
    const body = (sqlText || candidate.trim() || '-- （暂无 SQL）').trim()
    return `${head}${body}\n`
  }

  const download = () => {
    const text = buildExport()
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${demandId}-delivery.sql`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="page">
      <Card
        title={`出数与交付 · ${demandId}`}
        collapsible
        id="delivery-main"
        extra={
          <>
            <button onClick={gen} disabled={busy === 'gen'}>
              {busy === 'gen' ? '生成中…' : '生成 SQL'}
            </button>
            <button className="primary" onClick={run} disabled={busy === 'exec' || (!sqlText && !candidate.trim())}>
              {busy === 'exec' ? '执行中…' : '执行取数'}
            </button>
          </>
        }
      >
        <ErrorBox error={error} />
        <PartialErrorsBox errors={status?.partial_errors || []} />

        {/* 质量内核：未经人工确认，不得标注「可执行」（FR-6.7 / AC-13 / D15） */}
        {reviewSummary && reviewSummary.ok !== false ? (
          reviewSummary.all_approved ? (
            <div className="okbox">
              <div className="okbox-title">人工复核已通过</div>
              <div className="okbox-msg">
                本需求共 {reviewSummary.total ?? 0} 条 SQL 已全部复核通过，可以标注「可执行」。
              </div>
            </div>
          ) : (
            <div className="warnbox">
              <div className="warnbox-title">人工复核未完成 · 不得标注「可执行」</div>
              <div className="warnbox-hint">
                本需求共 {reviewSummary.total ?? 0} 条 SQL：待复核 {reviewSummary.counts?.待复核 ?? 0} 条、
                已退回 {reviewSummary.counts?.已退回 ?? 0} 条。全量人工复核、不看置信度分流；
                未经人工在真实环境预测试确认，不得标注「可执行」。请到左侧「人工复核」逐条处理。
              </div>
            </div>
          )
        ) : null}

        {busy === 'gen' ? <Loading tip="正在做取数计划与门禁校验…" /> : null}
        {busy === 'exec' ? <Loading tip="正在只读执行…" /> : null}

        <p className="muted">
          本页直接对库取数（不经大模型）。只有已建模口径能自动生成，未命中会被拦下（设计如此）；
          涉及新维度时，可填入分析人员提供的候选 SQL，同样过校验再执行。
        </p>
        <textarea
          className="sqlinput"
          rows={4}
          placeholder="（可选）粘贴候选 SQL，留空则由内置确定性规划器尝试生成"
          value={candidate}
          onChange={(e) => setCandidate(e.target.value)}
        />

        {sqlText ? (
          <Section title="待执行 SQL" id="delivery-pending">
            <pre className="sqlbox">{sqlText}</pre>
          </Section>
        ) : null}

        {exec ? (
          <Section title="执行结果" id="delivery-exec">
            <p className="muted"><RichText text={exec.execution_summary} /></p>
            <p>
              交付判定：
              {exec.deliverable ? (
                <span className="chip chip-green">可交付</span>
              ) : (
                <span className="chip chip-red">不可交付</span>
              )}
              <span className="muted"> <RichText text={exec.deliverable_reason} /></span>
            </p>
          </Section>
        ) : null}
      </Card>

      {exec ? (
        <Card
          title={`执行结果 · ${rows.length} 行样本 × ${cols.length} 列`}
          collapsible
          id="delivery-rows"
          count={rows.length}
          extra={
            <div className="seg" role="group" aria-label="结果视图切换">
              <button className={view === 'chart' ? 'seg-btn on' : 'seg-btn'} onClick={() => setView('chart')}>
                图表
              </button>
              <button className={view === 'table' ? 'seg-btn on' : 'seg-btn'} onClick={() => setView('table')}>
                表格
              </button>
            </div>
          }
        >
          {cols.length === 0 ? (
            <p className="muted">无样本行。</p>
          ) : view === 'chart' ? (
            <>
              <ResultChart columns={cols} rows={rows} />
              {truncated ? (
                <p className="notice">
                  本次共返回 {totalRows} 行，图表基于其中 {rows.length} 行样本绘制（未聚合、未补齐）；如需核对全部数字，请切到「表格」。
                </p>
              ) : null}
            </>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    {cols.map((c, i) => (
                      <th key={i}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i}>
                      {r.map((c, j) => (
                        <td key={j}>{String(c ?? '')}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ) : null}

      <Card
        title="生产脚本导出"
        collapsible
        id="delivery-export"
        extra={
          <button onClick={download} disabled={!sqlText && !candidate.trim()}>
            下载 .sql
          </button>
        }
      >
        <p className="muted">
          导出脚本自带口径依据、门禁记录与溯源版本号，便于在生产库复核后执行。
        </p>
        <details className="fold">
          <summary>预览导出内容</summary>
          <pre className="sqlbox small">{buildExport()}</pre>
        </details>
      </Card>

      {/* 分级分类自动脱敏（FR-7.8 / FR-7.9）：落点在**应用端**，不在 SQL 执行端 */}
      <Card
        title="分级分类脱敏 · 应用端"
        collapsible
        id="delivery-masking"
        extra={
          <button type="button" onClick={loadPolicies} disabled={policyBusy}>
            {policyBusy ? '加载中…' : '查看分级策略'}
          </button>
        }
      >
        <p className="muted">
          展示与导出结果按分级分类<b>在应用端（网关/BFF 编排层）自动脱敏</b>，不在 SQL 执行端。
          策略源不可用、字段未匹配到分级标准时，一律按<b>最高敏感级</b>处理（失败即关闭，业务侧不可关闭）。
        </p>

        {!masking ? (
          <p className="muted">执行一次取数后，这里会显示本次结果的逐列分级与脱敏情况。</p>
        ) : (
          <>
            {masking.sealed ? (
              <div className="warnbox">
                <div className="warnbox-title">策略源不可用：已按最高敏感级关闭（失败即关闭）</div>
              </div>
            ) : null}
            <p className="muted">{masking.location}</p>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>列</th>
                    <th>分级</th>
                    <th>分类</th>
                    <th>脱敏算法</th>
                    <th>来源</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {(masking.per_column || []).map((c, i) => {
                    const closed = c.source === 'closed-by-default'
                    return (
                      <tr key={i} className={closed ? 'row-bad' : c.algorithm === 'none' ? 'row-ok' : undefined}>
                        <td className="mono">{c.column}</td>
                        <td>
                          <span className={c.level === 'L4' ? 'chip chip-red' : c.level === 'L3' ? 'chip chip-amber' : 'chip chip-muted'}>
                            {c.level} {c.level_label}
                          </span>
                        </td>
                        <td>{c.category || '-'}</td>
                        <td>{c.algorithm_label || c.algorithm}</td>
                        <td>{closed ? '失败即关闭' : c.source === 'builtin' ? '内置策略' : '策略表'}</td>
                        <td><RichText text={c.description || c.reason || '-'} /></td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <p className="muted">
              默认交付路径为「复制 SQL + 口径说明」到自有客户端执行——该路径<b>不携带脱敏逻辑</b>，
              也<b>不得宣称已脱敏</b>。
            </p>
          </>
        )}

        {policies ? (
          <Section
            title="分级策略清单"
            id="delivery-policies"
            count={policies.count ?? 0}
            defaultOpen={false}
            desc="策略表（可维护）优先，内置默认策略兜底；按顺序匹配列名，第一条命中即生效"
          >
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>分级</th>
                    <th>分类</th>
                    <th>匹配表达式</th>
                    <th>算法</th>
                    <th>来源</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {(policies.items || []).map((p) => (
                    <tr key={p.policy_id}>
                      <td><span className="chip chip-muted">{p.level}</span></td>
                      <td>{p.category || '-'}</td>
                      <td className="mono">{p.pattern}</td>
                      <td>{p.algorithm}</td>
                      <td>{p.source === 'builtin' ? '内置' : '策略表'}</td>
                      <td><RichText text={p.description || '-'} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        ) : null}
      </Card>

      {/* 演示口径与边界：数字纪律 + 私有化边界 + 方言范围 */}
      <Card title="演示口径与边界说明" collapsible id="delivery-scope" defaultOpen={false}>
        <Section title="数字口径（数字纪律）" id="delivery-numbers" defaultOpen>
          <p className="muted">
            下列数字在需求文档中带 <code>[假设]</code> / <code>[待核实]</code> 标记，
            Demo 中一律表述为<b>目标值 / 演示参数</b>，<b>不得写成实测结果</b>。
          </p>
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>值</th>
                  <th>标记</th>
                </tr>
              </thead>
              <tbody>
                <tr><td>SQL 可直接执行率（验收底线）</td><td>≥95%（50 条真实历史需求回放）</td><td>目标，非实测</td></tr>
                <tr><td>意图结构化正确率</td><td>≥80%（20 人抽样）</td><td><code>[假设]</code></td></tr>
                <tr><td>端到端生成时延 P95</td><td>≤30 秒</td><td><code>[假设]</code></td></tr>
                <tr><td>1000 表全量元数据采集</td><td>≤30 分钟</td><td><code>[假设]</code></td></tr>
                <tr><td>审计日志留存</td><td>≥180 天</td><td><code>[假设]</code></td></tr>
                <tr><td>并发会话</td><td>20 并发 / 单实例</td><td>目标</td></tr>
                <tr><td>EXPLAIN 阻断阈值</td><td>1000 万行</td><td><code>[待核实 per DB]</code></td></tr>
              </tbody>
            </table>
          </div>
        </Section>

        <Section title="安全与部署边界" id="delivery-boundary" defaultOpen>
          <ul className="scope-list">
            <li><b>数据不出网</b>：私有化部署，只调内网私有化大模型；数据 / 提示词 / 上下文 / SQL 均不出网。</li>
            <li><b>脱敏在应用端</b>：系统内展示与导出按分级分类自动脱敏；默认交付路径（复制 SQL + 口径说明到自有客户端执行）不携带脱敏逻辑、不得宣称已脱敏。</li>
            <li><b>失败即关闭</b>：分级标准缺失 / 字段未匹配 / 策略源不可用时，一律按最高敏感级处理，不提供业务侧关闭开关。</li>
            <li><b>方言不承诺范围</b>：存储过程 / 复杂 UDF / 动态 SQL / 跨库联邦查询不在兼容范围，返回「不支持」而非给出错误 SQL。</li>
            <li><b>合规预检与自动脱敏为新增能力</b>：原型未实现，须新建；不得拿原型现状冒充已实现。</li>
            <li><b>Demo 全量 ≠ 开发全量</b>：Demo 追求「讲得全、看得懂」；开发按里程碑分批实现、逐条满足验收判据。</li>
          </ul>
        </Section>
      </Card>

      <Card title="迭代记录" collapsible id="delivery-iter">
        {runs.length === 0 ? (
          <p className="muted">暂无取数记录。</p>
        ) : (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>sql_run_id</th>
                  <th>生成器</th>
                  <th>门禁</th>
                  <th>需求版本</th>
                  <th>生成时间</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.sql_run_id}>
                    <td className="mono">{r.sql_run_id}</td>
                    <td>{genLabel(r.generator_model)}</td>
                    <td>{r.review_status}</td>
                    <td>{fmtReqVer(r.requirement_version)}</td>
                    <td className="mono">{r.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 审计回放：把「迭代记录」那张留痕表还原成可对照复算的完整判断链 */}
      <AuditReplay demandId={demandId} />

      {/* 审计导出与对接（FR-7.7 · P2）：CSV / Syslog 两种交付格式，供客户 SIEM 对接 */}
      <AuditExportCard demandId={demandId} dataset={dataset} />

      {/* 取到数之后的下一步：把需求收口到终态「已通过」，或退回让业务方改 */}
      <DemandStatusActions
        demandId={demandId}
        currentStatus={status?.demand?.status}
        onDone={onRefresh}
      />
    </div>
  )
}