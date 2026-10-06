import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端只认识自己的相对路径 /api。
// 开发态通过 vite 代理把 /api 转发到自研 BFF；
// 生产态由 BFF 同源静态托管（见 bff/app.py），同样无需任何绝对地址。
// 红线 R2：这里不得出现后端开源组件的地址或端口。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 18000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18081',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: '../bff/static',
    emptyOutDir: true,
    sourcemap: false,
  },
})