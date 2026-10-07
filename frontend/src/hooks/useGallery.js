import { useCallback, useEffect, useState } from 'react'
import { api, imgSrc } from '../api'
import { downloadName } from '../lib/download'

/**
 * 作品浏览域：最近生成拉取、单图下载、灯箱。
 *
 * ★ 灯箱与微调面板必须在 toast-wrap 之外：toast-wrap 有 pointer-events:none
 *   （让 toast 不挡点击），该属性会被子元素继承—— 除非每层显式重置。
 *   灯箱需要完整鼠标交互，放里面会导致整层不可点（终审 P0-2）。
 *
 * @param deps.resultUrl 当前成品 url：变了就刷新「最近生成」缩略条
 */
export function useGallery(deps) {
  const { resultUrl, flash, aliveRef } = deps

  const [recent, setRecent] = useState([])   // 最近生成（右栏缩略条）
  const [lightbox, setLightbox] = useState(null)   // {url, dim} —— 示例图/画布/仓库共用
  const [downloading, setDownloading] = useState(false)

  // 最近生成：出图后刷新，让右栏缩略条跟上
  useEffect(() => {
    if (!resultUrl) return
    let alive = true
    ;(async () => {
      try {
        const r = await api.gallery(12, 'generated')
        if (alive) setRecent((r.items || []).slice(0, 8))
      } catch { /* 非关键路径 */ }
    })()
    return () => { alive = false }
  }, [resultUrl])

  const openLightbox = useCallback((payload) => setLightbox(payload), [])
  const closeLightbox = useCallback(() => setLightbox(null), [])

  // 下载成品。同源（走 vite 代理）所以 fetch 不涉及 CORS。
  // 用 blob 而不是直接<a href>：后者在某些浏览器会导航而不是保存。
  const downloadResult = useCallback(async () => {
    if (!resultUrl) return
    setDownloading(true)
    try {
      const resp = await fetch(imgSrc(resultUrl))
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = downloadName(blob.type)
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(a.href)
      flash('已开始下载')
    } catch (e) {
      flash(`下载失败：${e.message}`)
    } finally {
      if (aliveRef.current) setDownloading(false)
    }
  }, [resultUrl, flash, aliveRef])

  return {
    recent, lightbox, openLightbox, closeLightbox,
    downloading, downloadResult,
  }
}