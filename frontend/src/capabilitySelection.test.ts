// 本测试文件验证 capabilitySelection 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { normalizedIds, permissionLabel, toggleSelectedId } from './capabilitySelection'

// 测试分组：会话能力选择。
describe('会话能力选择', () => {
  // 测试场景：会去重、清理空技能并稳定切换选中状态。
  it('会去重、清理空技能并稳定切换选中状态', () => {
    expect(normalizedIds([' skill-a ', '', 'skill-a', 'skill-b'])).toEqual(['skill-a', 'skill-b'])
    expect(toggleSelectedId(['skill-a'], 'skill-b')).toEqual(['skill-a', 'skill-b'])
    expect(toggleSelectedId(['skill-a', 'skill-b'], 'skill-a')).toEqual(['skill-b'])
  })

  // 测试场景：显示后端权限值对应的中文名称。
  it('显示后端权限值对应的中文名称', () => {
    expect(permissionLabel('ask')).toBe('请求批准')
    expect(permissionLabel('smart')).toBe('智能审批')
    expect(permissionLabel('full')).toBe('完全访问')
  })
})
