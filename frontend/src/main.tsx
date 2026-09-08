// 本文件负责 main 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'

// 将应用挂载到 HTML 的 root 节点；StrictMode 在开发环境帮助发现副作用问题。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
