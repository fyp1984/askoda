import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 全局错误边界
 *
 * 为什么必须有：React 里渲染期抛出的未捕获异常会让**整页白屏**——
 * 用户看到的只有一个空白页面，既不知道出了什么事，也没法继续操作。
 * 演示场景下这比"报错"更伤：观众只看到白屏，不知道是数据问题还是系统崩了。
 *
 * 边界能做什么 / 不能做什么：
 *  · 能兜住：子组件 render 抛异常、生命周期钩子抛异常。
 *  · 兜不住：事件处理器里的异常、`setTimeout` 回调里的异常、
 *    以及**自己这个边界组件自身**抛的异常（所以下面的兜底分支必须零依赖）。
 *
 * 这里刻意**不用任何 UI 库、不引任何依赖**：错误边界是最后一道防线，
 * 越简单越可靠——它自己出问题的话就是白屏套白屏。
 */

interface Props {
  children: ReactNode
  /** 自定义降级 UI；不传则用内置的默认样式 */
  fallback?: (error: Error, reset: () => void) => ReactNode
  /** 出错时上报（可接埋点 / 日志上报） */
  onError?: (error: Error, info: ErrorInfo) => void
}

interface State {
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 上报不阻断：上报本身再抛异常也不能影响兜底渲染
    try {
      this.props.onError?.(error, info)
    } catch {
      /* 忽略上报异常 */
    }
    // 开发期留痕，方便定位；生产环境由 onError 决定是否上报
    if (typeof console !== 'undefined') {
      console.error('[ErrorBoundary] 渲染异常已被拦截：', error, info.componentStack)
    }
  }

  reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback) return this.props.fallback(error, this.reset)

    // 内置降级：纯内联样式，不依赖外部 CSS 是否已加载
    return (
      <div style={S.page}>
        <div style={S.card}>
          <h2 style={S.title}>页面出错了</h2>
          <p style={S.desc}>
            这一步没能正常渲染。可以点下面的按钮重试；
            如果反复出现，把下面的错误信息发给维护者。
          </p>
          <pre style={S.detail}>{String(error?.message || error)}</pre>
          <div style={S.actions}>
            <button style={S.primary} onClick={this.reset}>
              重试
            </button>
            <button style={S.ghost} onClick={() => window.location.reload()}>
              刷新页面
            </button>
          </div>
        </div>
      </div>
    )
  }
}

const S: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '60vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '24px',
    background: '#f7f8fa',
  },
  card: {
    maxWidth: '680px',
    background: '#fff',
    border: '1px solid #e3e6ea',
    borderRadius: '8px',
    padding: '24px 26px',
    boxShadow: '0 1px 3px rgba(16,24,40,.06)',
  },
  title: { margin: '0 0 10px', fontSize: '18px', color: '#1f2937' },
  desc: { margin: '0 0 14px', fontSize: '13.5px', lineHeight: '1.7', color: '#4b5563' },
  detail: {
    margin: '0 0 16px',
    padding: '10px 12px',
    background: '#f5f6f8',
    border: '1px solid #e3e6ea',
    borderRadius: '6px',
    fontSize: '12px',
    color: '#b42318',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all',
    maxHeight: '160px',
    overflow: 'auto',
  },
  actions: { display: 'flex', gap: '10px' },
  primary: {
    padding: '7px 18px',
    fontSize: '13px',
    color: '#fff',
    background: '#1a5fb4',
    border: 'none',
    borderRadius: '6px',
    cursor: 'pointer',
  },
  ghost: {
    padding: '7px 18px',
    fontSize: '13px',
    color: '#1a5fb4',
    background: '#fff',
    border: '1px solid #cfdef5',
    borderRadius: '6px',
    cursor: 'pointer',
  },
}