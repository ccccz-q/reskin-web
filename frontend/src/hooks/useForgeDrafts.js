import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { installFlash, promptSaveBlocked } from '../lib/forge'

/**
 * 草稿域：当前打开的版本、迭代链、我的库，以及围绕它们的
 * 复制 / 安装 / 手改提示词 / 移除 / 选示例图。
 *
 * ★ 为什么要独立成 hook：
 *   这些动作共享同一个核心 state（current）。原先它们和输入区的
 *   theory/notes/images 混在一个组件里，于是「加一版」要同时改
 *   组件和它内部的 6 个闭包；现在 current 只属于这个 hook，
 *   迭代链的维护（append / 替换 / 清空）只有一处真相。
 *
 * ★ 行为契约：
 *   - 库只在用户真的切到「我的库」时才拉（refreshLibrary 由调用点触发）。
 *     打开工坊本身不该顺手打一个用户没请求的接口。
 *   - 复制走三级降级（clipboard → execCommand → 展开并选中）：
 *     非 HTTPS 的局域网地址拿不到 clipboard 权限，只用 navigator.clipboard
 *     会让用户点一下什么都没发生。
 */
export function useForgeDrafts(deps) {
  const { flash, onInstalled } = deps

  const [current, setCurrent] = useState(null)   // {id, lineage, version, spec, prompt, ...}
  const [versions, setVersions] = useState([])   // 迭代链
  const [library, setLibrary] = useState([])
  const [tab, setTab] = useState('draft')        // draft | library

  const [installing, setInstalling] = useState(false)
  const [copying, setCopying] = useState(false)
  const [copied, setCopied] = useState(false)     // 复制按钮的 ✓ 动画
  const [promptSaving, setPromptSaving] = useState(false)
  const [promptEditing, setPromptEditing] = useState(false)
  const [promptDraft, setPromptDraft] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [pickExample, setPickExample] = useState(false)

  // ★ 所有 setTimeout 都要登记，卸载时统一清 —— 否则用户关掉工坊后
  //   定时器仍会 setState（React 警告），更糟的是它会操作已卸载的 DOM。
  const timersRef = useRef(new Set())
  const deadRef = useRef(false)

  useEffect(() => {
    deadRef.current = false
    const timers = timersRef.current
    return () => {
      deadRef.current = true
      for (const t of timers) clearTimeout(t)
      timers.clear()
    }
  }, [])

  const later = useCallback((fn, ms) => {
    const id = setTimeout(() => {
      timersRef.current.delete(id)
      if (!deadRef.current) fn()
    }, ms)
    timersRef.current.add(id)
  }, [])

  const refreshLibrary = useCallback(async () => {
    try {
      const r = await api.forgeLibrary()
      setLibrary(r.items || [])
    } catch { /* 库读不出来不该挡住使用 */ }
  }, [])

  const openLibrary = useCallback(() => {
    setTab('library')
    refreshLibrary()
  }, [refreshLibrary])

  /** 打开某一版：右栏展示它，并把迭代链换成它自己的链。 */
  const loadVersion = useCallback(async (id) => {
    try {
      const r = await api.forgeGet(id)
      setCurrent(r)
      setVersions(r.versions || [])
    } catch { /* ignore */ }
  }, [])

  const removeDraft = useCallback(async (it) => {
    try {
      await api.forgeDelete(it.id)
      flash?.(`已移除「${it.name}」第 ${it.version} 版`)
      // 删的是当前打开的版本 → 清掉右侧展示
      setCurrent((c) => (c?.id === it.id ? null : c))
      setVersions((v) => (v.some((x) => x.id === it.id) ? [] : v))
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '移除失败')
    }
  }, [flash, refreshLibrary])

  /** 一版提炼成功后落地：新版本成为当前版本，迭代链追加一节。 */
  const adoptDraft = useCallback((row) => {
    setCurrent(row)
    setVersions(row?.id ? [{ id: row.id, version: row.version, installed: false }] : [])
  }, [])

  /** 迭代成功后落地：沿用原行为（追加到链尾，不整体替换）。 */
  const appendVersion = useCallback((r) => {
    setCurrent(r)
    setVersions((prev) => [...prev, { id: r.id, version: r.version, installed: false }])
  }, [])

  // ── 手改提示词：保存 / 恢复自动 ────────────────────────
  const savePrompt = useCallback(async (text) => {
    if (!current?.id || promptSaving) return
    const blocked = promptSaveBlocked(text, { saving: promptSaving })
    if (blocked) return flash?.(blocked)
    setPromptSaving(true)
    try {
      const r = await api.forgeSavePrompt(current.id, (text || '').trim())
      setCurrent((cur) => ({ ...cur, prompt: r.prompt, prompt_override: r.prompt_override }))
      setPromptEditing(false)
      flash?.(r.message || ((text || '').trim() ? '已保存手改提示词' : '已恢复自动渲染'))
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '保存失败')
    } finally {
      setPromptSaving(false)
    }
  }, [current, promptSaving, flash, refreshLibrary])

  // ── 安装为家族 ───────────────────────────────────────
  const install = useCallback(async () => {
    if (!current?.id) return
    setInstalling(true)
    try {
      const r = await api.forgeInstall(current.id)
      // ★ 2026-10-06：后端现在会带回 QC 警告与安装期问题（此前算完就丢）。
      //   只报成功会让用户以为一切正常，而「这个家族有隐患」恰恰最该说。
      flash?.(installFlash(r))
      onInstalled?.()
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '安装失败')
    } finally {
      setInstalling(false)
    }
  }, [current, flash, onInstalled, refreshLibrary])

  // 三级复制：clipboard API → execCommand → 兜底「展开并选中」。
  const copyText = useCallback(async (text) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        return true
      }
    } catch { /* 权限被拒 / 非安全上下文 → 走下一级 */ }
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return !!ok
    } catch {
      return false
    }
  }, [])

  const copyPrompt = useCallback(async () => {
    if (!current?.id) return
    setCopying(true)
    try {
      const r = await api.forgeRender(current.id)
      const text = r.prompt || ''
      if (!text) return flash?.('这一版还没有可复制的提示词')
      if (await copyText(text)) {
        setCopied(true)
        later(() => setCopied(false), 1800)
        flash?.(`提示词已复制（${text.length} 字）`)
        return
      }
      // 兜底：展开提示词面板并全选，用户按 Ctrl/⌘ + C 即可
      setShowPrompt(true)
      flash?.('浏览器拦住了自动复制 —— 提示词已展开并选中，请按 Ctrl/⌘ + C')
      later(() => {
        const el = document.querySelector('.ws-prompt-text')
        if (!el) return
        const sel = window.getSelection()
        const range = document.createRange()
        range.selectNodeContents(el)
        sel.removeAllRanges()
        sel.addRange(range)
      }, 80)
    } catch (e) {
      flash?.(e.message || '复制失败')
    } finally {
      setCopying(false)
    }
  }, [current, copyText, flash, later])

  // 给这一版家族设置示例图：写进 storage/images/examples/<家族id>，
  // 左侧风格列表随即带上示例图
  const setExample = useCallback(async (url) => {
    const fid = current?.spec?.id || current?.family_id
    if (!fid || !url) return undefined
    try {
      const r = await api.setFamilyExample(fid, url)
      flash?.(`已设为「${current?.spec?.name || fid}」的示例图`)
      onInstalled?.()          // 让主页面重新拉家族列表（带上新示例图）
      return r
    } catch (e) {
      flash?.(e.message || '设置示例图失败')
      return undefined
    }
  }, [current, flash, onInstalled])

  return {
    current, setCurrent, versions, library, tab, setTab,
    installing, copying, copied, promptSaving, promptEditing, setPromptEditing,
    promptDraft, setPromptDraft, showPrompt, setShowPrompt,
    pickExample, setPickExample,
    refreshLibrary, openLibrary, loadVersion, removeDraft,
    adoptDraft, appendVersion, savePrompt, install, copyPrompt, setExample,
  }
}
