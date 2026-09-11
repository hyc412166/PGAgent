// 本测试验证个人指令草稿与后台刷新数据之间的优先级。
import { describe, expect, it } from 'vitest'

import { normalizePersonalizationDraft, personalizationDraftAfterSave, resolvePersonalizationDraft } from './personalizationDraft'

describe('个人指令草稿', () => {
  it('未编辑时跟随后台刷新，编辑后保留本地草稿', () => {
    expect(resolvePersonalizationDraft(null, '服务端新内容')).toBe('服务端新内容')
    expect(resolvePersonalizationDraft('尚未保存的本地内容', '服务端新内容')).toBe('尚未保存的本地内容')
  })

  it('草稿改回服务端值后重新跟随后续刷新', () => {
    const pristine = normalizePersonalizationDraft('服务端原值', '服务端原值')

    expect(pristine).toBeNull()
    expect(resolvePersonalizationDraft(pristine, '服务端后续刷新')).toBe('服务端后续刷新')
  })

  it('保存请求期间继续输入时保留更新后的本地草稿', () => {
    expect(personalizationDraftAfterSave('请求期间新增的内容', '提交时的内容')).toBe('请求期间新增的内容')
    expect(personalizationDraftAfterSave('提交时的内容', '提交时的内容')).toBeNull()
  })
})
