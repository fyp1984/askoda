import type { ReactNode } from 'react'

/**
 * 轻量文本格式化 —— 工程内所有「后端来的文本」的统一出口
 *
 * 为什么需要（用户 2026-10-08 两次指出「前端不能展示 Markdown 或 HTML 标签内容」）：
 *   后端结论文本会用 markdown 强调 `**口径未定义**`、行内代码 `` `member_status` ``；
 *   而这些文本最终来自**知识库原文**，里面还夹着 HTML（表格型 chunk 常见）：
 *     `<table><thead><tr><th>字段</th>…</table>`、`<code>member_id</code>`、`<p>`、`<br>`
 *   前端此前无任何渲染器 → 星号、反引号、`<table>` 全部原样透出。
 *
 * 做法：把标记**转成排版元素**，而不是让浏览器去解释 HTML。
 *   · markdown 强调/斜体/代码 → <strong>/<em>/<code>
 *   · markdown 标题井号/列表符号 → 去掉符号保留内容
 *   · HTML 标签 → 转成可读文本（表格单元格用 `|` 分隔），**不注入 DOM**
 *   · 连续空行压缩
 *
 * 不引第三方 markdown 库（会让包体再大一份），不使用 dangerouslySetInnerHTML（避免 XSS）。
 *
 * ★ 实现要点（都是踩过的坑，勿简化）：
 *   ① 占位符必须**互不相同且 ASCII 可读**——用相同前缀会让正则互相吃掉内容。
 *   ② 抽标记与渲染分成两步：先全部替换成token，最后一步才渲染；
 *      混在一起做时，后一步的正则会把前一步刚生成的 token 当正文改坏。
 */

/** 占位前缀（互不相同；ASCII 可读，便于调试与搜索） */
const T_BOLD = '｟b｠'
const T_ITAL = '｟i｠'
const T_CODE = '｟c｠'

export default function RichText({ text }: { text?: string | null }): ReactNode {
  if (text === null || text === undefined || text === '') return null
  let s = String(text)

  // ① HTML → 可读文本。先处理表格结构，保证列与列可分辨
  s = s
    .replace(/<\/?(?:table|thead|tbody|tr)\s*>/gi, '\n')
    .replace(/<\/?(?:td|th)\b[^>]*>/gi, ' | ')
    .replace(/<\/?(?:p|div|br|li|ul|ol|h[1-6])\b[^>]*>/gi, '\n')
    // ★ 兜底只剥「长得像标签的」：`<` 后面必须**紧跟字母或 /字母**。
    //   原来的 `/<[^>]{1,300}>/g` 会把业务文本里的比较符当标签吃掉——
    //   例如「订单金额 < 5000 且 > 1000」会被整段抹成「订单金额  1000」。
    .replace(/<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]{0,300})?\/?>/g, '')

  // ② markdown 标题井号 / 列表符号 / 回车（先做，避免影响后续行内规则）
  s = s
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '· ')
    .replace(/\r/g, '')

  // ③ 抽取三类行内标记 → token（此阶段不渲染，只放占位）
  const store: { b: string[]; i: string[]; c: string[] } = { b: [], i: [], c: [] }

  s = s.replace(/`([^`\n]{1,160})`/g, (_m, t) => {
    store.c.push(t)
    return `${T_CODE}${store.c.length - 1}${T_CODE}`
  })
  s = s.replace(/\*\*([^*\n]{1,300})\*\*/g, (_m, t) => {
    store.b.push(t)
    return `${T_BOLD}${store.b.length - 1}${T_BOLD}`
  })
  s = s.replace(/(^|[\s(（])_([^_\n]{1,300})_(?=$|[\s,.;，。；）)])/g, (_m, pre, t) => {
    store.i.push(t)
    return `${pre}${T_ITAL}${store.i.length - 1}${T_ITAL}`
  })

  // ④ token 回填成带类型的内容（仍不渲染，最后统一处理）
  s = s
    .replace(new RegExp(`${T_CODE}(\\d+)${T_CODE}`, 'g'), (_m, n) => `${T_CODE}${store.c[Number(n)] ?? ''}${T_CODE}`)
    .replace(new RegExp(`${T_BOLD}(\\d+)${T_BOLD}`, 'g'), (_m, n) => `${T_BOLD}${store.b[Number(n)] ?? ''}${T_BOLD}`)
    .replace(new RegExp(`${T_ITAL}(\\d+)${T_ITAL}`, 'g'), (_m, n) => `${T_ITAL}${store.i[Number(n)] ?? ''}${T_ITAL}`)

  // ⑤ 空白收整
  s = s.replace(/[ \t]{2,}/g, ' ').replace(/\n{3,}/g, '\n\n').trim()

  // ⑥ 按行渲染，保留换行结构
  const lines = s.split('\n')
  return (
    <>
      {lines.map((ln, i) => (
        <span key={i} className="rt-line">
          {renderInline(ln)}
          {i < lines.length - 1 ? '\n' : null}
        </span>
      ))}
    </>
  )
}

/** 行内解析：把 token 切成 React 元素（不使用 dangerouslySetInnerHTML） */
function renderInline(line: string): ReactNode {
  if (!line) return null
  const re = new RegExp(
    `${T_BOLD}([\\s\\S]*?)${T_BOLD}|${T_ITAL}([\\s\\S]*?)${T_ITAL}|${T_CODE}([\\s\\S]*?)${T_CODE}`,
    'g'
  )
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push(line.slice(last, m.index))
    if (m[1] !== undefined) out.push(<strong key={k++} className="rt-strong">{m[1]}</strong>)
    else if (m[2] !== undefined) out.push(<em key={k++} className="rt-em">{m[2]}</em>)
    else out.push(<code key={k++} className="rt-code">{m[3]}</code>)
    last = re.lastIndex
  }
  if (last < line.length) out.push(line.slice(last))
  return out.length ? out : line
}