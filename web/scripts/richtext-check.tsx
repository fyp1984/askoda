import { renderToStaticMarkup } from 'react-dom/server'
import RichText from '../src/components/RichText'

let pass = 0
let fail = 0
const FAILS: string[] = []

function check(name: string, input: string, want: (h: string) => boolean, desc: string) {
  const html = renderToStaticMarkup(<RichText text={input} /> as any)
  const ok = want(html)
  if (ok) pass++
  else {
    fail++
    FAILS.push(`${name}\n   输入: ${JSON.stringify(input)}\n   得到: ${JSON.stringify(html)}\n   期望: ${desc}`)
  }
}

const has = (s: string) => (h: string) => h.includes(s)
const notHas = (s: string) => (h: string) => !h.includes(s)

// ── 1. markdown 强调/代码/斜体 → 排版元素，星号反引号不得外露 ──
check('粗体', '**口径未定义**', has('<strong'), '含 <strong>')
check('粗体不留星号', '**口径未定义**', notHas('**'), '不含 **')
check('行内代码', '字段 `member_status` 未登记', has('<code'), '含 <code>')
check('代码不留反引号', '字段 `member_status` 未登记', notHas('`'), '不含反引号')
check('斜体', '这是 _强调_ 文字', has('<em'), '含 <em>')

// ── 2. HTML → 剥掉，不留标签；表格列可分辨 ──
check('剥 p 标签', '<p>会员活跃度未定义</p>', notHas('<p>'), '不含 <p>')
check('剥 code 标签', '<code>member_id</code>', notHas('<code>member_id</code>'), '不含原始 code 标签')
check('表格转分隔', '<tr><td>字段</td><td>口径</td></tr>', has(' | '), '含 | 分隔')

// ── 3. ★ 回归重点：比较符号不能被当成 HTML 标签吃掉 ──
//    注意：renderToStaticMarkup 会把文本里的 < 转义成 &lt;（React 正确行为），
//    所以断言要查转义后的形态，查的是「内容有没有被剥掉」而不是原字符。
check('小于号保留', '订单金额 < 5000', has('5000'), '保留 5000')
check('小于号本体保留', '订单金额 < 5000', has('&lt; 5000'), '完整保留「< 5000」')
check('区间比较保留', '5000 < 金额 > 1000', has('5000'), '保留 5000')
check('区间比较右端保留', '5000 < 金额 > 1000', has('1000'), '保留 1000')
check('区间比较中间词保留', '5000 < 金额 > 1000', has('金额'), '保留「金额」二字')
check('区间比较符号保留', '5000 < 金额 > 1000', has('&lt; 金额 &gt;'), '保留「< 金额 >」结构')
check('单尖括号保留', 'A < B', has('A &lt; B'), '完整保留 "A < B"')

// ── 4. markdown 标题井号与列表符号不得外露 ──
check('标题井号去除', '## 会员活跃度口径', notHas('##'), '不含 ##')
check('标题正文保留', '## 会员活跃度口径', has('会员活跃度口径'), '保留标题正文')
check('列表符号转点', '- 第一项', has('· 第一项'), '转成 · ')

// ── 5. 空值/边界 ──
check('空串不渲染', '', (h) => h === '', '输出空')
check('null 安全', null as any, (h) => h === '', '输出空')

console.log(`\n通过 ${pass} / 失败 ${fail}`)
if (FAILS.length) {
  console.log('\n失败明细：')
  FAILS.forEach((f, i) => console.log(`  ${i + 1}. ${f}`))
  process.exit(1)
}
console.log('✅ RichText 全部断言通过')
