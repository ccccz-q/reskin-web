import { useEffect, useRef, useState } from 'react'
import { imgSrc } from '../api'
// ★ PageFlip 库（MIT，Copyright 2020 Nodlik）—— **本地内置，不联网**
import './vendor/page-flip.browser.js'
import './album.css'
import { prefersReducedMotion } from './geometry'

/**
 * 电子画册 —— 双页对开 · 真实翻书
 *
 * ★★★ 为什么改用 PageFlip 库（2026-10-09 决定）：
 *   我先前**自研**了一套 CSS 3D 翻页，结果用户实测反馈「不流畅、翻转不对」，
 *   之后又反复出问题（右页消失、拖动不跟手、pointerup 收不到…）。
 *   ⇒ 结论：**翻页这种"形变 + 惯性 + 阴影"的手感，不该自研**。
 *   成熟库在这个场景已被验证过，**修bug 的成本远低于再写一遍**。
 *
 * ★ 为什么引入库不违背「离线可用」：
 *   库文件**随代码一起入库**（src/album/vendor/，43KB），
 *   答辩现场**不联网也能跑** —— 这与我当初不引库的理由不同：
 *   当初我是因为"库文件缺失就白屏"而不引；
 *   而现在库是**本仓库的一部分**，不存在缺失。
 *
 * ★ 参考实现来源：
 *   HaichaoLihc/create-photo-flipbook-ui（486 star）的vendor 目录，
 *   本项目按自己的视觉语言（拾景纸刊）重写了页面样式。
 */

const PAGE = { w: 460, h: 700 }

export default function AlbumViewer({ pages = [], onClose }) {
  const [ready, setReady] = useState(false)
  const [tick, setTick] = useState(0)   // 布局未就绪时用来触发重试
  const pagesRef = useRef(pages)
  // ★ aliveRef：区分「effect 重跑」与「组件真正卸载」。
  //   只有真正卸载才 destroy，否则重跑会把库建的 DOM 全删掉。
  const aliveRef = useRef(false)
  pagesRef.current = pages
  const [cursor, setCursor] = useState(0)
  const [turning, setTurning] = useState(false)
  const bookRef = useRef(null)
  const apiRef = useRef(null)
  // ★ 由JS 创建、React 只 append 一次的容器节点。
  //   这样 React 的重渲染不会删它 —— PageFlip 需要这个节点稳定存在。
  const [rigNode] = useState(() => {
    const d = document.createElement('div')
    d.className = 'album-rig'
    d.id = 'album-rig'
    return d
  })

  const total = pages.length
  const [pfCount, setPfCount] = useState(0)
  const canPrev = cursor > 0
  const canNext = cursor < total - 1

  useEffect(() => {
    // ★ 这个库是 UMD，挂在 window.St 上（不是 window.PageFlip）。
    //   在这里现取而不是模块顶层 —— 顶层 const 会在 import 阶段求值，
    //   时机上可能早于库挂载，导致 PageFlip 为 undefined（实测踩过）。
    const PageFlip = window.St?.PageFlip
    const el = bookRef.current || rigNode
    if (!total || !el || !PageFlip) return
    // ★ 库按元素的**实际尺寸**算页面几何。若此刻 offsetWidth 为 0
    //   （首帧、容器还没布局），它会把页子清空却不渲染
    //   ⇒ 表现为「状态栏 / -1、页数 0」。等一帧让布局完成再初始化。
    if (!el.offsetWidth) {
      const raf = requestAnimationFrame(() => setTick((n) => n + 1))
      return () => cancelAnimationFrame(raf)
    }
    // ★ 手动建页子并塞进去（不用 React 渲染，见上面的说明）
    if (!el.children.length) {
      pagesRef.current.forEach((pg, i) => el.appendChild(buildLeaf(pg, i)))
    }
    const leaves = el.querySelectorAll('.album-leaf')
    if (!leaves.length) return
    // ★ PageFlip 需要「宽度/高度」与「usePortrait」
    //   Portrait 模式下才是**单页对开**（一页一页翻）；
    //   Landscape 才是真正的双页并排。
    const api = new PageFlip(el, {
      width: PAGE.w,
      height: PAGE.h,
      size: 'stretch',
      minWidth: 240,
      maxWidth: 520,
      minHeight: 380,
      maxHeight: 760,
      drawShadow: true,
      // ★ 翻页时长跟着「降低动态」偏好走
      flippingTime: prefersReducedMotion() ? 1 : 820,
      usePortrait: true,              // 单页对开：更符合"一页一页翻"
      startZIndex: 10,
      autoSize: false,     // ★ 由我们自己定容器尺寸，避免库取最小值
      maxShadowOpacity: 0.42,
      showCover: true,
      mobileScrollSupport: false,
      clickEventForward: true,       // 点右半页 = 往后翻
      useMouseEvents: true,
      swipeDistance: 24,              // ★ 拖动跟手的阈值
      showPageCorners: true,          // 右下角卷角提示
      disableFlipByClick: false,
    })
    apiRef.current = api
    // 从 HTML 里的 .book-page 元素载入内容
    api.loadFromHTML(leaves)
    setPfCount(api.getPageCount?.() || leaves.length)
    api.on('flip', (e) => {
      setCursor(Number(e.data))
    })
    api.on('changeState', (e) => {
      // ★ 'read' 表示不在翻页中；其余是翻页动画阶段
      setTurning(e.data !== 'read')
    })
    setReady(true)
    apiRef.current = api
    aliveRef.current = true
    return () => { aliveRef.current = false }
    // ★ 只依赖 total：pages 是每次渲染新建的数组，
    //   放进依赖会让 effect 反复重跑 → destroy 把DOM 全删（实测踩过）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [total, tick, rigNode])

  // ★ 真正卸载时才销毁（空依赖，只跑一次）。
  //   上一版把 destroy 放在初始化 effect 的 cleanup 里，
  //   而该effect 会因依赖变化重跑 ⇒ 把库建的 DOM 全删 ⇒ 页面空白。
  useEffect(() => () => {
    try { apiRef.current?.destroy() } catch { /* 已销毁 */ }
  }, [])

  // 外部改变 cursor（点击页码）→ 调库翻过去
  useEffect(() => {
    const api = apiRef.current
    if (!api || !ready) return
    if (api.getCurrentPageIndex() !== cursor) api.turnToPage(cursor)
  }, [cursor, ready])

  const go = (delta) => {
    const api = apiRef.current
    if (!api || turning) return
    const n = cursor + delta
    if (n < 0 || n >= total) return
    if (delta > 0) api.flipNext('top')
    else api.flipPrev('top')
  }

  // 键盘：← → 翻页，Esc 关闭
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'ArrowLeft') go(-1)
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div className="album" role="dialog" aria-label="旅行随记电子画册">
      <style>{`
        .album{position:fixed;inset:0;z-index:70;display:grid;place-items:center;
              background:radial-gradient(120% 90% at 50% 20%, #35312c 0%, #1e1c19 70%);
              backdrop-filter:blur(3px)}
        .album-book{position:relative;display:grid;place-items:center;
              width:${PAGE.w}px;height:${PAGE.h}px}
        /* PageFlip 需要容器有明确的尺寸上下文 */
        .album-rig{position:relative;width:${PAGE.w}px;height:${PAGE.h}px}
        .album-leaf{background:var(--paper);overflow:hidden}
        .album-back{background:var(--paper-dim)}
        .album-paper{position:absolute;inset:0;background:var(--paper);
              box-shadow:inset 0 0 40px rgb(120 100 70 / .06)}
        .album-leaf.is-odd{box-shadow:inset 14px 0 22px -14px rgb(60 45 25 / .28)}
        .album-leaf.is-even{box-shadow:inset -14px 0 22px -14px rgb(60 45 25 / .28)}
        .album-grain{position:absolute;inset:0;opacity:.5;
              background-image:repeating-linear-gradient(0deg,
                rgb(120 100 70 / .035) 0 1px, transparent 1px 3px)}
        .album-photo{position:absolute;inset:0;width:100%;height:100%;
              object-fit:contain;background:var(--paper)}
        .album-caption{position:absolute;left:0;right:0;bottom:0;z-index:5;
              padding:78px 34px 34px;
              background:linear-gradient(180deg,transparent,rgb(38 30 20 /.88));
              color:#f6f0e3}
        .album-place{font-size:12px;letter-spacing:.26em;opacity:.92;margin-bottom:2px}
        .album-title{margin:0 0 9px;font-size:25px;font-weight:600;line-height:1.28;
              text-shadow:0 1px 0 rgb(0 0 0 / .34)}
        .album-story{font-size:13px;line-height:1.72;opacity:.92;
              display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
        .album-folio{position:absolute;bottom:16px;z-index:8;font-size:12px;
              letter-spacing:.1em;color:rgb(70 60 46 / .7)}
        .album-leaf.is-odd .album-folio{right:22px}
        .album-leaf.is-even .album-folio{left:22px}

        .album-cover{position:absolute;inset:0;display:grid;place-items:center;text-align:center;
              padding:40px;background:radial-gradient(90% 70% at 50% 26%, #fbf7ee, #f0e8d8)}
        .album-cover-mark{font-size:46px;line-height:1;margin-bottom:16px;opacity:.78}
        .album-cover-title{font-size:34px;font-weight:700;letter-spacing:.18em;color:#4a4036;
              text-shadow:0 1px 0 rgb(255 255 255 / .8)}
        .album-cover-sub{margin-top:13px;font-size:12px;letter-spacing:.34em;color:#8b7f6f}
        .album-cover-rule{width:46px;height:1px;margin:22px auto;
              background:linear-gradient(90deg,transparent,#a99a83,transparent)}
        .album-cover-foot{position:absolute;bottom:30px;left:0;right:0;text-align:center;
              font-size:11px;letter-spacing:.2em;color:#9c9080}
        .album-endpaper{position:absolute;inset:0;background:#e9e3d4;
              display:grid;place-items:center}
        .album-endpaper-inner{text-align:center;color:#7d7160;font-size:12.5px;
              line-height:2;letter-spacing:.14em}

        /* 控制条 */
        .album-bar{position:absolute;bottom:26px;left:50%;transform:translateX(-50%);
              display:flex;align-items:center;gap:14px;padding:9px 15px;border-radius:999px;
              background:rgb(22 19 16 / .66);backdrop-filter:blur(8px);color:#ece5d7;
              box-shadow:0 8px 28px rgb(0 0 0 / .4)}
        .album-btn{width:34px;height:34px;padding:0;border:1px solid rgb(255 255 255 / .16);
              border-radius:50%;background:transparent;color:inherit;font-size:17px;
              line-height:1;cursor:pointer;transition:background .16s,opacity .16s}
        .album-btn:hover:not(:disabled){background:rgb(255 255 255 / .14)}
        .album-btn:disabled{opacity:.26;cursor:default}
        .album-status{min-width:92px;text-align:center;font-size:12px;letter-spacing:.1em}
        .album-dots{display:flex;gap:6px}
        .album-dot{width:6px;height:6px;border-radius:50%;background:rgb(255 255 255 / .3);
              cursor:pointer;transition:transform .18s,background .18s}
        .album-dot.is-on{background:#dcc9a6;transform:scale(1.3)}
        .album-close{position:absolute;top:22px;right:26px;width:34px;height:34px;
              border:1px solid rgb(255 255 255 / .18);border-radius:50%;
              background:rgb(22 19 16 / .62);color:#ece5d7;font-size:14px;cursor:pointer;
              backdrop-filter:blur(6px)}
        @media (max-width:760px){
          .album-rig{width:min(88vw,${PAGE.w}px);height:calc(min(88vw,${PAGE.w}px) * ${(PAGE.h / PAGE.w).toFixed(4)})}
        }
      `}</style>

      <button type="button" className="album-close" aria-label="关闭画册"
              onClick={onClose} title="关闭（Esc）">✕</button>

      {!total ? (
        <div className="album-endpaper" style={{ width: PAGE.w, height: PAGE.h }}>
          还没有可展示的内容
        </div>
      ) : (
        <div className="album-book" ref={(n) => { if (n && rigNode.parentNode !== n) n.appendChild(rigNode); bookRef.current = rigNode }}>
          {/* ★★★ 容器用 ref 交给 PageFlip 接管，**绝不让 React 管它的内容**。
               为什么：库会改写容器内的 DOM（生成页容器、加阴影层），
               而任何 React 重渲染（setCursor / setReady / setTick）
               都会按VNode 把这些节点删回去 ⇒ 页面空白。
               实测踩过三轮：容器 0×0、kids=0、连 .album-rig 都查不到。
               与参考项目一致：页子写在 HTML 里，JS 只 loadFromHTML。*/}
        </div>
      )}

      <div className="album-bar">
        <button type="button" className="album-btn" onClick={() => go(-1)}
                disabled={!canPrev} aria-label="上一页" title="上一页（←）">‹</button>
        <span className="album-status">
          {pages[cursor]?.kind === 'cover' ? '封面'
            : pages[cursor]?.kind === 'back' ? '封底'
            : String(cursor).padStart(2, '0')} / {String(total - 1).padStart(2, '0')}
        </span>
        <div className="album-dots">
          {pages.map((p, i) => (
            <span key={i} className={`album-dot${i === cursor ? ' is-on' : ''}`}
                  onClick={() => setCursor(i)} role="button" tabIndex={-1}
                  aria-label={`第 ${i} 页`} />
          ))}
        </div>
        <button type="button" className="album-btn" onClick={() => go(1)}
                disabled={!canNext} aria-label="下一页" title="下一页（→）">›</button>
      </div>
    </div>
  )
}

/** 把一页构造成 DOM 元素（供 PageFlip 读取）。不经过 React。 */
function buildLeaf(page, idx) {
  const div = document.createElement('div')
  div.className = `album-leaf ${idx % 2 ? 'is-odd' : 'is-even'}`
  div.dataset.density = (page.kind === 'cover' || page.kind === 'back') ? 'hard' : 'soft'
  div.appendChild(buildLeafFace(page))
  if (page.kind !== 'cover' && page.kind !== 'back') {
    const f = document.createElement('div')
    f.className = 'album-folio'
    f.textContent = String(idx).padStart(2, '0')
    div.appendChild(f)
  }
  return div
}

/** 一页的画面（DOM 版，不经 React） */
function buildLeafFace(page) {
  const box = document.createElement('div')
  box.style.position = 'absolute'
  box.style.inset = '0'

  const grain = () => {
    const g = document.createElement('div')
    g.className = 'album-grain'
    return g
  }
  const mk = (cls, html) => {
    const d = document.createElement('div')
    d.className = cls
    d.innerHTML = html
    return d
  }

  if (page.kind === 'cover') {
    box.appendChild(mk('album-paper', ''))
    box.appendChild(mk('album-cover',
      `<div><div class="album-cover-mark">✂</div>
       <div class="album-cover-title">旅行随记</div>
       <div class="album-cover-sub">TRAVELOGUE</div>
       <div class="album-cover-rule"></div>
       <div class="album-cover-sub" style="letter-spacing:.18em">结构保真 · 影像留存</div></div>
       <div class="album-cover-foot">换颜 Reskin 转化而成</div>`))
    box.appendChild(grain())
    return box
  }
  if (page.kind === 'back') {
    box.appendChild(mk('album-endpaper',
      `<div class="album-endpaper-inner">换颜 Reskin<br/>
       <span style="opacity:.72;font-size:11.5px">实拍摄影 · 结构保真转化</span></div>`))
    return box
  }
  if (page.blank) { box.appendChild(mk('album-paper', '')); return box }

  box.appendChild(mk('album-paper', ''))
  if (page.src) {
    const img = document.createElement('img')
    img.className = 'album-photo'
    img.src = imgSrc(page.src)
    img.alt = page.title || page.place || '旅行照片'
    img.draggable = false
    box.appendChild(img)
  }
  const cap = []
  if (page.place) cap.push(`<div class="album-place">${esc(page.place)}</div>`)
  if (page.title) cap.push(`<div class="album-title">${esc(page.title)}</div>`)
  if (page.story) cap.push(`<div class="album-story">${esc(page.story)}</div>`)
  if (cap.length) box.appendChild(mk('album-caption', cap.join('')))
  box.appendChild(grain())
  return box
}

/** 文案转义（页子走 innerHTML，必须防注入） */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}
