import { describe, expect, it } from 'vitest'
import { buildContextUsageView } from './contextUsage'

describe('上下文圆环显示', () => {
  it('按已用和总量计算百分比并格式化辅助说明', () => {
    const view = buildContextUsageView({
      used_tokens: 89000,
      limit_tokens: 100000,
      compact_threshold_tokens: 90000,
      percent: 0,
    })
    expect(view.percent).toBe(89)
    expect(view.roundedPercent).toBe(89)
    expect(view.detail).toBe('已用 89,000 Token（标记），共 100,000')
    expect(view.compressionHint).toBe('达到 90% 时自动压缩上下文')
    expect(view.tone).toBe('near-limit')
  })

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
