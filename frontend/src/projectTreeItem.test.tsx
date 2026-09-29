// 本测试文件验证 projectTreeItem 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ProjectTreeItem } from './features/sessions/components/ProjectTreeItem'
import { projectDeleteConfirmation } from './features/sessions/projectDeletion'

// 测试分组：project deletion。
describe('project deletion', () => {
  // 测试场景：explains that conversations are deleted but local files stay。
  it('explains that conversations are deleted but local files stay', () => {
    expect(projectDeleteConfirmation('Alpha', 2)).toBe(
      '确定从 PGAgent 删除项目“Alpha”吗？该项目下的 2 个历史对话及其运行记录也会永久删除。本地项目文件不会被删除，此操作无法撤销。',
    )
  })

  // 测试场景：为每个项目渲染可访问的新建对话和删除操作。
  it('renders accessible project actions for every project', () => {
    const markup = renderToStaticMarkup(
      <ProjectTreeItem
        workspace={{ id: 'workspace-a', name: 'Alpha' }}
        expanded={false}
        deleting={false}
        deleteDisabled={false}
        newConversationDisabled={false}
        hoverCardVisible={false}
        onToggle={() => undefined}
        onNewConversation={() => undefined}
        onDelete={() => undefined}
        onManageGit={() => undefined}
        onShowHoverCard={() => undefined}
        onHideHoverCard={() => undefined}
      ><div>children</div></ProjectTreeItem>,
    )
    expect(markup).toContain('aria-label="在项目 Alpha 下新建对话"')
    expect(markup).toContain('title="在此项目下新建对话"')
    expect(markup).toContain('aria-label="删除项目 Alpha"')
    expect(markup).toContain('title="从 PGAgent 删除项目"')
    expect(markup).not.toContain('children')
  })
})


describe('项目标题', () => {
  const actions = { onToggle() {}, onNewConversation() {}, onDelete() {}, onManageGit() {}, onShowHoverCard() {}, onHideHoverCard() {} }
  it('只显示项目名，不在历史会话栏重复展示 Git 状态和展开箭头', () => {
    const html = renderToStaticMarkup(<ProjectTreeItem workspace={{ id: 'w', name: 'Alpha', git_info: { branch: 'master' } }} expanded={false} deleting={false} deleteDisabled={false} newConversationDisabled={false} hoverCardVisible={false} {...actions}>{null}</ProjectTreeItem>)
    expect(html).toContain('<span class="project-name">Alpha</span>')
    expect(html).toContain('aria-expanded="false"')
    expect(html).not.toContain('master')
    expect(html).not.toContain('project-chevron')
    expect(html).toContain('管理项目 Alpha 的 Git 与 worktree')
  })
})
