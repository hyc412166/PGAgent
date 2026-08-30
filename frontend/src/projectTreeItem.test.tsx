import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ProjectTreeItem } from './features/sessions/components/ProjectTreeItem'
import { projectDeleteConfirmation } from './features/sessions/projectDeletion'

describe('project deletion', () => {
  it('explains that conversations are deleted but local files stay', () => {
    expect(projectDeleteConfirmation('Alpha', 2)).toBe(
      '确定从 PGAgent 删除项目“Alpha”吗？该项目下的 2 个历史对话及其运行记录也会永久删除。本地项目文件不会被删除，此操作无法撤销。',
    )
  })

  it('renders an accessible destructive action for every project', () => {
    const markup = renderToStaticMarkup(
      <ProjectTreeItem
        workspace={{ id: 'workspace-a', name: 'Alpha' }}
        expanded={false}
        deleting={false}
        deleteDisabled={false}
        hoverCardVisible={false}
        onToggle={() => undefined}
        onDelete={() => undefined}
        onShowHoverCard={() => undefined}
        onHideHoverCard={() => undefined}
      ><div>children</div></ProjectTreeItem>,
    )
    expect(markup).toContain('aria-label="删除项目 Alpha"')
    expect(markup).toContain('title="从 PGAgent 删除项目"')
    expect(markup).not.toContain('children')
  })
})
