import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

/**
 * 菜单⑤ · 交付页 · 执行结果自动图表
 *
 * 设计前提（决定了下述全部选型规则）：
 * 1) 使用者是业务客户，不是分析师 —— **不许让他选图型**，系统按数据特征自己判断，
 *    并把「为什么选这个图」写在图上方（`reason`），保证可解释、可预期。
 * 2) 图表必须**忠实反映 sample_rows**：不做插值、不补零、不改顺序、不聚合。
 *    sample_rows 是被后端截断过的样本（实测真实 9 行只给 5 行），
 *    所以图上标注的是「本次返回的 N 行样本」，不谎称是全量。
 * 3) 空数据 / 单行 / 无可用数值列一律优雅降级为文字提示，绝不白屏或抛错。
 *
 * 真实数据形态（dataset=B，三条演示样本实测，见文件末尾注释）：
 *   - 数值列在 JSON 里是**字符串**（`"5988.5"`），不是 number —— 解析必须容忍类型。
 *   - 日期列形如 `2026-09-01`，且列名同时含「月份」语义（`stat_month`）。
 *   - 存在两列文本 + 一列日期 + 一列数值（store_name / stat_month / 复购用户数）。
 */

/** 单列最多参与绘图的取值数：超过说明这是明细流水，聚合展示会失真，交给表格。 */
const MAX_CATEGORIES = 15
/** 饼图的分块上限：超过 8 块人眼已经无法逐一对应图例，改用柱状。 */
const MAX_PIE_SLICES = 8
/** 时间序列的最小点数：2 点连线没有趋势意义。 */
const MIN_TREND_POINTS = 3

const PALETTE = [
  '#1f5fd0',
  '#c8342b',
  '#1c7a4a',
  '#9a6400',
  '#6b4fbb',
  '#0d7d8c',
  '#8a5a2b',
  '#4a5b6a',
]

export type ChartKind = 'line' | 'bar' | 'groupedBar' | 'pie' | 'hbar' | 'fallbackBar'

export type ChartSpec = {
  kind: ChartKind
  /** 「自动识别为 …→ 图型名」，直接展示给用户看。 */
  reason: string
  /** 数据不足时给出的人话解释（含行数）。 */
  degraded?: string
}

/** 一列数据的类型推断结果。 */
type ColKind = 'date' | 'number' | 'category'

/** 列名语义 → 日期列。列名是业务语义最可靠的信号，优先于值形态。 */
const DATE_NAME_RE = /(日期|时间|月份|周|年|月|日|date|month|time|day|week|year|dt)/i
/** 值形态 → 日期列。只认ISO 风格，避免把 `20260901` 之类的编号误判成日期。 */
const DATE_VALUE_RE = /^\d{4}-\d{2}(-\d{2})?$/

/**
 * 把一个单元格解析成数值。
 * 必须容忍后端把数字序列化成字符串（实测 `"5988.5"` / `"0"` / `"1.0"`），
 * 并去掉千分位逗号与百分号。解析不了返回 null（而不是 NaN，NaN 会污染坐标轴）。
 */
function toNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'bigint') return Number(v)
  if (typeof v !== 'string') return null
  const t = v.trim().replace(/,/g, '').replace(/%$/, '')
  if (t === '' || !/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(t)) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

function toText(v: unknown): string {
  if (v === null || v === undefined) return ''
  return String(v)
}

/**
 * 推断每一列的类型。要求该列**全部**非空值都满足该类型才算——
 * 混合列（如 "1,234" 与 "暂无"）宁可降级成分类列，也不要画出错误的图。
 */
function inferKinds(cols: string[], rows: unknown[][]): ColKind[] {
  return cols.map((name, j) => {
    const vals = rows.map((r) => r[j])
    const present = vals.filter((v) => v !== null && v !== undefined && v !== '')
    if (present.length === 0) return 'category'

    const nameHit = DATE_NAME_RE.test(name)
    const allDateShaped = present.every((v) => DATE_VALUE_RE.test(toText(v).trim()))
    // 列名像日期 或 值全是 ISO 日期，两者任一成立即认定为时间轴。
    if (allDateShaped || (nameHit && present.every((v) => typeof v === 'string' && v.trim() !== ''))) {
      return 'date'
    }

    const allNumeric = present.every((v) => toNumber(v) !== null)
    if (allNumeric) return 'number'

    return 'category'
  })
}

/** 日期列归一化：只做排序与打标，不改变原始值（标签仍显示后端给的值）。 */
function normalizeDate(v: unknown): number {
  const t = toText(v).trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return Date.parse(`${t}T00:00:00Z`)
  if (/^\d{4}-\d{2}$/.test(t)) return Date.parse(`${t}-01T00:00:00Z`)
  return Number.NaN
}

/**
 * 自动选型。这是整个组件唯一的「决策中枢」，规则按优先级从上到下命中即返回。
 *
 * | 数据特征 | 图型 | 依据 |
 * |---|---|---|
 * | 有日期列 + ≥1 数值列，且去重后时间点 ≥3 | 折线图 | 趋势是时间序列的第一语义 |
 * | 日期列 + 多数值列 | 多系列折线 | 同上，按指标拆线 |
 * | 日期列 + 1 数值列 + 1 文本列（文本可分组） | 按文本分组的折线 | 例：各门店月度复购数 |
 * | 无日期，1 文本列 + 1 数值列，分块数 2~8 | 环形图 | 占比关系最直观 |
 * | 无日期，1 文本列 + 1 数值列，行数 ≤30 | 横向条形 | 竖排类目标签会互相压字|
 * | 无日期，≥1 文本列 + 多数值列，分类数 ≤15 | 分组柱状 | 多指标横向对比 |
 * | 无日期，1 文本列 + 1 数值列，分类数 ≤15 | 柱状 | 通用对比 |
 * | 兜底 | 柱状 + 轻提示 | 形态不明时至少给出量级对比 |
 */
export function pickChart(cols: string[], rows: unknown[][]): ChartSpec {
  const n = rows.length
  // ---- 降级 1：样本不足以绘图 ----
  if (!cols.length || n === 0) {
    return { kind: 'bar', reason: '无数据', degraded: '执行没有返回样本行，无内容可绘制。' }
  }
  if (n === 1) {
    return { kind: 'bar', reason: '样本不足', degraded: '数据量不足以绘图（1 行），请查看下方表格。' }
  }

  const kinds = inferKinds(cols, rows)
  const dateCols = cols.filter((_, j) => kinds[j] === 'date')
  const numCols = cols.filter((_, j) => kinds[j] === 'number')
  const catCols = cols.filter((_, j) => kinds[j] === 'category')

  // ---- 降级 2：没有任何可绘制的数值列 ----
  if (numCols.length === 0) {
    return {
      kind: 'bar',
      reason: '无数值列',
      degraded: `这批样本没有可用于绘图的数值列（共 ${cols.length} 列：${cols.join('、')}），已按文本表格展示。`,
    }
  }

  // ---- 规则 1：时间序列 → 折线 ----
  if (dateCols.length > 0) {
    const dateCol = dateCols[0]
    const di = cols.indexOf(dateCol)
    // 用唯一时间点数量判断「是否构成序列」，而不是行数：
    // 同一时间点有多行（如 3 门店同月）时，唯一时间点才是真正的趋势长度。
    const uniqTimes = new Set(rows.map((r) => toText(r[di]).trim())).size
    if (uniqTimes >= MIN_TREND_POINTS) {
      const parts: string[] = [`识别到日期列「${dateCol}」`, `共 ${uniqTimes} 个时间点`]
      if (numCols.length > 1) {
        parts.push(`${numCols.length} 个指标数值列（${numCols.join('、')}），按指标拆成多条线`)
      } else if (catCols.length > 0) {
        parts.push(`按文本列「${catCols[0]}」分组分线`)
      } else {
        parts.push('单指标趋势')
      }
      return { kind: 'line', reason: `自动识别为时间序列 → 折线图：${parts.join('，')}。` }
    }
    // 时间点不足 3 个：趋势不成立，继续往下走对比类规则（不return，交给下面）
  }

  const numIdx = numCols.map((c) => cols.indexOf(c))

  // ---- 规则 2：单文本 + 单数值 → 环形 / 横向条形 ----
  if (catCols.length === 1 && numCols.length === 1) {
    const ci = cols.indexOf(catCols[0])
    const ni = numIdx[0]
    const slices = new Set(rows.map((r) => toText(r[ci]).trim())).size
    if (slices >= 2 && slices <= MAX_PIE_SLICES) {
      const total = rows.reduce((acc, r) => acc + Math.abs(toNumber(r[ni]) ?? 0), 0)
      // 占比成立的前提：各分块都是非负量。含负值（如利润、差额）时占比无意义。
      const allNonNeg = rows.every((r) => (toNumber(r[ni]) ?? 0) >= 0)
      if (allNonNeg && total > 0) {
        return {
          kind: 'pie',
          reason: `自动识别为单维构成 → 环形图：1 个分类列「${catCols[0]}」（${slices} 个分块，均为非负量、合计 ${total}），1 个数值列「${numCols[0]}」，适合看占比。`,
        }
      }
    }
    if (n <= 30) {
      return {
        kind: 'hbar',
        reason: `自动识别为单指标排序对比 → 横向条形图：分类列「${catCols[0]}」共 ${n} 行，横向排布可保证类目标签不被截断。`,
      }
    }
  }

  // ---- 规则 3：多数值列 → 分组柱状（分类数 ≤15 才放得下） ----
  if (numCols.length >= 2 && catCols.length >= 1) {
    const ci = cols.indexOf(catCols[0])
    const cats = new Set(rows.map((r) => toText(r[ci]).trim())).size
    if (cats <= MAX_CATEGORIES) {
      return {
        kind: 'groupedBar',
        reason: `自动识别为多指标对比 → 分组柱状图：分类列「${catCols[0]}」（${cats} 类）+ ${numCols.length} 个数值列（${numCols.join('、')}）。`,
      }
    }
    return {
      kind: 'fallbackBar',
      reason: `自动识别为多指标明细 → 柱状图：分类数 ${cats} 超过 ${MAX_CATEGORIES}，未做聚合以免失真，仅按原样展示首个指标。`,
      degraded: `数据形态不匹配趋势/占比分析，已按对比展示：分类数 ${cats} 超过 ${MAX_CATEGORIES}，图中只画了首个指标「${numCols[0]}」，完整数据请看表格。`,
    }
  }

  // ---- 规则 4：单文本 + 单数值但分类数 >15 ----
  if (numCols.length === 1 && catCols.length >= 1) {
    const ci = cols.indexOf(catCols[0])
    const cats = new Set(rows.map((r) => toText(r[ci]).trim())).size
    if (cats <= MAX_CATEGORIES) {
      return {
        kind: 'bar',
        reason: `自动识别为分类对比 → 柱状图：分类列「${catCols[0]}」（${cats} 类）+ 数值列「${numCols[0]}」。`,
      }
    }
    return {
      kind: 'fallbackBar',
      reason: `自动识别为高基数明细 → 柱状图：分类数 ${cats} 超过 ${MAX_CATEGORIES}，未做聚合，按原样展示首个指标。`,
      degraded: `数据形态不匹配趋势/占比分析，已按对比展示：分类数 ${cats} 超过 ${MAX_CATEGORIES}，图中只画了首个指标「${numCols[0]}」，完整数据请看表格。`,
    }
  }

  // ---- 规则 5：多个文本列、无时间轴 → 用第一列分类做柱状 ----
  if (catCols.length >= 1) {
    const ci = cols.indexOf(catCols[0])
    const cats = new Set(rows.map((r) => toText(r[ci]).trim())).size
    return {
      kind: 'bar',
      reason: `自动识别为维度对比 → 柱状图：无时间轴，按首个文本列「${catCols[0]}」（${cats} 类）分组对比指标「${numCols[0]}」。`,
    }
  }

  // ---- 兜底：只有数值列（如纯度量的多行样本） ----
  return {
    kind: 'bar',
    reason: `自动识别为纯数值样本 → 柱状图：无分类列也无时间轴，按行序展示指标「${numCols[0]}」。`,
    degraded: '数据形态不匹配趋势/占比分析，已按对比展示：没有分类列与时间轴，仅按行序展示数值。',
  }
}

/** 单行数据。值允许为 null：表示该系列在此时间点无数据（图中断开，不补零）。 */
type Row = Record<string, string | number | null>

const short = (s: string, n = 12) => (s.length > n ? `${s.slice(0, n)}…` : s)

/**
 * 结果图表：按数据特征自动选图型并渲染。
 * - 数据不足（0/1 行）时只出提示文字，绝不渲染空坐标系。
 * - 所有序列都取自 sample_rows 原值，不做任何补齐或平滑。
 */
export default function ResultChart({
  columns,
  rows,
  height = 320,
}: {
  columns: string[]
  rows: unknown[][]
  height?: number
}) {
  const spec = pickChart(columns, rows)

  // 降级：把「为什么没图 / 为什么这么画」讲清楚，不抛错、不空图
  if (spec.degraded) {
    return (
      <div className="chart-degraded">
        <p className="muted">{spec.degraded}</p>
        <p className="muted">{spec.reason}</p>
      </div>
    )
  }

  const kinds = inferKinds(columns, rows)
  const numCols = columns.filter((_, j) => kinds[j] === 'number')
  const catCols = columns.filter((_, j) => kinds[j] === 'category')
  const dateCols = columns.filter((_, j) => kinds[j] === 'date')

  // ---- 折线：X 轴=日期去重后的时间点，按「指标」或「文本列」拆系列 ----
  if (spec.kind === 'line' && dateCols.length > 0) {
    const di = columns.indexOf(dateCols[0])
    const axisKey = '__x'
    const dateOf = (r: unknown[]) => toText(r[di]).trim()

    // 先按时间升序排（趋势图必须有序）；时间不可解析（如「2026年Q3」）时保持原始行序。
    const ordered = [...rows]
      .map((r, i) => ({ r, i, t: normalizeDate(r[di]) }))
      .sort((a, b) => (Number.isNaN(a.t) || Number.isNaN(b.t) ? a.i - b.i : a.t - b.t))

    // 关键：**X 轴必须是去重后的时间点，而不是数据行**。
    // 同一月份可能有多行（如 3 个门店同月各一行）；若按行建X 轴，
    // 同一个 2026-08 会被拆到三个不同横坐标上，画出来的「趋势」是假的。
    const ticks: string[] = []
    for (const { r } of ordered) {
      const k = dateOf(r)
      if (!ticks.includes(k)) ticks.push(k)
    }

    let seriesKeys: string[]
    // 返回 null 表示「该系列在这个时间点没有数据」，Recharts 会断开而不是补零。
    let valueOf: (r: unknown[], key: string) => number | null
    let splitBy: string | null = null

    if (numCols.length >= 2) {
      seriesKeys = numCols
      splitBy = null
      valueOf = (r, key) => toNumber(r[columns.indexOf(key)])
    } else if (catCols.length > 0 && catCols[0] !== dateCols[0]) {
      const gi = columns.indexOf(catCols[0])
      splitBy = catCols[0]
      const vi = columns.indexOf(numCols[0])
      const seen = new Set<string>()
      for (const r of rows) {
        const k = toText(r[gi]).trim() || '(空)'
        if (!seen.has(k)) seen.add(k)
      }
      seriesKeys = [...seen]
      valueOf = (r, key) => (toText(r[gi]).trim() === key ? toNumber(r[vi]) : null)
    } else {
      seriesKeys = numCols
      valueOf = (r, key) => toNumber(r[columns.indexOf(key)])
    }

    // 每个时间点一行；同系列同时间点出现多行时取该时间点内的**合计**是不行的
    // （会凭空造出没发生过的量），因此这里只取首个匹配值，其余以 null 断开，
    // 并在note 里说明「同一时间点存在多行，未做聚合」。
    const dupKeys = new Set<string>()
    const data: Row[] = ticks.map((tick) => {
      const obj: Row = { [axisKey]: tick }
      for (const k of seriesKeys) {
        const hit = ordered.find(({ r }) => dateOf(r) === tick && valueOf(r, k) !== null)
        if (hit) {
          const sameCount = ordered.filter(({ r }) => dateOf(r) === tick && valueOf(r, k) !== null).length
          if (sameCount > 1) dupKeys.add(`${tick}/${k}`)
          obj[k] = valueOf(hit.r, k)
        } else {
          obj[k] = null
        }
      }
      return obj
    })

    return (
      <Frame
        reason={spec.reason}
        note={
          (splitBy
            ? `每条线是一个「${splitBy}」；横轴为「${dateCols[0]}」的 ${ticks.length} 个去重时间点，已按时间升序排列。某门店在该月无记录时线段断开，未补零、未跨档连线。`
            : `横轴为「${dateCols[0]}」的 ${ticks.length} 个去重时间点，已按时间升序排列；无数据的时间点断开显示，未补零。`) +
          (dupKeys.size > 0
            ? ` 注意：有 ${dupKeys.size} 处「同一时间点同一对象」出现多行，图中只取该时间点首个值（未做求和，以免造出数据中不存在的量），完整明细请看表格。`
            : '')
        }
        height={height}
      >
        <ResponsiveContainer width="100%" height={height}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e3e6ea" />
            <XAxis dataKey={axisKey} tick={{ fontSize: 11 }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 11 }} width={56} />
            <Tooltip contentStyle={{ fontSize: 12 }} />
            {seriesKeys.length > 1 ? <Legend wrapperStyle={{ fontSize: 11 }} /> : null}
            {seriesKeys.map((k, i) => (
              <Line
                key={k}
                type="linear"
                dataKey={k}
                stroke={PALETTE[i % PALETTE.length]}
                strokeWidth={2}
                dot={{ r: 3 }}
                /* 不开connectNulls：某门店只在个别月份有值，跨空档连线会画出
                   数据里根本不存在的趋势（实测Q2AL 被连成 1→0→1的假走势）。
                   宁可让该系列断开，只在真实取到值的月份落点连线。 */
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </Frame>
    )
  }

  // ---- 环形图 ----
  if (spec.kind === 'pie') {
    const ci = columns.indexOf(catCols[0])
    const ni = columns.indexOf(numCols[0])
    const agg = new Map<string, number>()
    for (const r of rows) {
      const k = toText(r[ci]).trim() || '(空)'
      agg.set(k, (agg.get(k) ?? 0) + Math.abs(toNumber(r[ni]) ?? 0))
    }
    const data: Row[] = [...agg.entries()].map(([name, value]) => ({ name: short(name, 16), value }))
    return (
      <Frame
        reason={spec.reason}
        note={`扇区面积即「${numCols[0]}」在合计中的占比；数值取绝对值以保证扇区可比。`}
        height={height}
      >
        <ResponsiveContainer width="100%" height={height}>
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius="45%"
              outerRadius="72%"
              paddingAngle={1}
              label={(e: any) => `${e.name} ${((e.percent ?? 0) * 100).toFixed(1)}%`}
              labelLine={false}
            >
              {data.map((d, i) => (
                <Cell key={d.name} fill={PALETTE[i % PALETTE.length]} stroke="#fff" strokeWidth={1} />
              ))}
            </Pie>
            <Tooltip contentStyle={{ fontSize: 12 }} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
          </PieChart>
        </ResponsiveContainer>
      </Frame>
    )
  }

  // ---- 分组柱状 / 柱状 / 兜底柱状：共用一套渲染 ----
  if (spec.kind === 'bar' || spec.kind === 'groupedBar' || spec.kind === 'fallbackBar') {
    const useGroup = spec.kind === 'groupedBar'
    const catCol = catCols[0] ?? columns[0]
    const ci = columns.indexOf(catCol)
    const seriesCols = useGroup ? numCols.slice(0, 4) : numCols.slice(0, 1)
    const data: Row[] = rows.map((r) => {
      const obj: Row = { name: short(toText(r[ci]).trim() || '(空)', 14) }
      for (const c of seriesCols) obj[c] = toNumber(r[columns.indexOf(c)]) ?? 0
      return obj
    })
    return (
      <Frame
        reason={spec.reason}
        note={
          useGroup
            ? `横轴为「${catCol}」，每组柱子是一个指标；${seriesCols.length < numCols.length ? `指标过多，仅展示前 ${seriesCols.length} 个（完整数据见表格）` : '全部指标均已展示'}。`
            : `横轴为「${catCol}」，柱高为「${seriesCols[0]}」。`
        }
        height={height}
      >
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e3e6ea" vertical={false} />
            <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={data.length > 8 ? -20 : 0} textAnchor={data.length > 8 ? 'end' : 'middle'} height={data.length > 8 ? 52 : 30} />
            <YAxis tick={{ fontSize: 11 }} width={56} />
            <Tooltip contentStyle={{ fontSize: 12 }} cursor={{ fill: 'rgba(31,95,208,.06)' }} />
            {seriesCols.length > 1 ? <Legend wrapperStyle={{ fontSize: 11 }} /> : null}
            {seriesCols.map((c, i) => (
              <Bar key={c} dataKey={c} fill={PALETTE[i % PALETTE.length]} radius={[3, 3, 0, 0]} maxBarSize={48} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </Frame>
    )
  }

  // ---- 横向条形 ----
  const catCol0 = catCols[0] ?? columns[0]
  const ci0 = columns.indexOf(catCol0)
  const valCol = numCols[0]
  const hbarData: Row[] = rows.map((r) => ({
    name: short(toText(r[ci0]).trim() || '(空)', 18),
    value: toNumber(r[columns.indexOf(valCol)]) ?? 0,
  }))
  return (
    <Frame
      reason={spec.reason}
      note={`纵轴为「${catCol0}」，条长为「${valCol}」；长类目标签横向排布以保证可读。`}
      height={Math.max(height, hbarData.length * 26 + 40)}
    >
      <ResponsiveContainer width="100%" height={Math.max(height, hbarData.length * 26 + 40)}>
        <BarChart data={hbarData} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e3e6ea" horizontal={false} />
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={130} interval={0} />
          <Tooltip contentStyle={{ fontSize: 12 }} cursor={{ fill: 'rgba(31,95,208,.06)' }} />
          <Bar dataKey="value" fill="#1f5fd0" radius={[0, 3, 3, 0]} maxBarSize={20} />
        </BarChart>
      </ResponsiveContainer>
    </Frame>
  )
}

/** 图表外框：图型说明（可解释性）+ 图 + 读图提示，风格沿用既有 card-body。 */
function Frame({
  reason,
  note,
  height,
  children,
}: {
  reason: string
  note?: string
  height: number
  children: React.ReactNode
}) {
  return (
    <div className="chart-frame">
      <div className="chart-why">
        <span className="chart-why-tag">自动选型</span>
        <span>{reason}</span>
      </div>
      <div style={{ height, width: '100%', minWidth: 0 }}>{children}</div>
      {note ? <p className="muted chart-note">{note}</p> : null}
    </div>
  )
}
