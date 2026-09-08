// 本文件负责 AppShell 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import { Bot, BookOpen, Cable, ChartNoAxesCombined, Database, History, LayoutDashboard, Menu, MessageSquare, Moon, PanelLeftClose, PanelLeftOpen, Settings2, Sparkles, Sun, Type } from 'lucide-react'
import { useEffect, useLayoutEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from '../api'
import { PenguinMark } from '../components/penguin'
import { AgentsPage } from '../features/agents/AgentsPage'
import { DashboardPage } from '../features/dashboard/DashboardPage'
import { MemoriesPage } from '../features/memories/MemoriesPage'
import { McpPage } from '../features/mcp/McpPage'
import { ModelsPage } from '../features/models/ModelsPage'
import { RunsPage } from '../features/runs/RunsPage'
import { AppearanceSettingsPage } from '../features/settings/AppearanceSettingsPage'
import { PersonalizationPage } from '../features/settings/PersonalizationPage'
import { SessionsPage } from '../features/sessions/SessionsPage'
import { SkillsPage } from '../features/skills/SkillsPage'
import { UsagePage } from '../features/usage/UsagePage'
import { useApiData } from '../shared/hooks/useApiData'
import type { Health } from '../types'

// navigation 是侧栏和路由展示共用的导航元数据；前七项属于工作台，其余属于系统设置。
const navigation = [
  { path: '/dashboard', label: '总览', icon: LayoutDashboard },
  { path: '/agents', label: 'Agent 小队', icon: Bot },
  { path: '/skills', label: '技能库', icon: BookOpen },
  { path: '/mcp', label: 'MCP', icon: Cable },
  { path: '/sessions', label: '会话', icon: MessageSquare },
  { path: '/runs', label: '运行记录', icon: History },
  { path: '/usage', label: '用量统计', icon: ChartNoAxesCombined },
  { path: '/settings/models', label: '模型设置', icon: Settings2 },
  { path: '/settings/personalization', label: '个性化', icon: Sparkles },
  { path: '/settings/memories', label: '持久记忆', icon: Database },
  { path: '/settings/appearance', label: '界面设置', icon: Type },
]

// 字号缩放上下限保护页面布局，fontScale 的值会持久化到 localStorage。
const fontScaleMin = 0.85
const fontScaleMax = 1.25

// 读取并夹紧已保存的字号倍率；浏览器禁用本地存储时回到默认倍率。
function loadFontScale() {
  try {
    const stored = window.localStorage.getItem('pgagent-font-scale')
    if (stored === null) return 1
    const saved = Number(stored)
    return Number.isFinite(saved) ? Math.min(fontScaleMax, Math.max(fontScaleMin, saved)) : 1
  } catch {
    return 1
  }
}

// AppShell 是前端顶层布局，负责导航、主题、字号、健康状态和所有页面路由的装配。
function AppShell() {
  // mobileOpen/collapsed 控制两种侧栏形态；theme/fontScale 是跨页面的外观状态。
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      return window.localStorage.getItem('pgagent-theme') === 'dark' ? 'dark' : 'light'
    } catch {
      return 'light'
    }
  })
  const [fontScale, setFontScale] = useState(loadFontScale)
  // location 用于路由变化后关闭移动导航；health 驱动侧栏底部的后端在线状态。
  const location = useLocation()
  const health = useApiData<Health | null>(null, () => api.get<Health>('/api/health'), [])

  useEffect(() => setMobileOpen(false), [location.pathname])
  useEffect(() => {
    document.documentElement.style.colorScheme = theme
    document.documentElement.dataset.theme = theme
    try { window.localStorage.setItem('pgagent-theme', theme) } catch { /* 本地存储不可用时只保持当前页面状态。 */ }
  }, [theme])
  useLayoutEffect(() => {
    document.documentElement.style.fontSize = `${16 * fontScale}px`
    try { window.localStorage.setItem('pgagent-font-scale', String(fontScale)) } catch { /* 本地存储不可用时只保持当前页面状态。 */ }
  }, [fontScale])

  return (
    <div className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''} theme-${theme}`}>
      <button className="mobile-menu" aria-label="打开导航" onClick={() => setMobileOpen(true)}><Menu /></button>
      {mobileOpen && <button className="mobile-backdrop" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}>
          <div className="brand">
            <div className="brand-mark"><PenguinMark size={27} /></div>
            <div className="brand-copy"><strong>PGAgent</strong><span>企鹅工作台</span></div>
            <button
              className="theme-toggle icon-button"
              type="button"
              aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
              title={theme === 'dark' ? '浅色主题' : '深色主题'}
              onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}
            >
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button className="collapse-button" type="button" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? '展开导航' : '收起导航'} title={collapsed ? '展开工作台' : '收起工作台'}>
              {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
            </button>
          </div>
        <nav aria-label="主导航">
          <p className="nav-label">企鹅工作台</p>
          {navigation.slice(0, 7).map(({ path, label, icon: Icon }) => (
            <NavLink key={path} to={path} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`} title={collapsed ? label : undefined}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-label nav-label-spaced">系统舱</p>
          {navigation.slice(7).map(({ path, label, icon: Icon }) => (
            <NavLink key={path} to={path} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`} title={collapsed ? label : undefined}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className={`service-pill ${health.error ? 'offline' : ''}`} title={health.error || '本地服务已连接'}>
            <i />
            <div><strong>{health.error ? '服务未连接' : health.loading ? '正在检测' : '本地服务在线'}</strong><span>{health.data?.version ? `v${health.data.version}` : '127.0.0.1'}</span></div>
          </div>
        </div>
      </aside>
      <main className="main-content">
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/workspaces" element={<Navigate to="/sessions" replace />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/mcp" element={<McpPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/settings/models" element={<ModelsPage />} />
          <Route path="/settings/personalization" element={<PersonalizationPage />} />
          <Route path="/settings/memories" element={<MemoriesPage />} />
          <Route path="/settings/appearance" element={<AppearanceSettingsPage fontScale={fontScale} onFontScaleChange={setFontScale} />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}


export { AppShell }
