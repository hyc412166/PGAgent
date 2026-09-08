// 本文件负责 App 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import { AppShell } from './app/AppShell'

import './App.css'
import './styles/index.css'

// App 装配浏览器路由环境；具体布局与页面路由由 AppShell 负责。
export default function App() {
  return <AppShell />
}
