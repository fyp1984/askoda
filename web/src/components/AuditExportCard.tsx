import { useState } from 'react'
import { api, type AuditExportResp } from '../api/client'
import { Card, ErrorBox } from './ui'

/**
 * 审计导出与对接（FR-7.7 · P2）
 *
 * 导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条在 Demo 中照常呈现**。
 * 验收要点是「审计日志可按客户 SIEM 要求导出（CSV / Syslog）」。
 *
 * 两个刻意的处理：
 * 1. **两种格式都给**——CSV 给人看（Excel 打开即核对），Syslog 给机器吃
 *    （RFC5424 风格单行，客户 SIEM 采集器直接投递）；导出格式是客户侧的现实约束，
 *    不是我们的偏好，所以不默认只给一种。
 * 2. **留存期写「目标值」**——PRD FR-7.4 的 ≥180 天带 `[假设]`，属目标而非实测，
 *    界面与导出头部都照此口径（数字纪律）。
 */
export default function AuditExportCard({ demandId, dataset }: { demandId: string; dataset: string }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [last, setLast] = useState<AuditExportResp | null>(null)
  const [scopeAll, setScopeAll] = useState(false)

  const doExport = async (fmt: 'csv' | 'syslog') => {
    setBusy(fmt)
    setError(null)
    try {
      const r = await api.auditExport({
        dataset,
        demand_id: scopeAll ? '' : demandId,
        fmt,
      })
      setLast(r)
      if (r.ok && r.content) {
        // 前端落盘：BFF 传内容、浏览器造下载，避免后端再存一份审计文件
        const mime = fmt === 'csv' ? 'text/csv;charset=utf-8' : 'text/plain;charset=utf-8'
        const blob = new Blob([r.content], { type: mime })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = r.filename || `askoda-audit.${fmt === 'csv' ? 'csv' : 'log'}`
        a.click()
        URL.revokeObjectURL(url)
      }
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card
      title="审计导出与对接"
      tone="muted"
      collapsible
      defaultOpen={false}
      id="delivery-audit-export"
      extra={<span className="muted">FR-7.7 · P2</span>}
    >
      <p className="muted">
        按客户 SIEM 要求导出审计日志：<b>CSV</b> 供人工核对，<b>Syslog</b>（RFC5424 单行）
        供采集器直接投递。字段对齐全链路审计要素（提问人 / 问题 / 生成 SQL / 引用口径版本 /
        扫及表 / 执行耗时 / 返回行数 / 试跑次数 / 是否人工改写）。导出动作本身也留痕。
      </p>

      <label className="checkline">
        <input type="checkbox" checked={scopeAll} onChange={(e) => setScopeAll(e.target.checked)} />
        <span>导出全量审计（不限本条需求）；不勾选时只导出当前需求 {demandId} 的记录</span>
      </label>

      <div className="followup-actions">
        <button type="button" className="primary" onClick={() => doExport('csv')} disabled={!!busy}>
          {busy === 'csv' ? '导出中…' : '导出 CSV'}
        </button>
        <button type="button" onClick={() => doExport('syslog')} disabled={!!busy}>
          {busy === 'syslog' ? '导出中…' : '导出 Syslog'}
        </button>
      </div>

      <ErrorBox error={error} />

      {last?.ok ? (
        <div className="okbox">
          <div className="okbox-title">
            已导出 {last.count} 条 · {last.format === 'csv' ? 'CSV' : 'Syslog'}
          </div>
          <div className="okbox-msg">
            文件名 {last.filename}。留存期 {last.retention}。
          </div>
        </div>
      ) : null}

      {last?.ok && last.content ? (
        <details className="fold">
          <summary>预览导出内容（前若干行）</summary>
          <pre className="sqlbox small">{last.content.split('\n').slice(0, 12).join('\n')}</pre>
        </details>
      ) : null}
    </Card>
  )
}
