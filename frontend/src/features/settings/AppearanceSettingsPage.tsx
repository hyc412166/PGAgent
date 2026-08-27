import { Type } from 'lucide-react'
import { PageHeader } from '../../components/ui'

function AppearanceSettingsPage({ fontScale, onFontScaleChange }: { fontScale: number; onFontScaleChange: (value: number) => void }) {
  const percentage = Math.round(fontScale * 100)
  const presets = [90, 100, 110, 120]
  return (
    <div className="page appearance-settings-page">
      <PageHeader eyebrow="冰面观感" title="界面设置" description="调整整个工作台的文字大小，修改会立即应用并自动保存。" />
      <section className="appearance-settings-card card">
        <header><span className="appearance-settings-icon"><Type size={20} /></span><div><h2>全局字体大小</h2><p>对导航、对话、卡片和设置页面中的文字统一缩放。</p></div><output>{percentage}%</output></header>
        <div className="font-scale-preview" aria-hidden="true"><small>实时预览</small><strong>呆萌企鹅准备出发</strong><p>文字会变大或变小，控件和图标仍保持原来的紧凑尺寸。</p></div>
        <div className="font-scale-control">
          <div className="font-scale-labels"><span>较小</span><span>标准</span><span>较大</span></div>
          <input type="range" min={85} max={125} step={5} value={percentage} aria-label={`全局字体大小 ${percentage}%`} onChange={(event) => onFontScaleChange(Number(event.target.value) / 100)} />
          <div className="font-scale-presets" aria-label="字体大小快捷选项">{presets.map((value) => <button key={value} type="button" className={percentage === value ? 'active' : ''} onClick={() => onFontScaleChange(value / 100)}>{value}%</button>)}</div>
        </div>
      </section>
    </div>
  )
}

export { AppearanceSettingsPage }
