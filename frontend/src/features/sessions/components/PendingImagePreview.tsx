import { ChevronLeft, ChevronRight, Maximize, Minus, Plus, X } from 'lucide-react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { PendingAttachment } from '../../../attachments'

const MIN_ZOOM = 0.25
const MAX_ZOOM = 5

// 切图时通过 key 重建视口，使缩放和拖动位置只属于当前图片。
function ImageViewport({ image }: { image: PendingAttachment }) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null)
  const previousSizeRef = useRef({ width: 0, height: 0 })
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 })
  const [imageSize, setImageSize] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState(1)
  const fit = imageSize.width && imageSize.height
    ? Math.min(viewportSize.width / imageSize.width, viewportSize.height / imageSize.height)
    : 0
  const width = imageSize.width * fit * zoom
  const height = imageSize.height * fit * zoom
  const stageWidth = Math.max(viewportSize.width, width)
  const stageHeight = Math.max(viewportSize.height, height)

  useEffect(() => {
    const viewport = viewportRef.current!
    const observer = new ResizeObserver(([entry]) => setViewportSize({ width: entry.contentRect.width, height: entry.contentRect.height }))
    observer.observe(viewport)
    // React 的滚轮监听默认是 passive；这里阻止页面滚动，只缩放图片。
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      setZoom((current) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, current * Math.exp(-event.deltaY * 0.002))))
    }
    viewport.addEventListener('wheel', onWheel, { passive: false })
    return () => { observer.disconnect(); viewport.removeEventListener('wheel', onWheel) }
  }, [])

  // 保持视口中心对应的图片位置；真实尺寸的滚动区域保证四边都可到达。
  useLayoutEffect(() => {
    const viewport = viewportRef.current!
    const previous = previousSizeRef.current
    viewport.scrollLeft = previous.width ? (viewport.scrollLeft + viewport.clientWidth / 2) * stageWidth / previous.width - viewport.clientWidth / 2 : 0
    viewport.scrollTop = previous.height ? (viewport.scrollTop + viewport.clientHeight / 2) * stageHeight / previous.height - viewport.clientHeight / 2 : 0
    previousSizeRef.current = { width: stageWidth, height: stageHeight }
  }, [stageWidth, stageHeight])

  return <>
    <div className="pending-image-zoom" role="group" aria-label="图片缩放">
      <button type="button" className="pending-image-control" aria-label="缩小图片" title="缩小" disabled={zoom <= MIN_ZOOM} onClick={() => { const next = Math.max(MIN_ZOOM, zoom - 0.25); if (next === MIN_ZOOM) viewportRef.current!.focus(); setZoom(next) }}><Minus size={18} aria-hidden="true" /></button>
      <output aria-label="缩放比例">{Math.round(zoom * 100)}%</output>
      <button type="button" className="pending-image-control" aria-label="放大图片" title="放大" disabled={zoom >= MAX_ZOOM} onClick={() => { const next = Math.min(MAX_ZOOM, zoom + 0.25); if (next === MAX_ZOOM) viewportRef.current!.focus(); setZoom(next) }}><Plus size={18} aria-hidden="true" /></button>
      <button type="button" className="pending-image-control pending-image-fit" onClick={() => setZoom(1)} title="恢复适配窗口"><Maximize size={16} aria-hidden="true" />适配窗口</button>
    </div>
    <div ref={viewportRef} className={`pending-image-viewport${zoom > 1 ? ' is-zoomed' : ''}`} tabIndex={0} role="region" aria-label="图片预览，可滚轮缩放，放大后拖动或滚动查看"
      onPointerDown={(event) => {
        if (event.button !== 0 || zoom <= 1) return
        const viewport = event.currentTarget
        dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop }
        viewport.setPointerCapture(event.pointerId)
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current
        if (!drag || drag.id !== event.pointerId) return
        event.currentTarget.scrollLeft = drag.left + drag.x - event.clientX
        event.currentTarget.scrollTop = drag.top + drag.y - event.clientY
      }}
      onPointerUp={(event) => { if (dragRef.current?.id === event.pointerId) { dragRef.current = null; event.currentTarget.releasePointerCapture(event.pointerId) } }}
      onLostPointerCapture={() => { dragRef.current = null }}
    >
      <div className="pending-image-stage" style={{ width: stageWidth, height: stageHeight }}>
        <img className="pending-image-full" src={image.previewUrl} alt={image.file.name} draggable={false} style={{ width, height }} onLoad={(event) => setImageSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} />
      </div>
    </div>
  </>
}

// 图片列表直接来自待发送附件，移除/发送/切换会话后不保留已失效的预览 URL。
export function PendingImagePreview({ images, selectedId, onSelect, onClose }: { images: PendingAttachment[]; selectedId: string; onSelect: (id: string) => void; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const index = images.findIndex((image) => image.id === selectedId)
  const image = images[index]

  useLayoutEffect(() => { dialogRef.current!.showModal() }, [])
  // 首尾按钮会变为禁用，切图后把焦点留在弹层，方向键才能继续工作。
  useLayoutEffect(() => { dialogRef.current!.focus() }, [selectedId])

  function moveImage(direction: number) {
    const next = images[index + direction]
    if (next) onSelect(next.id)
  }

  return <dialog ref={dialogRef} className="pending-image-dialog" tabIndex={-1} aria-labelledby={titleId} onClose={onClose}
    onClick={(event) => { if (event.target === event.currentTarget) event.currentTarget.close() }}
    onKeyDown={(event) => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        moveImage(event.key === 'ArrowLeft' ? -1 : 1)
      }
    }}
  >
    <header>
      <strong id={titleId} title={image.file.name}>{image.file.name}</strong>
      <button type="button" className="pending-image-control" aria-label="关闭图片预览" onClick={() => dialogRef.current?.close()}><X size={20} aria-hidden="true" /></button>
    </header>
    <ImageViewport key={image.id} image={image} />
    {images.length > 1 && <nav className="pending-image-navigation" aria-label="图片切换">
      <button type="button" className="pending-image-control" aria-label="上一张图片" disabled={index === 0} onClick={() => moveImage(-1)}><ChevronLeft size={20} aria-hidden="true" /></button>
      <span aria-live="polite">{index + 1} / {images.length}</span>
      <button type="button" className="pending-image-control" aria-label="下一张图片" disabled={index === images.length - 1} onClick={() => moveImage(1)}><ChevronRight size={20} aria-hidden="true" /></button>
    </nav>}
  </dialog>
}
