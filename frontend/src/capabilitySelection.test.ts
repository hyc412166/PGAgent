import { describe, expect, it } from 'vitest'
import { normalizedIds, permissionLabel, toggleSelectedId } from './capabilitySelection'

describe('会话能力选择', () => {
  it('会去重、清理空技能并稳定切换选中状态', () => {
    expect(normalizedIds([' skill-a ', '', 'skill-a', 'skill-b'])).toEqual(['skill-a', 'skill-b'])
    expect(toggleSelectedId(['skill-a'], 'skill-b')).toEqual(['skill-a', 'skill-b'])
    expect(toggleSelectedId(['skill-a', 'skill-b'], 'skill-a')).toEqual(['skill-b'])
  })

  it('显示后端权限值对应的中文名称', () => {
    expect(permissionLabel('ask')).toBe('请求批准')
    expect(permissionLabel('smart')).toBe('智能审批')
    expect(permissionLabel('full')).toBe('完全访问权限')
  })
})
