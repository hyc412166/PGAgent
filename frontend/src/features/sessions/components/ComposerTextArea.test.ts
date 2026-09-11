// 本测试验证编辑器在会话身份变化时丢弃旧草稿，避免文本串到另一个会话。
import { describe, expect, it } from 'vitest'

import { alignComposerValueState } from './composerTextAreaState'

describe('ComposerTextArea resetKey', () => {
  it('resetKey 变化后清空旧输入', () => {
    expect(alignComposerValueState({ resetKey: 'session-a', value: '旧会话草稿' }, 'session-b')).toEqual({
      resetKey: 'session-b',
      value: '',
    })
  })
})
