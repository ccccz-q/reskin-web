import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, imgSrc, thumbSrc } from './api'

/**
 * 历史作品仓库 —— 生成图的存档，按日期分组，可多选打包下载
 *
 * 交互设计：
 *  - 单击卡片 = 选中/取消（勾选圈 + 描边 + 上浮）
 *  - 双击卡片 = 放大预览（不是全屏：约 78% 视口的居中大卡 + 毛玻璃背景）
 *  - 放大/缩小用弹簧过冲曲线（超过目标值再回弹），关闭时反向播放
 *  - 底部浮动操作条：已选数量 → zip 打包下载（后端 zipfile 标准库）
 *  - 每个日期分组有「本日全选」
 *
 * 之前一次真实教训：勾选后用 galleryDownload 拿 zip 时必须走 blob，
 * 不能直接 <a href> —— 那在某些浏览器会导航而不是保存。
 */
const SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)'   // 弹簧过冲

function groupByDate(items) {
  const map = new Map()
  for (const it of items) {
    const day = (it.modified || '').slice(0, 10) || '未知日期'
    if (!map.has(day)) map.set(day, [])
    map.get(day).push(it)
  }
  // 日期倒序（新的在上），组内已由后端按时间倒序
  return [...map.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1))
}

let _galCache = { ts: 0, items: null }   // 打开过一次后 60s 内复用，避免每次全量拉

export default function GalleryModal({ onClose, flash, pickMode = false, onPick, pickHint = '用这张' }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(() => new Set())
  const [viewer, setViewer] = useState(null)      // {url, filename} 大图预览
  const [viewerClosing, setViewerClosing] = useState(false)
  const [zipping, setZipping] = useState(false)
  const [entered, setEntered] = useState(false)   // 入场动画
  const viewerTimer = useRef(null)

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        let r
        if (_galCache.items && Date.now() - _galCache.ts < 30000) {
          r = _galCache.items
        } else {
          r = await api.gallery(200)
          _galCache = { ts: Date.now(), items: r }
        }
        if (!alive) return
        // ★ 历史仓库只显示「作品」：生成图 + 种子展示图；上传的原图不进仓库
        setItems((r.items || []).filter((x) => x.kind === 'generated' || x.kind === 'seed'))
      } catch (e) {
        flash?.(`仓库加载失败：${e.message}`)
      } finally {
        if (alive) setLoading(false)
      }
      requestAnimationFrame(() => alive && setEntered(true))
    })()
    return () => { alive = false; clearTimeout(viewerTimer.current) }
  }, [flash])

  const groups = useMemo(() => groupByDate(items), [items])

  const toggle = useCallback((url) => {
    // 选图模式：单选（再点一次取消选择），不复选
    if (pickMode) {
      setSelected((prev) => (prev.has(url) ? new Set() : new Set([url])))
      return
    }
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(url) ? next.delete(url) : next.add(url)
      return next
    })
  }, [])

  const selectDay = useCallback((day, urls) => {
    setSelected((prev) => {
      const next = new Set(prev)
      const allIn = urls.every((u) => next.has(u))
      urls.forEach((u) => (allIn ? next.delete(u) : next.add(u)))
      return next
    })
  }, [])

  const saveBlob = useCallback((blob, filename) => {
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(a.href)
  }, [])

  const downloadSelected = useCallback(async () => {
    const urls = [...selected]
    if (urls.length === 0) return
    setZipping(true)
    try {
      if (urls.length === 1) {
        // 单张：直接下原图，不用打包
        const resp = await fetch(imgSrc(urls[0]))
        const blob = await resp.blob()
        const ext = (blob.type.split('/')[1] || 'jpg').replace('jpeg', 'jpg')
        saveBlob(blob, `换颜-${new Date().toISOString().slice(0, 10)}.${ext}`)
      } else {
        const resp = await api.galleryDownload(urls)
        saveBlob(await resp.blob(), `作品-${new Date().toISOString().slice(0, 10)}.zip`)
      }
      flash?.(`已下载 ${urls.length} 张`)
    } catch (e) {
      flash?.(e.message || '下载失败')
    } finally {
      setZipping(false)
    }
  }, [selected, flash, saveBlob])

  /** 双击放大。用两段式关闭（先播缩小动画再卸载），否则关闭会显得突兀 */
  const openViewer = useCallback((it) => setViewer(it), [])

  const closeViewer = useCallback(() => {
    setViewerClosing(true)
    clearTimeout(viewerTimer.current)
    viewerTimer.current = setTimeout(() => {
      setViewer(null)
      setViewerClosing(false)
    }, 240)                      // 与 CSS 过渡时长一致
  }, [])

  /** 放大态里 ‹ › 切换同批次的上一张/下一张 */
  const step = useCallback((dir) => {
    setViewer((cur) => {
      if (!cur) return cur
      const idx = items.findIndex((x) => x.url === cur.url)
      const next = items[(idx + dir + items.length) % items.length]
      return next || cur
    })
  }, [items])

  useEffect(() => {
    const onKey = (e) => {
      if (!viewer) return
      if (e.key === 'Escape') closeViewer()
      if (e.key === 'ArrowLeft') step(-1)
      if (e.key === 'ArrowRight') step(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [viewer, closeViewer, step])

  return (
    <div className={`gal-mask ${entered ? 'on' : ''}`} onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="gal" role="dialog" aria-label="历史作品仓库">
        <header className="gal-head">
          <div>
            <span className="ws-title">作品仓库</span>
            <span className="ws-sub">按日期归档 · 单击选中，双击放大 · 多选可打包下载</span>
          </div>
          <button className="ws-close" onClick={onClose}>关闭</button>
        </header>

        <div className="gal-body">
          {loading && <p className="muted gal-loading">加载中…</p>}
          {!loading && items.length === 0 && (
            <p className="muted gal-loading">还没有作品 —— 出几张图之后这里就有了。</p>
          )}

          {groups.map(([day, list]) => (
            <section key={day} className="gal-day">
              <div className="gal-day-head">
                <h3 className="gal-day-title">{day}</h3>
                <span className="muted">{list.length} 张</span>
                <button className="gal-pick-day" onClick={() => selectDay(day, list.map((x) => x.url))}>
                  本日全选
                </button>
              </div>
              <div className="gal-grid">
                {list.map((it, i) => {
                  const on = selected.has(it.url)
                  return (
                    <figure key={it.url}
                            className={`gal-card ${on ? 'on' : ''}`}
                            style={{ animationDelay: `${Math.min(i * 35, 400)}ms` }}
                            onClick={() => toggle(it.url)}
                            onDoubleClick={() => openViewer(it)}
                            title="单击选中 · 双击放大">
                      <div className="gal-thumb">
                        <img src={thumbSrc(it.url, 360)} alt={it.filename} loading="lazy" decoding="async" />
                        {it.kind === 'seed' && <span className="gal-seed-tag">种子</span>}
                      </div>
                      <figcaption className="gal-name">{it.filename}</figcaption>
                      <span className="gal-check" aria-hidden="true">✓</span>
                    </figure>
                  )
                })}
              </div>
            </section>
          ))}
        </div>

        {selected.size > 0 && (
          <div className="gal-bar">
            <span>已选 <b>{selected.size}</b> 张</span>
            <span className="tb-spacer" />
            <button className="btn ghost tb-btn" onClick={() => setSelected(new Set())}>取消选择</button>
            {pickMode ? (
              <button className="btn solid tb-btn"
                      onClick={() => {
                        const url = [...selected][0]
                        onPick?.(url)
                        onClose?.()
                      }}>
                {pickHint}
              </button>
            ) : (
              <button className="btn solid tb-btn" onClick={downloadSelected} disabled={zipping}>
                {zipping ? '打包中…' : (selected.size > 1 ? '⬇ 下载 ZIP' : '⬇ 下载图片')}
              </button>
            )}
          </div>
        )}
      </div>

      {/* ── 放大预览：约 78% 视口的大卡，不是全屏；弹簧缩放 + 毛玻璃背景 ── */}
      {viewer && (
        <div className={`zoom-mask ${viewerClosing ? 'out' : ''}`}
             onClick={closeViewer} role="dialog" aria-label="图片预览">
          <div className="zoom-card" onClick={(e) => e.stopPropagation()}>
            <img src={imgSrc(viewer.url)} alt={viewer.filename} />
            <div className="zoom-meta">
              <span className="zoom-name">{viewer.filename}</span>
              <span className="muted">{(viewer.modified || '').slice(0, 10)}</span>
              <span className="tb-spacer" />
              <button className="zoom-nav" onClick={() => step(-1)} aria-label="上一张">‹</button>
              <button className="zoom-nav" onClick={() => step(1)} aria-label="下一张">›</button>
              <button className="btn ghost tb-btn"
                      onClick={async () => {
                        const resp = await fetch(imgSrc(viewer.url))
                        saveBlob(await resp.blob(), viewer.filename)
                        flash?.('已下载')
                      }}>
                ⬇ 下载
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
