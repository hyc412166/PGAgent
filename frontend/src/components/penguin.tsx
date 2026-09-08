// 本文件提供可复用的 penguin 展示组件，供各功能页面组合使用并保持交互表现一致。
import type { SVGProps } from 'react'

// PenguinMarkProps 在标准 SVG 属性上增加便捷尺寸参数。
type PenguinMarkProps = SVGProps<SVGSVGElement> & {
  size?: number
}

/**
 * The small PGAgent mascot is deliberately inline SVG: it keeps the brand
 * available on first paint without adding a network request or a raster asset.
 */
// PenguinMark 渲染品牌企鹅矢量标记，供侧栏和状态区复用。
export function PenguinMark({ size = 24, ...props }: PenguinMarkProps) {
  return (
    <svg
      {...props}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <ellipse cx="32" cy="36" rx="20" ry="24" fill="currentColor" />
      <ellipse cx="32" cy="40" rx="12.5" ry="16.5" fill="#F7FCFF" />
      <path d="M17 37c-7 3-10 8-11 14 7-1 12-4 16-9" fill="currentColor" opacity=".9" />
      <path d="M47 37c7 3 10 8 11 14-7-1-12-4-16-9" fill="currentColor" opacity=".9" />
      <circle cx="25" cy="24" r="5.7" fill="#F7FCFF" />
      <circle cx="39" cy="24" r="5.7" fill="#F7FCFF" />
      <circle cx="26" cy="25" r="2" fill="#102B3D" />
      <circle cx="38" cy="25" r="2" fill="#102B3D" />
      <path d="m32 27 6 4-6 4-6-4 6-4Z" fill="#F6A646" />
      <path d="M15 34c5 4 11 6 17 6s12-2 17-6v8c-5 4-11 6-17 6s-12-2-17-6v-8Z" fill="#E64B53" />
      <path d="M42 42c3 2 5 5 5 9-4-1-7-3-9-6" fill="#C83B49" />
    </svg>
  )
}
