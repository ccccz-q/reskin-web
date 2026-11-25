import { useCallback, useEffect, useRef, useState } from 'react'
import { imgSrc } from '../api'

/**
 * 作品大图展播 —— 左右拖动切换上一张 / 下一张
 *
 * ★ 用户要求（2026-10-09实测反馈）：
 *   「在作品展示的时候，拖动图片可以进行上一张下一张」
 *   —— 即拖动是**翻页**，不是"在同一张图上滑动对比"。
 *   这个语义我一开始理解错了，做成了对比滑块。
 *
 * ★ 为什么拖动而不是点箭头：
 *   真实相册/作品集的交互就是**划屏翻页**，
 *   而点箭头是"控件思维"，移动端根本没有。
 *
 * ★ 三层交互不打架：
 *   · 左右拖 → 翻页（切作品）
 *   · 底部缩略图 → 直达某一张
 *   · 顶部按钮 → 编辑 / 打开画册 / 关闭
 *   ★ 拖动与点击要区分：位移小于阈值视为点击（不翻页）
 */
export default function StageView({
  items = [], index = 0, onIndex, onClose,
  onEdit, onOpenAlbum, onTryStyle, onCompare,
}) {
  const [drag, setDrag] = useState(null)   // {x0, dx}
  const [busy, setBusy] = useState(false)
  const startX = useRef(0)
  const moved = useRef(false)
  const touchX = useRef(0)
  // ★ 实时位移。用 ref 而不是 state：state 会因批处理而滞后，
  //   onUp 读到旧值 ⇒ 明明拖够了却不翻页（实测踩过）。
  const liveDx = useRef(0)
  const widthRef = useRef(0)   // ★ onDown 时记下宽度，供 onUp 判定阈值
  const bodyRef = useRef(null) // ★ .sv-body 本体，取宽度用

  const total = items.length
  const cur = items[index]

  const go = useCallback((delta) => {
    if (busy || !total) return
    let n = index + delta
    // 循环：最后一张往前回到第一张
    if (n < 0) n = total - 1
    if (n >= total) n = 0
    if (n === index) return
    setBusy(true)
    onIndex(n)
    setTimeout(() => setBusy(false), 320)   // 与滑动动画同步
  }, [index, total, busy, onIndex])

  // 键盘：← → 翻页，Esc 关闭
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'ArrowLeft') go(-1)
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go, onClose])

  /* 拖动翻页 —— 用 pointer 事件统一处理鼠标与触摸 */
  const onDown = (e) => {
    startX.current = e.clientX ?? e.touches?.[0]?.clientX ?? 0
    touchX.current = startX.current
    moved.current = false
    liveDx.current = 0
    // ★ 拖动开始时锁定容器宽度（e.currentTarget 在 onUp 里已为 null）
    widthRef.current = bodyRef.current?.offsetWidth || 1
    setDrag({ dx: 0 })
  }
  const onMove = (e) => {
    const x = e.clientX ?? e.touches?.[0]?.clientX ?? 0
    const dx = x - startX.current
    // ★ 超过 8px 才算"拖动" —— 否则普通点击会被误判成翻页
    if (Math.abs(dx) > 8) moved.current = true
    if (moved.current) {
      // 阻尼：拖到头时给点阻力，暗示"到头了"
      const edge = (index === 0 && dx > 0) || (index === total - 1 && dx < 0)
      liveDx.current = edge ? dx * 0.32 : dx
      setDrag({ dx: liveDx.current })
    }
  }
  const onUp = (e) => {
    const dx = liveDx.current          // ★ 用实时值，不是 state
    liveDx.current = 0
    setDrag(null)
    if (!moved.current) return          // 只是点击，不翻页
    // ★ 宽度不能从 e.currentTarget 取：React 池化了事件对象，
    //   异步处理器里它是 null ⇒ w 变成 1 ⇒ 阈值约 0px，判定失效。
    //   ⇒ 用 drag 启动时记下的宽度。
    const w = widthRef.current || 1
    // ★ 阈值 5%：大图容器宽约 1200+px，8% 要拖 100px 才翻，
    //   实测 110px 刚好卡在边界上会失败 —— 真实用户拖不到位。
    //   5% 约 60px，是「明显拖动」与「手抖」的舒适分界。
    const TH = w * 0.05
    if (dx < -TH) go(1)
    else if (dx > TH) go(-1)
  }

  if (!cur) return null
  const brokenHere = cur.broken
  const leftBroken = cur.leftBroken

  return (
    <div className="stageview" role="dialog" aria-label="作品展">
      {/* 顶部操作条 */}
      <div className="sv-top">
        <div className="sv-title">
          <b>{cur.title}</b>
          <span>{cur.place}</span>
        </div>
        <div className="sv-acts">
          {onEdit && (
            <button type="button" className="sv-btn" onClick={() => onEdit(cur)}
                    title="编辑这张成品：改文案、换风格重新生成">
              ✎ 编辑
            </button>
          )}
          {onCompare && (
            <button type="button" className="sv-btn" onClick={() => onCompare(index)}
                    title="与实拍摄影逐像素对比">
              ⇄ 原图对比
            </button>
          )}
          {onOpenAlbum && (
            <button type="button" className="sv-btn" onClick={() => onOpenAlbum(index)}
                    title="在电子画册中查看全部">
              📖 画册
            </button>
          )}
          {onTryStyle && (
            <button type="button" className="sv-btn" onClick={() => onTryStyle(cur)}
                    title="用这个风格再试一张">
              ✍ 试一张
            </button>
          )}
          <button type="button" className="sv-close" onClick={onClose}
                  aria-label="关闭作品展" title="关闭（Esc）">
                <span aria-hidden="true">✕</span>
                <span className="sv-close-text">关闭</span>
              </button>
        </div>
      </div>

      {/* 主体：可拖动翻页 */}
      <div
        ref={bodyRef}
        className={`sv-body${drag ? ' is-dragging' : ''}${busy ? ' is-turning' : ''}`}
        style={drag ? { transform: `translateX(${drag.dx}px)` } : undefined}
        onPointerDown={onDown} onPointerMove={onMove}
        onPointerUp={onUp} onPointerCancel={onUp}
      >
        {brokenHere ? (
          <div className="sv-ph">这张成品暂不可显示</div>
        ) : (
          /* ★ 这里只放成品，不再嵌对比滑块 ——
             用户反馈"展示页里拖动的是关闭，特别不好用"。
             对比改由顶部「原图对比」按钮单独打开（与卡片一致）。*/
          <div className="sv-frame">
            <img className="sv-img" src={imgSrc(cur.generated)}
                 alt={`${cur.title} 系统成品`} draggable={false} />
            {leftBroken ? <div className="sv-note">原图暂不可显示</div> : null}
          </div>
        )}
      </div>

      {/* 左右翻页按钮（鼠标可见；拖动也能翻） */}
      <button type="button" className="sv-nav sv-nav--l" onClick={() => go(-1)}
              aria-label="上一张" title="上一张（←）">‹</button>
      <button type="button" className="sv-nav sv-nav--r" onClick={() => go(1)}
              aria-label="下一张" title="下一张（→）">›</button>

      {/* 底部：缩略图直达 + 提示 */}
      <div className="sv-bottom">
        <div className="sv-strip">
          {items.map((it, i) => (
            <button key={i} type="button"
                    className={`sv-chip${i === index ? ' is-on' : ''}`}
                    onClick={() => onIndex(i)}
                    title={`${it.title}（${it.place}）`}>
              <img src={imgSrc(it.generated)} alt="" loading="lazy" />
              <span>{it.place?.split(' ').pop() || it.title}</span>
            </button>
          ))}
        </div>
        <div className="sv-hint">
          左右拖动翻页 · 键盘 ← → 也可 · 顶部可开原图对比
        </div>
      </div>
    </div>
  )
}