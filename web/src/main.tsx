import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import './styles.css'

/**
 * 全局错误边界挂在最外层，覆盖整个应用。
 *
 * 这是**兜底**：单个页面出错时不该整页白屏，最坏也只白那一块。
 * 注意 ErrorBoundary 只能捕获**渲染期**异常——事件处理器 / 定时器回调里的
 * 异常不归它管，那些由 `api/client.ts` 的统一错误处理兜。
 */
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)