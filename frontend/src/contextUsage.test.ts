// 本测试文件验证 contextUsage 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { buildContextUsageView } from './contextUsage'

// 测试分组：上下文圆环显示。
describe('上下文圆环显示', () => {
  // 测试场景：按已用和总量计算百分比并格式化辅助说明。
  it('按已用和总量计算百分比并格式化辅助说明', () => {
    const view = buildContextUsageView({
      used_tokens: 89000,
      limit_tokens: 100000,
      compact_threshold_tokens: 90000,
      percent: 0,
    })
    expect(view.percent).toBe(89)
    expect(view.roundedPercent).toBe(89)
    expect(view.detail).toBe('已用 89,000 Token，共 100,000；剩余 11,000 Token')
    expect(view.compressionHint).toBe('达到 90% 时自动压缩上下文')
    expect(view.tone).toBe('near-limit')
  })

  // 测试场景：达到阈值后切换为自动压缩状态并钳制异常百分比。
  it('达到阈值后切换为自动压缩状态并钳制异常百分比', () => {
    const view = buildContextUsageView({
      used_tokens: 120000,
      limit_tokens: 100000,
      compact_threshold_tokens: 90000,
      percent: 120,
    })
    expect(view.percent).toBe(100)
    expect(view.tone).toBe('compacting')
    expect(view.ariaLabel).toContain('正在使用自动压缩机制')
  })
})
