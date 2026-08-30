import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MemoriesPage } from './features/memories/MemoriesPage'
import { PersonalizationPage } from './features/settings/PersonalizationPage'

describe('持久记忆设置入口', () => {
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
