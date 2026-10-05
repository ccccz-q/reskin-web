import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Swiper, SwiperSlide } from 'swiper/react'
import { Autoplay, EffectCoverflow, Keyboard } from 'swiper/modules'
import 'swiper/css'
import 'swiper/css/effect-coverflow'
import { api, imgSrc, thumbSrc } from './api'

/**
 * 作品环绕展示（Swiper coverflow）—— 主页面区域，可收缩
 *
 * 两种状态（弹簧动画切换，状态记 localStorage）：
 *   展开 ≈44vh   Swiper 3D 环绕，鼠标左右控制方向与转速
 *   收起 112px   作品流胶带（CSS 单排无限滚动），仍在流动但不占空间
 *
 * ★ 收起态不渲染 Swiper，改用 CSS 胶带：
 *   slide 宽度由 CSS 决定，压到 112px 再改宽度会触发 Swiper 重排重建，
 *   装饰态没必要冒这个险。
 *
 * 鼠标连续转向：
 *   停在左半边 → 内容持续左移；右半边 → 持续右移；居中 4% 死区 → 停住；
 *   越靠边越快。方向一变就立刻走一步（否则要等一个 delay，手感发木）。
 *   只改 swiper.params（delay / speed / reverseDirection）并复用 autoplay，
 *   不自己改 translate —— 自己改会和 coverflow 的 transform 打架。
 */
const LS_KEY = 'ring-collapsed'

const DEAD = 0.04        // 中间死区（占环宽比例）
const DUR_DELAY = { slow: 1900, fast: 90 }     // 两页之间的间隔：慢 ↔ 快
const DUR_SPEED = { slow: 950, fast: 260 }     // 单页切换时长：慢 ↔ 快

export default function HeroRing({ latest, source = 'mix' }) {
  const [items, setItems] = useState([])
  const [entered, setEntered] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(LS_KEY) === '1')
  const [viewer, setViewer] = useState(null)
  const [closing, setClosing] = useState(false)
  const [dir, setDir] = useState(0)      // -1 左移 / 0 停 / +1 右移

  const swiperRef = useRef(null)
  const wrapRef = useRef(null)
  const rectRef = useRef(null)
  const rafRef = useRef(0)
  const pendingRef = useRef(null)
  const closeTimer = useRef(null)

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const r = await api.gallery(120, source === 'upload' ? 'upload' : 'seed,generated')
        if (!alive) return
        setItems(r.items || [])
      } catch { /* 加载失败不影响工作台 */ }
      requestAnimationFrame(() => alive && setEntered(true))
    })()
    return () => {
      alive = false
      clearTimeout(closeTimer.current)
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [source])

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      localStorage.setItem(LS_KEY, c ? '0' : '1')
      return !c
    })
  }, [])

  /** 出图成功 → 新图插到最前并定位 */
  useEffect(() => {
    if (!latest?.url) return
    setItems((prev) => {
      const rest = prev.filter((x) => x.url !== latest.url)
      return [{ url: latest.url, filename: '刚生成', kind: 'generated',
                modified: new Date().toISOString() }, ...rest]
    })
  }, [latest?.url])   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!latest?.url || items.length === 0 || !swiperRef.current) return
    const idx = items.findIndex((x) => x.url === latest.url)
    if (idx >= 0) swiperRef.current.slideToLoop(idx, 900)
  }, [items, latest?.url])

  /** 鼠标位置 → 方向与转速（rAF 合并，一帧只算一次） */
  const applySpin = useCallback(() => {
    rafRef.current = 0
    const sw = swiperRef.current
    const off = pendingRef.current
    if (!sw || off === null || items.length < 2 || !sw.params?.autoplay) return

    const abs = Math.abs(off)
    if (abs < DEAD) {
      sw.params.autoplay.delay = DUR_DELAY.slow
      sw.params.speed = DUR_SPEED.slow
      try { sw.autoplay.stop() } catch { /* 版本差异兜底 */ }
      setDir(0)
      return
    }

    const dist = Math.min(1, (abs - DEAD) / (0.5 - DEAD))
    const rev = off > 0                       // 鼠标在右 → 内容右移
    const next = rev ? 1 : -1

    sw.params.autoplay.reverseDirection = rev
    sw.params.autoplay.delay = Math.round(
      DUR_DELAY.slow - (DUR_DELAY.slow - DUR_DELAY.fast) * Math.pow(dist, 0.7))
    sw.params.speed = Math.round(
      DUR_SPEED.slow - (DUR_SPEED.slow - DUR_SPEED.fast) * dist)

    // ★ 方向刚变就立刻走一步，不用等第一个 delay
    if (dir !== next) {
      try { rev ? sw.slidePrev() : sw.slideNext() } catch { /* 兜底 */ }
    }
    try { if (!sw.autoplay.running) sw.autoplay.start() } catch { /* 兜底 */ }
    setDir(next)
  }, [items.length, dir])

  const onMouseMove = useCallback((e) => {
    if (collapsed) return
    const el = wrapRef.current
    if (!el) return
    const r = rectRef.current || (rectRef.current = el.getBoundingClientRect())
    if (!r.width) return
    const rel = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
    pendingRef.current = rel - 0.5
    if (!rafRef.current) rafRef.current = requestAnimationFrame(applySpin)
  }, [applySpin, collapsed])

  const onMouseEnter = useCallback(() => {
    const el = wrapRef.current
    rectRef.current = el ? el.getBoundingClientRect() : null
  }, [])

  const onMouseLeave = useCallback(() => {
    rectRef.current = null
    pendingRef.current = null
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
    const sw = swiperRef.current
    if (sw?.params?.autoplay) {
      sw.params.autoplay.reverseDirection = false
      sw.params.autoplay.delay = DUR_DELAY.slow
      sw.params.speed = DUR_SPEED.slow
      try { if (!sw.autoplay.running) sw.autoplay.start() } catch { /* 兜底 */ }
    }
    setDir(-1)
  }, [])

  const open = useCallback((url, filename) => { setViewer({ url, filename }); setClosing(false) }, [])
  const close = useCallback(() => {
    setClosing(true)
    clearTimeout(closeTimer.current)
    closeTimer.current = setTimeout(() => { setViewer(null); setClosing(false) }, 230)
  }, [])

  useEffect(() => {
    const onKey = (e) => {
      if (!viewer) return
      if (e.key === 'Escape') close()
      if (e.key === 'ArrowLeft') swiperRef.current?.slideNext()
      if (e.key === 'ArrowRight') swiperRef.current?.slidePrev()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [viewer, close])

  const params = useMemo(() => ({
    modules: [Autoplay, EffectCoverflow, Keyboard],
    effect: 'coverflow',
    grabCursor: true,
    centeredSlides: true,
    slidesPerView: 'auto',
    loop: items.length > 4,
    speed: DUR_SPEED.slow,
    // ★ 关掉 pauseOnMouseEnter：方向由鼠标左右位置接管，
    //   再让它自动暂停的话鼠标一进来就停转，功能直接失效
    autoplay: { delay: DUR_DELAY.slow, disableOnInteraction: false, pauseOnMouseEnter: false },
    keyboard: { enabled: true },
    coverflowEffect: {
      rotate: 34,          // 两侧向内旋转的角度 → 环绕感的主力
      stretch: 0,
      depth: 190,          // 两侧向后退的深度
      modifier: 1.35,
      slideShadows: true,  // 印刷品投在页面上的影子
    },
  }), [items.length])

  const strip = useMemo(
    () => (items.length ? Array.from({ length: 4 }, () => items).flat() : []),
    [items])

  return (
    <section ref={wrapRef}
             className={`ring ${collapsed ? 'folded' : ''} ${entered ? 'on' : ''}`}
             onMouseMove={onMouseMove} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}
             aria-label="作品环绕展示">
      <button type="button" className="ring-toggle" onClick={toggle}
              title={collapsed ? '展开环绕展示' : '收起为作品流'}>
        {collapsed ? '▾ 展开环绕' : '▴ 收起环绕'}
      </button>

      {!collapsed && (
        <div className="ring-deco" aria-hidden="true">
          <h2 className="ring-title">换颜</h2>
          <p className="ring-tag">同一张照片，换一张面孔</p>
          <span className="ring-dot d1" /><span className="ring-dot d2" />
          <span className="ring-dot d3" />
        </div>
      )}

      {collapsed ? (
        /* ── 收起态：112px 作品流胶带（横版格子 + 图铺满）── */
        <div className="ring-strip">
          <span className="strip-label">作品流</span>
          <div className="ring-strip-tape" aria-hidden="true">
            {strip.map((it, i) => (
              <span key={it.url + i} className="strip-cell" style={{ '--i': i }}>
                <img src={thumbSrc(it.url, 420)} alt="" decoding="async" />
              </span>
            ))}
          </div>
        </div>
      ) : items.length > 0 ? (
        /* ── 展开态：Swiper 3D 环绕 ── */
        <>
          <Swiper {...params} onSwiper={(sw) => (swiperRef.current = sw)}
                  className="ring-swiper">
            {items.map((it, i) => (
              <SwiperSlide key={it.url} className="ring-slide">
                <button type="button"
                        className={`ring-cell ${latest?.url === it.url ? 'lit' : ''}`}
                        onClick={() => open(it.url, it.filename)}
                        title="点击放大">
                  <img src={thumbSrc(it.url, 480)} alt={it.filename} decoding="async" />
                  <span className="ring-cap">{(it.modified || '').slice(0, 10)}</span>
                </button>
              </SwiperSlide>
            ))}
          </Swiper>
          <p className={`ring-hint ${dir !== 0 ? 'lit' : ''}`}>
            {dir === -1 ? '←｜持续左移（越靠左越快）'
              : dir === 1 ? '持续右移（越靠右越快）｜→'
              : '鼠标移到左侧 = 持续左移 · 右侧 = 持续右移 · 居中停住'}
          </p>
        </>
      ) : (
        <p className="ring-empty">作品正在装裱…</p>
      )}

      {viewer && (
        <div className={`zoom-mask ${closing ? 'out' : ''}`} onClick={close} role="dialog">
          <div className="zoom-card" onClick={(e) => e.stopPropagation()}>
            <img src={imgSrc(viewer.url)} alt={viewer.filename} />
            <div className="zoom-meta">
              <span className="zoom-name">{viewer.filename}</span>
              <span className="tb-spacer" />
              <button className="btn ghost tb-btn"
                      onClick={async () => {
                        const resp = await fetch(imgSrc(viewer.url))
                        const blob = await resp.blob()
                        const a = document.createElement('a')
                        a.href = URL.createObjectURL(blob)
                        a.download = viewer.filename || '作品.jpg'
                        a.click()
                        URL.revokeObjectURL(a.href)
                      }}>
                ⬇ 下载
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
