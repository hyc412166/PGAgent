// 本测试文件验证 memorySettingsPlacement 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MemoriesPage } from './features/memories/MemoriesPage'
import { PersonalizationPage } from './features/settings/PersonalizationPage'

// 测试分组：持久记忆设置入口。
describe('持久记忆设置入口', () => {
  // 测试场景：只在持久记忆页面提供总开关，不提供人工写入表单。
  it('只在持久记忆页面提供总开关，不提供人工写入表单', () => {
    const personalization = renderToStaticMarkup(<PersonalizationPage />)
    const memories = renderToStaticMarkup(<MemoriesPage />)

    expect(personalization).not.toContain('全局记忆')
    expect(memories).toContain('持久记忆开关')
    expect(memories).not.toContain('新增或更新记忆')
    expect(memories).not.toContain('NEW MEMORY')
    expect(memories).not.toContain('memory-form')
  })
})
