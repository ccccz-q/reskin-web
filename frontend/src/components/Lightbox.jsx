import { imgSrc } from '../api'

/**
 * 大图灯箱 —— 示例图 / 画布成品 / 仓库缩略图共用一套预览。
 *
 * ★ 为什么必须放在 toast-wrap 之外：toast-wrap 有 pointer-events:none
 *   （让 toast 不挡点击），而pointer-events 会被子元素继承 ——
 *   除非每层显式重置。灯箱需要完整鼠标交互，放进去会导致整层不可点（终审 P0-2）。
 */
export default function Lightbox({ item, onClose }) {
  if (!item) return null
  return (
    <div className="lightbox" onClick={onClose} role="dialog" aria-label="大图预览">
      <button type="button" className="lightbox-close" aria-label="关闭大图"
              title="关闭"
              onClick={(e) => { e.stopPropagation(); onClose() }}>✕</button>
      <img src={imgSrc(item.url)} alt="大图预览" />
      <div className="lightbox-bar">
        <span className="lightbox-dim">{item.dim || ''}</span>
        <span className="muted">点击图片外任意处或 ✕ 关闭</span>
      </div>
    </div>
  )
}