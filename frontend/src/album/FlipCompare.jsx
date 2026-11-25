import { useEffect, useRef, useState } from 'react'
import { imgSrc } from '../api'

/**
 * 卡内翻页对比 —— 在缩略图上左右翻动，看「实拍 → 成品」
 *
 * ★ 用户要求：「每页是融合的，跟原图对比的那种一样」
 *   两张图叠在同一个框里、跟手移动，松手滑到位——
 *   不是点一下瞬间切换。
 *
 * ★ 实测踩到的两个坑（都在这文件里）：
 *   ① **pointerup 收不到**：原先把 onPointerMove/onPointerUp 挂在元素上，
 *      但 pointerup 只在**指针所在元素**上触发，拖到元素外松手就丢
 *      ⇒ 表现为「拖动跟手了，但松手不吸附，停在半路」。
 *      实测抓到的事件序列确实没有 pointerup。
 *      ⇒ 改为在 window 上监听 move/up/cancel。
 *   ② **拖动追不上手**：吸附动画(transition)会跟手动画打架
 *      ⇒ 拖动中给容器加 .is-dragging 关掉过渡，松手再恢复。
 */
export default function FlipCompare({
  srcLeft, srcRight, pct = 1, onPct,
  altLeft = '实拍摄影', altRight = '系统成品',
  leftBroken, rightBroken, onLeftError, onRightError,
}) {
  const boxRef = useRef(null)
  const drag = useRef(null)
  const live = useRef(pct)          // ★ 拖动中的实时值（受控 prop 会有滞后）
  const [shown, setShown] = useState(pct)

  useEffect(() => { setShown(pct); live.current = pct }, [pct])

  useEffect(() => {
    const end = () => {
      const d = drag.current
      drag.current = null
      document.querySelectorAll('.flipcmp.is-dragging')
        .forEach((el) => el.classList.remove('is-dragging'))
      if (!d) return
      // ★ 松手吸附到最近的一侧 —— 让它"翻到位"而不是停在半路
      const target = live.current >= 0.5 ? 1 : 0
      live.current = target
      setShown(target)
      onPct(target)
    }
    const move = (e) => {
      const d = drag.current
      if (!d) return
      const x = e.clientX ?? e.touches?.[0]?.clientX ?? 0
      const clamped = Math.max(0, Math.min(1, d.startPct + (x - d.x) / d.w))
      live.current = clamped
      setShown(clamped)             // 本地立刻更新 ⇒ 跟手
      onPct(clamped)
    }
    // ★ 挂在 window 上 —— 元素外松手也能收到（坑①）
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', end)
    }
  }, [onPct])

  const onDown = (e) => {
    const x = e.clientX ?? e.touches?.[0]?.clientX ?? 0
    const w = boxRef.current?.offsetWidth || 1
    live.current = pct              // 抓住时先同步，避免从旧值起跳
    drag.current = { x, w, startPct: pct }
    boxRef.current?.classList.add('is-dragging')   // 关过渡（坑②）
  }

  const onKeyDown = (e) => {
    if (e.key === 'ArrowLeft') { live.current = 0; setShown(0); onPct(0) }
    else if (e.key === 'ArrowRight') { live.current = 1; setShown(1); onPct(1) }
  }

  return (
    <div
      ref={boxRef}
      className="flipcmp"
      onPointerDown={onDown}
      role="slider"
      aria-label="左右滑动对比实拍原图与系统成品"
      aria-valuenow={Math.round(shown * 100)}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      {/* 底层：实拍摄影 */}
      {leftBroken ? (
        <div className="flipcmp__ph">实拍原图暂不可显示</div>
      ) : (
        <img className="flipcmp__img" src={imgSrc(srcLeft)} alt={altLeft}
             draggable={false} loading="lazy" onError={onLeftError} />
      )}

      {/* 上层：成品，按 shown 从左往右露出 —— 两层融合同框 */}
      <div className="flipcmp__layer" style={{ width: `${shown * 100}%` }}>
        {rightBroken ? (
          <div className="flipcmp__ph">成品暂不可显示</div>
        ) : (
          <img className="flipcmp__img" src={imgSrc(srcRight)} alt={altRight}
               draggable={false} loading="lazy" onError={onRightError} />
        )}
      </div>

      <div className="flipcmp__handle" style={{ left: `${shown * 100}%` }}>
        <span className="flipcmp__grip">⇄</span>
      </div>

      {/* 两侧标签：一眼知道现在看到的是哪张 */}
      <div className={`flipcmp__tag flipcmp__tag--l${shown > 0.5 ? ' flipcmp__tag--off' : ''}`}>
        实拍
      </div>
      <div className={`flipcmp__tag flipcmp__tag--r${shown <= 0.5 ? ' flipcmp__tag--off' : ''}`}>
        成品
      </div>
    </div>
  )
}