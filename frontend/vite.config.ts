import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端构建与开发服务配置：启用 React，并把本地 /api 请求代理到 FastAPI 后端。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        // 默认端口与后端启动脚本一致；VITE_API_PROXY_TARGET 可在不改源码时切换本地实例。
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
