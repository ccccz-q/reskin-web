import { useCallback, useEffect, useRef, useState } from 'react'
import { api, imgSrc } from './api'
import GalleryModal from './GalleryModal'

/**
 * 模板工坊 —— 「风格理论 + 参考图 → 新家族」，并支持反复迭代直到满意
 *
 * 流程被刻意拆成**四个独立动作**，而不是一个「一键生成」：
 *   1. 提炼   从理论 + 参考图得到第一版
 *   2. 迭代   带着上一版 + 你的意见改下一版（版本可回溯）
 *   3. 安装   写进家族目录，变成可用风格
 *   4. 入库   每一版都自动存，随时能翻回去
 *
 * 之所以不合成一个按钮：用户点一次「不满意重来」就丢掉前面所有成果的话，
 * 他就不敢点了。迭代必须建立在「上一版还在」的前提上。
 */
export default function Workshop({ onClose, onInstalled, flash }) {
  const [theory, setTheory] = useState('')
  const [notes, setNotes] = useState('')
  const [name, setName] = useState('')
  const [images, setImages] = useState([])      // [{url, filename}]
  // ★ busy 只表示「当前版本的同步操作（迭代/安装）进行中」——
  //   后台提炼任务的运行状态由 bgTasks 表达，两者绝不混用：
  //   旧版轮询 setBusy(running>0) 会把后台任务误当成锁，导致提炼期间
  //   「复制提示词 / 安装为家族」被禁；而 doInstall 结束时 setBusy(false)
  //   又会反过来误清后台状态（实测互踩 bug，2026-10-03 拆分）。
  const [busy, setBusy] = useState(false)
  const [installing, setInstalling] = useState(false)   // 安装动作本身的短锁
  const [copying, setCopying] = useState(false)         // 复制动作本身的短锁
  const [submitting, setSubmitting] = useState(false)  // 提交动作本身的短锁（防双击）
  const submitLock = useRef(false)              // setSubmitting 是异步的，要同步标志

  const [current, setCurrent] = useState(null)  // {id, lineage, version, spec, prompt, ...}
  const [versions, setVersions] = useState([])  // 迭代链
  const [feedback, setFeedback] = useState('')
  const [library, setLibrary] = useState([])
  const [tab, setTab] = useState('draft')       // draft | library
  const [elapsed, setElapsed] = useState(0)     // 保留：同步操作（迭代/安装）的耗时显示
  const [phase, setPhase] = useState('分析图片')
  const [showPrompt, setShowPrompt] = useState(false)
  const [pickExample, setPickExample] = useState(false)   // 从作品仓库选图做家族示例
  const [bgTasks, setBgTasks] = useState([])    // 并行提炼任务的实时快照列表
  const [copied, setCopied] = useState(false)   // 复制按钮的 ✓ 动画
  const [stylePrompt, setStylePrompt] = useState('')   // 用户收集的生图 Prompt（第三输入源）
  // ★ 强确认弹窗队列：多任务并行提炼时可能先后完成，逐个弹、确认一个再弹下一个。
  //   在工坊里等结果时必须点「确认」才消失；不在工坊时由 App 的全局哨兵弹 forgeNotice。
  const [donePopupQueue, setDonePopupQueue] = useState([])
  // ★ 手改提示词（实测需求 2026-10-03）：只改片段不用跑 4-6 分钟 LLM 迭代，
  //   直接编辑文本保存即可；重新提炼前用 reviseConfirm 弹窗让用户三思。
  const [promptEditing, setPromptEditing] = useState(false)
  const [promptDraft, setPromptDraft] = useState('')
  const [promptSaving, setPromptSaving] = useState(false)
  const [reviseConfirm, setReviseConfirm] = useState(false)  // 重新提炼确认弹窗

  // 弹窗打开期间 Esc 完全失效（capture 阶段拦截，先于对话框的 Esc 关闭逻辑）
  useEffect(() => {
    if (donePopupQueue.length === 0 && !reviseConfirm) return
    const stop = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation() }
    }
    document.addEventListener('keydown', stop, true)
    return () => document.removeEventListener('keydown', stop, true)
  }, [donePopupQueue.length, reviseConfirm])
  // ★ 提炼要发 9–11 次模型调用，耗时以分钟计。
  //   没有进度反馈的话用户只会以为"卡死了"—— 显示已用秒数 + 当前阶段。

  const fileRef = useRef(null)
  const [refsDragging, setRefsDragging] = useState(false)   // 参考图区拖拽悬停高亮

  // 全局粘贴：工坊开着时，Ctrl+V 的截图直接进参考图区
  // （主画布的粘贴监听在工坊打开时会主动让位，两边不会重复上传）
  useEffect(() => {
    const onPaste = (e) => {
      const items = e.clipboardData?.items
      if (!items) return
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const f = it.getAsFile()
          if (f) {
            e.preventDefault()
            ;(async () => {
              try {
                const r = await api.upload(f)
                setImages((prev) => [...prev, { url: r.url, filename: r.filename }])
                flash?.(`已粘贴 ${r.filename}`)
              } catch (err) {
                flash?.(err.message)
              }
            })()
          }
          break
        }
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [flash])
  const dialogRef = useRef(null)
  const restoreRef = useRef(null)

  // ── 对话框该有的三件事：进来先给焦点、Esc 能关、Tab 不会跑出去
  useEffect(() => {
    restoreRef.current = document.activeElement
    const node = dialogRef.current
    const list = () => Array.from(node?.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) || [])
    list()[0]?.focus()

    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose?.()
        return
      }
      if (e.key !== 'Tab') return
      const items = list()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      else if (!node.contains(document.activeElement)) { e.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      restoreRef.current?.focus?.()
    }
  }, [onClose])

  const refreshLibrary = useCallback(async () => {
    try {
      const r = await api.forgeLibrary()
      setLibrary(r.items || [])
    } catch { /* 库读不出来不该挡住使用 */ }
  }, [])

  // 库只在「真的要看」时才拉：开工坊本身不该顺手打一个用户没请求的接口。
  // 提炼 / 迭代 / 安装成功后各有一次 refreshLibrary()，所以数据不会旧。
  const openLibrary = () => {
    setTab('library')
    refreshLibrary()
  }

  const uploadRefs = async (files) => {
    for (const f of Array.from(files || [])) {
      try {
        const r = await api.upload(f)
        setImages((prev) => [...prev, { url: r.url, filename: r.filename }])
      } catch (e) {
        flash?.(e.message || '上传失败')
      }
    }
  }

  // ═══ 后台提炼 ═══
  // 提炼在服务端线程跑；本组件只负责"看进度"。用户可以关掉工坊去干别的 ——
  // 关掉后由 App 的全局哨兵接管轮询，完成时在任意页面弹窗。
  const taskTimerRef = useRef(null)
  const stopTaskTimer = useCallback(() => {
    if (taskTimerRef.current) { clearInterval(taskTimerRef.current); taskTimerRef.current = null }
  }, [])

  const readIds = useCallback(() => {
    // 兼容旧的单值键：迁移进新数组键
    const legacy = localStorage.getItem('forgeTaskId')
    if (legacy) {
      localStorage.removeItem('forgeTaskId')
      let cur = []
      try { cur = JSON.parse(localStorage.getItem('forgeTaskIds') || '[]') } catch { /* ignore */ }
      const ids = [legacy, ...cur.filter((x) => x !== legacy)]
      localStorage.setItem('forgeTaskIds', JSON.stringify(ids))
      return ids
    }
    try { return JSON.parse(localStorage.getItem('forgeTaskIds') || '[]') } catch { return [] }
  }, [])

  const writeIds = useCallback((ids) => {
    localStorage.setItem('forgeTaskIds', JSON.stringify(ids))
  }, [])

  const finishOne = useCallback((t) => {
    // 多任务下可能先后完成 —— 弹窗入队，确认一个再弹下一个
    setDonePopupQueue((q) => [...q, {
      ok: t.status === 'done',
      cancelled: t.status === 'cancelled',
      name: t.result?.name || '未命名',
      version: t.result?.version,
      elapsed: t.elapsed_sec,
      error: t.error,
    }])
    if (t.status === 'done' && t.result?.id) {
      ;(async () => {
        try {
          const row = await api.forgeGet(t.result.id)
          setCurrent(row)
          setVersions(row.id ? [{ id: row.id, version: row.version, installed: false }] : [])
        } catch { /* 草稿详情读不出来不该崩 */ }
        if (t.result.version != null) {
          flash?.(`「${t.result.name}」第 ${t.result.version} 版已就绪`)
        }
        refreshLibrary()
      })()
    } else if (t.status === 'cancelled') {
      flash?.('提炼已中止，本次没有保存任何版本')
    } else {
      flash?.(t.error || '提炼失败')
    }
  }, [flash, refreshLibrary])

  const startWatchAll = useCallback(() => {
    stopTaskTimer()
    taskTimerRef.current = setInterval(async () => {
      const ids = readIds()
      if (!ids.length) { setBgTasks([]); stopTaskTimer(); return }
      const snaps = await Promise.all(
        ids.map((id) => api.forgeTask(id).catch(() => null)))
      const running = []
      const finishedIds = []
      for (let i = 0; i < ids.length; i++) {
        const t = snaps[i]
        if (!t) { finishedIds.push(ids[i]); continue }   // 404：服务重启丢了任务，清掉
        if (t.status === 'done' || t.status === 'failed' || t.status === 'cancelled') {
          finishedIds.push(ids[i])
          finishOne(t)
        } else {
          running.push(t)
        }
      }
      if (finishedIds.length) writeIds(readIds().filter((x) => !finishedIds.includes(x)))
      setBgTasks(running)
      // ★ 不碰 busy —— busy 只属于「当前版本的同步操作」；后台任务状态在 bgTasks 里
      if (!running.length && !readIds().length) stopTaskTimer()
    }, 2500)
  }, [finishOne, readIds, stopTaskTimer, writeIds])

  // 挂载时若有未完成任务（用户曾离开再回来），接管全部并行任务
  useEffect(() => {
    if (readIds().length) startWatchAll()
    return stopTaskTimer
  }, [startWatchAll, stopTaskTimer, readIds])

  const doDraft = async () => {
    if (submitLock.current) return           // 同步守卫：只防提交动作本身的双击
    if (images.length === 0 && !theory.trim() && !stylePrompt.trim()) {
      return flash?.('至少上传一张参考图，或写一段风格理论 / 贴一段风格提示词。')
    }
    submitLock.current = true
    setSubmitting(true)
    try {
      const r = await api.forgeDraftAsync({
        theory,
        image_urls: images.map((i) => i.url),
        user_notes: notes,
        style_prompt: stylePrompt,
        name,
      })
      writeIds([...readIds(), r.task_id])
      const n = readIds().length
      flash?.(n > 1
        ? `已开始提炼（现有 ${n} 个任务并行）—— 可以继续上传下一组，完成后会逐个提醒`
        : '已在后台开始提炼 —— 可以继续提交下一组或去别的页面，完成后会提醒你')
      startWatchAll()
    } catch (e) {
      flash?.(e.message || '提交失败')
    } finally {
      submitLock.current = false
      setSubmitting(false)
    }
  }

  const doRemove = async (it) => {
    try {
      await api.forgeDelete(it.id)
      flash?.(`已移除「${it.name}」第 ${it.version} 版`)
      if (current?.id === it.id) {           // 删的是当前打开的版本 → 清掉右侧展示
        setCurrent(null)
        setVersions([])
      }
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '移除失败')
    }
  }

  // ── 中止一个后台提炼任务（协作式：正在跑的模型调用结束后立即终止，
  //    解构阶段单次调用约 20–60s，因此「中止中」可能持续几十秒——非卡死）
  const doCancel = async (t) => {
    try {
      await api.forgeCancel(t.task_id)
      flash?.('已请求中止 —— 正在进行的模型调用跑完后任务立即终止（通常几秒到一分钟）')
      setBgTasks((prev) => prev.map((x) => (x.task_id === t.task_id
        ? { ...x, cancelling: true, phase: '已请求中止（当前阶段结束后立即终止）' } : x)))
    } catch (e) {
      flash?.(e.message || '中止失败')
    }
  }

  // ── 重新提炼确认（用户需求 2026-10-03）────────────────────
  // 「迭代一版」会重跑整条 LLM 链（4-6 分钟）——点下去之前必须让用户知道：
  //   ① 只改个别片段 → 用「修改提示词」直接编辑保存，秒级生效；
  //   ② 有手改时，重新提炼出的新版本会从自动渲染重新开始（手改不跨版本）。
  const onReviseClick = () => {
    if (!current?.id) {
      return flash?.('当前没有打开的版本（可能刚被删除）—— 请从「我的库」打开一版再迭代')
    }
    if (!feedback.trim()) return
    setReviseConfirm(true)
  }

  const doRevise = async () => {
    setReviseConfirm(false)
    if (!current?.id) {
      return flash?.('当前没有打开的版本（可能刚被删除）—— 请从「我的库」打开一版再迭代')
    }
    if (!feedback.trim()) return
    setBusy(true)
    try {
      // ★ 左侧参考图区若贴了新图（如理想效果图），本轮迭代会对新图做视觉识别
      const r = await api.forgeRevise(
        current.id, feedback, images.map((i) => i.url))
      if (!r.ok) {
        return flash?.('这一版没通过校验，上一版仍然保留 —— 换个说法再试试')
      }
      setCurrent(r)
      setVersions((prev) => [...prev, { id: r.id, version: r.version, installed: false }])
      setFeedback('')
      setPromptEditing(false)
      flash?.(`已更新到第 ${r.version} 版`
        + (images.length > 0 ? `（已按你贴的 ${images.length} 张新参考图做视觉识别）` : ''))
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '迭代失败')
    } finally {
      setBusy(false)
    }
  }

  // ── 手改提示词：保存 / 恢复自动 ─────────────────────────
  const doSavePrompt = async (text) => {
    if (!current?.id || promptSaving) return
    const trimmed = (text || '').trim()
    if (trimmed && trimmed.length < 20) {
      return flash?.('改后的提示词太短了（至少 20 字）—— 想恢复自动渲染请用「恢复自动」')
    }
    setPromptSaving(true)
    try {
      const r = await api.forgeSavePrompt(current.id, trimmed)
      setCurrent((cur) => ({ ...cur, prompt: r.prompt, prompt_override: r.prompt_override }))
      setPromptEditing(false)
      flash?.(r.message || (trimmed ? '已保存手改提示词' : '已恢复自动渲染'))
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '保存失败')
    } finally {
      setPromptSaving(false)
    }
  }

  const doInstall = async () => {
    if (!current?.id) return
    setInstalling(true)
    try {
      const r = await api.forgeInstall(current.id)
      // ★ 2026-10-06：后端现在会带回 QC 警告与安装期问题（此前算完就丢）。
      //   这里合并成一条完整提示 —— 只报成功会让用户以为一切正常，
      //   而「这个家族有隐患」「下次迭代会带缺陷回来」恰恰是最该说的。
      const notes = [
        ...(r.qc_warnings || []),
        ...(r.warnings || []),
      ].filter(Boolean)
      flash?.(notes.length
        ? `已安装「${r.family_id}」，但有 ${notes.length} 处需要注意：${notes.join('；')}`
        : `已安装「${r.family_id}」，可以在左侧家族列表里用了`)
      onInstalled?.()
      refreshLibrary()
    } catch (e) {
      flash?.(e.message || '安装失败')
    } finally {
      setInstalling(false)
    }
  }

  // ── 给这一版家族设置示例图：打开作品仓库选一张（生成图/展示图）→ 写进
  //    storage/images/examples/<家族id>，左侧风格列表随即带上示例图
  const doSetExample = async (url) => {
    const fid = spec?.id || current?.family_id
    if (!fid || !url) return
    try {
      const r = await api.setFamilyExample(fid, url)
      flash?.(`已设为「${spec?.name || fid}」的示例图`)
      onInstalled?.()          // 让主页面重新拉家族列表（带上新示例图）
      return r
    } catch (e) {
      flash?.(e.message || '设置示例图失败')
    }
  }

  // 三级复制：clipboard API → execCommand → 兜底"展开并选中"。
  // 直接只用 navigator.clipboard 会失败：非 HTTPS 的局域网地址（以及部分
  // 浏览器在 iframe / 非聚焦文档里）拿不到 clipboard 权限，用户点一下什么都没发生。
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

  const doCopy = async () => {
    if (!current?.id) return
    setCopying(true)
    try {
      const r = await api.forgeRender(current.id)
      const text = r.prompt || ''
      if (!text) return flash?.('这一版还没有可复制的提示词')
      if (await copyText(text)) {
        setCopied(true)
        setTimeout(() => setCopied(false), 1800)
        flash?.(`提示词已复制（${text.length} 字）`)
        return
      }
      // 兜底：展开提示词面板并全选，用户按 Ctrl/⌘ + C 即可
      setShowPrompt(true)
      flash?.('浏览器拦住了自动复制 —— 提示词已展开并选中，请按 Ctrl/⌘ + C')
      setTimeout(() => {
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
  }

  const loadVersion = async (id) => {
    try {
      const r = await api.forgeGet(id)
      setCurrent(r)
      setVersions(r.versions || [])
    } catch { /* ignore */ }
  }

  const spec = current?.spec
  const seg = spec?.segments
  // ★ 失败版本识别：后端把失败原因写进了记录的 feedback 字段（【提炼失败】前缀）。
  //   不标记的话，一条「未命名 / 空提示词」的失败版会被当成正常版本，误导用户。
  const failNote = typeof current?.feedback === 'string' && current.feedback.startsWith('【提炼失败】')
    ? current.feedback.slice('【提炼失败】'.length)
    : ''

  return (
    <div className="ws-mask" onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="ws" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="ws-h">
        <header className="ws-head">
          <div>
            <span className="ws-title" id="ws-h">模板工坊</span>
            <span className="ws-sub">多张同类型图片（理论可选）→ 提炼视觉语法 → 家族模板，可反复迭代</span>
          </div>
          <div className="ws-tabs" role="group" aria-label="工坊视图">
            <button type="button" className={`ws-tab ${tab === 'draft' ? 'on' : ''}`}
                    aria-pressed={tab === 'draft'}
                    onClick={() => setTab('draft')}>提炼</button>
            <button type="button" className={`ws-tab ${tab === 'library' ? 'on' : ''}`}
                    aria-pressed={tab === 'library'}
                    onClick={openLibrary}>我的库 {library.length > 0 && `(${library.length})`}</button>
          </div>
          <button type="button" className="ws-close" onClick={onClose} aria-label="关闭模板工坊">
            关闭
          </button>
        </header>

        {tab === 'library' ? (
          <div className="ws-body ws-lib">
            {bgTasks.map((t) => (
              <div key={t.task_id} className="lib-task" role="status">
                <span className="lib-task-dot" aria-hidden="true" />
                <span>
                  后台提炼中：{t.phase || '进行中'}
                  （已 {Math.round(t.elapsed_sec || 0)}s）
                  —— 完成后会自动提醒，可以去别的页面
                </span>
                <button type="button" className="link ws-lib-del"
                        disabled={t.cancelling}
                        title="中止这个提炼任务（当前阶段结束后终止）"
                        onClick={() => doCancel(t)}>
                  {t.cancelling ? '中止中…' : '中止'}
                </button>
              </div>
            ))}
            {library.length === 0 && bgTasks.length === 0 && <p className="muted">还没有创造过模板。</p>}
            {library.map((it) => (
              <div key={it.id} className="ws-lib-row">
                <span className="ws-lib-ver">v{it.version}</span>
                <span className="ws-lib-name">{it.name}</span>
                <span className="muted">{it.family_id}</span>
                {it.installed && <span className="tag">已安装</span>}
                <button type="button" className="link"
                        aria-label={`打开 ${it.name} 第 ${it.version} 版`}
                        onClick={() => { loadVersion(it.id); setTab('draft') }}>
                  打开
                </button>
                <button type="button" className="link ws-lib-del"
                        aria-label={`移除 ${it.name} 第 ${it.version} 版`}
                        title="从我的库里移除这一版（不影响已安装的家族文件）"
                        onClick={() => doRemove(it)}>
                  移除
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="ws-body">
            {/* ── 左：输入 ─────────────────────────── */}
            <section className="ws-col ws-in">
              <label className="ws-label" htmlFor="ws-f-name">风格名称（可留空，让模型起名）</label>
              <input id="ws-f-name" className="ws-input" value={name}
                     onChange={(e) => setName(e.target.value)}
                     placeholder="例如：Risograph 孔版" />

              <label className="ws-label" htmlFor="ws-f-theory">风格理论 / 介绍（<b>可选</b>——不写就完全靠看图）</label>
              <textarea id="ws-f-theory" className="ws-input ws-theory" rows={9} value={theory}
                        onChange={(e) => setTheory(e.target.value)}
                        placeholder="把这种风格的起源、工艺、配色逻辑、美学主张贴进来。写得越具体，提炼出的三段式越准。" />

              <label className="ws-label" htmlFor="ws-f-sprompt">风格提示词（<b>可选</b>——把你收集的生图 Prompt 整包贴进来，正向词与 SD 反向词都可以）</label>
              <textarea id="ws-f-sprompt" className="ws-input" rows={6} value={stylePrompt}
                        onChange={(e) => setStylePrompt(e.target.value)}
                        placeholder={'例如：\n我的世界游戏截图，体素方块画风，所有物体由正方形方块拼接构成，方块云朵，明亮晴朗蓝天……\nSD反向词：blurry, deformed, non-block structure, watermark（反向词会被转成禁止项）'} />

              <span className="ws-label" id="ws-f-refs">参考图（<b>主角</b>，推荐同类型 3–6 张；支持拖拽图片到这里，或直接 Ctrl+V 粘贴）</span>
              <div className={`ws-refs ${refsDragging ? 'dragging' : ''}`} role="group" aria-labelledby="ws-f-refs"
                   onDragOver={(e) => { e.preventDefault(); setRefsDragging(true) }}
                   onDragLeave={(e) => {
                     // 只在真正离开容器时熄灭高亮（经过子元素时 relatedTarget 仍在容器内）
                     if (!e.currentTarget.contains(e.relatedTarget)) setRefsDragging(false)
                   }}
                   onDrop={(e) => {
                     e.preventDefault()
                     setRefsDragging(false)
                     if (e.dataTransfer?.files?.length) uploadRefs(e.dataTransfer.files)
                   }}>
                {images.map((im, i) => (
                  <div key={i} className="ws-ref">
                    <img src={imgSrc(im.url)} alt="" />
                    <button type="button" aria-label={`移除第 ${i + 1} 张参考图`}
                            onClick={() => setImages((p) => p.filter((_, j) => j !== i))}>×</button>
                  </div>
                ))}
                <button type="button" className="ws-ref-add" aria-label="添加参考图"
                        onClick={() => fileRef.current?.click()}>+</button>
                <input ref={fileRef} type="file" accept="image/*" multiple hidden tabIndex={-1}
                       onChange={(e) => uploadRefs(e.target.files)} />
              </div>
              {/* 提炼耗时提示 */}
              <p className="ws-note">
                参考图 + 风格理论 + 风格提示词三路综合：图取色板与结构，理论给美学语义，
                提示词给精确的词汇与质感 —— 三样都是可选的，但至少给一样。
              </p>

              <label className="ws-label" htmlFor="ws-f-notes">补充说明（可留空）</label>
              <input id="ws-f-notes" className="ws-input" value={notes}
                     onChange={(e) => setNotes(e.target.value)}
                     placeholder="例如：只用于人物照、不要出现文字" />

              <button type="button" className="btn-generate" onClick={doDraft}
                      disabled={submitting}>
                {/* ★ 不再因后台任务显示「提炼中…」—— 提炼在后台跑，这个按钮只是
                    提交入口；旧文案会让用户把迭代/其它操作误读成「正在提炼」
                    （实测 2026-10-03：用户点了迭代，按钮却写提炼中）。 */}
                {submitting ? '提交中…' : current ? '重新提炼' : '开始提炼'}
              </button>
              {bgTasks.length > 0 && (
                <p className="ws-note">
                  另有 {bgTasks.length} 个提炼任务在后台进行（见下方任务卡），可继续提交下一组。
                </p>
              )}

              {/* ── 后台任务实时卡：中止入口也在这里（2026-10-03 新增）── */}
              {bgTasks.length > 0 && (
                <div className="ws-tasks" role="status">
                  {bgTasks.map((t) => (
                    <div key={t.task_id} className="lib-task">
                      <span className="lib-task-dot" aria-hidden="true" />
                      <span className="lib-task-txt">
                        {t.name ? `「${t.name}」` : ''}后台提炼中：{t.phase || '进行中'}
                        （已 {Math.round(t.elapsed_sec || 0)}s）
                      </span>
                      <button type="button" className="link ws-lib-del"
                              disabled={t.cancelling}
                              title="中止这个提炼任务（正在进行的模型调用跑完即终止）"
                              onClick={() => doCancel(t)}>
                        {t.cancelling ? '中止中…' : '中止'}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* ── 右：结果 ─────────────────────────── */}
            <section className="ws-col ws-out">
              {!current && (
                <p className="muted ws-empty">
                  左边填好理论（或传几张参考图）后点「开始提炼」。<br />
                  一次调用大概十几秒，会产出一套完整的三段式提示词结构。
                </p>
              )}

              {current && (
                <>
                  <div className="ws-ver-bar" role="group" aria-label="迭代版本">
                    {versions.map((v) => (
                      <button type="button" key={v.id}
                              className={`ws-ver ${v.id === current.id ? 'on' : ''}`}
                              aria-pressed={v.id === current.id}
                              onClick={() => loadVersion(v.id)}>
                        v{v.version}
                      </button>
                    ))}
                    <span className="muted">共 {versions.length} 版</span>
                  </div>

                  <div className="ws-spec">
                    <span className="ws-icon" aria-hidden="true">{spec?.icon || '◧'}</span>
                    <div>
                      <div className="ws-name">{spec?.name || '未命名'}</div>
                      <div className="muted">{spec?.description}</div>
                      <div className="ws-tags">
                        <span className="tag">{spec?.layout}</span>
                        <span className="tag">{spec?.forbid_scope}</span>
                        <span className="tag">{spec?.id}</span>
                      </div>
                    </div>
                  </div>

                  {failNote && (
                    <div className="notice bad" role="alert">
                      ⛔ 这一版提炼失败：{failNote}
                      <br />瞬时网络错误已支持自动重试 —— 请回到左侧点「重新提炼」再试一次。
                    </div>
                  )}
                  {current.errors?.length > 0 && (
                    <div className="notice bad" role="alert">
                      校验未通过：{current.errors.join(' / ')}
                    </div>
                  )}
                  {current.warnings?.length > 0 && (
                    <div className="notice warn">⚠ {current.warnings.join(' / ')}</div>
                  )}

                  {seg && (
                    <div className="segments">
                      <div className="seg preserve">
                        <span className="seg-tag">保留</span><p>{seg.preserve}</p>
                      </div>
                      <div className="seg creative">
                        <span className="seg-tag">创作</span><p>{seg.creative}</p>
                      </div>
                      <div className="seg forbid">
                        <span className="seg-tag">禁止</span><p>{seg.forbid}</p>
                      </div>
                    </div>
                  )}

                  <label className="ws-label" htmlFor="ws-f-fb">对这一版的修改意见（左侧贴了新图会一并做视觉识别）</label>
                  <textarea id="ws-f-fb" className="ws-input" rows={3} value={feedback}
                            onChange={(e) => setFeedback(e.target.value)}
                            placeholder={'例如：按照理想图保留顶部 YOU DIED 界面的位置、布局和样式，文字纯白、按钮灰色石质。\n左侧贴了新参考图（如理想效果图）时，迭代会以它为识别对象。'} />
                  <div className="ws-actions">
                    <button type="button" className="btn ghost" onClick={onReviseClick}
                            disabled={busy || !feedback.trim() || !!failNote || !spec}>
                      {busy ? '修改中…' : '迭代一版'}
                    </button>
                    {/* ★ 提炼中不再锁这里的操作：复制/安装只看这一版自己的数据，
                        与后台有没有任务在跑无关（busy 只代表迭代进行中） */}
                    <button type="button"
                            className={`copy-btn ${copied ? 'done' : ''}`}
                            onClick={doCopy} disabled={copying || !current.prompt}
                            title="复制当前版本的完整提示词">
                      <span className="copy-ico" aria-hidden="true">{copied ? '✓' : '⧉'}</span>
                      {copied ? '已复制' : '复制提示词'}
                    </button>
                    <button type="button" className="btn solid" onClick={doInstall}
                            disabled={installing || !current.ok}>
                      {installing ? '安装中…' : '安装为家族'}
                    </button>
                    <button type="button" className="btn ghost" onClick={() => setPickExample(true)}
                            disabled={!(spec?.id || current?.family_id)}
                            title="从作品仓库选一张图，作为这个家族的示例图">
                      🖼 设置示例图
                    </button>
                    <button type="button" className="ws-prompt-toggle"
                            onClick={() => setShowPrompt((v) => !v)}>
                      {showPrompt ? '收起提示词 ▴' : '查看提示词 ▾'}
                    </button>
                  </div>

                  {showPrompt && (
                    <div className="ws-prompt-panel">
                      <div className="ws-prompt-head">
                        <span className={`ws-prompt-badge ${current.prompt_override ? 'manual' : 'auto'}`}>
                          {current.prompt_override ? '✎ 已手动修改' : '⚙ 自动渲染'}
                        </span>
                        {!promptEditing && (
                          <span className="ws-prompt-tools">
                            <button type="button" className="link-btn"
                                    onClick={() => { setPromptDraft(current.prompt_override || current.prompt || ''); setPromptEditing(true) }}
                                    disabled={!current.prompt && !current.prompt_override}>
                              修改提示词
                            </button>
                            {current.prompt_override && (
                              <button type="button" className="link-btn"
                                      onClick={() => doSavePrompt('')}
                                      disabled={promptSaving}>
                                恢复自动渲染
                              </button>
                            )}
                          </span>
                        )}
                      </div>
                      {promptEditing ? (
                        <>
                          <textarea className="ws-input ws-prompt-editor" rows={14}
                                    value={promptDraft}
                                    onChange={(e) => setPromptDraft(e.target.value)}
                                    spellCheck={false} />
                          <div className="ws-prompt-tools" style={{ marginTop: 8 }}>
                            <button type="button" className="btn solid" onClick={() => doSavePrompt(promptDraft)}
                                    disabled={promptSaving}>
                              {promptSaving ? '保存中…' : '保存修改'}
                            </button>
                            <button type="button" className="btn ghost" onClick={() => setPromptEditing(false)}
                                    disabled={promptSaving}>
                              取消
                            </button>
                            <span className="muted">保存后生成图片时直接使用这份文本，无需重新提炼</span>
                          </div>
                        </>
                      ) : current.prompt
                        ? <pre className="ws-prompt-text">{current.prompt}</pre>
                        : <p className="muted">这一版还没渲染出提示词（可能校验未通过）。</p>}
                      <p className="ws-prompt-note">
                        {current.prompt_override
                          ? '这份提示词被你手动改过 —— 生成图片时直接使用它（参数调节暂不参与）；「恢复自动渲染」可回到模板自动生成。'
                          : '这是最终发给生图模型的三段式全文。只改个别片段：点「修改提示词」直接编辑保存，立即生效；整版都不满意才需要「迭代一版」（重新提炼约 4-6 分钟）。'}
                      </p>
                    </div>
                  )}
                  <p className="ws-note">
                    每一版都会自动存进「我的库」，随时可以翻回更早的版本。
                    安装后会写进家族目录，左侧列表立即出现。
                  </p>
                </>
              )}
            </section>
          </div>
        )}

        {/* ★ 重新提炼确认弹窗（用户需求 2026-10-03）：重跑 LLM 前，
            告知「可以先直接改提示词」+ 预计耗时；有手改时额外提醒会被替代 */}
        {reviseConfirm && (() => {
          const hasManual = !!current?.prompt_override
          return (
          <div className="forge-notice-mask" style={{ zIndex: 80 }} role="alertdialog"
               aria-modal="true" aria-label="确认重新提炼">
            <div className="forge-notice">
              <header className="fn-head">
                <span className="fn-title">⟳ 确认重新提炼？</span>
              </header>
              <div className="fn-body">
                <p>重新提炼会由 AI 重写整版提示词并生成新版本，<b>约需 4-6 分钟</b>。</p>
                <p>
                  💡 如果只是<b>个别片段不对</b>，不用重新提炼 ——
                  点「查看提示词 ▾」→「修改提示词」，直接改字保存即可，<b>立即生效</b>。
                </p>
                {hasManual && (
                  <p className="notice warn">⚠ 这一版有你手动修改过的提示词；
                    重新提炼产生的新版本将从自动渲染重新开始，手改内容不会带过去。</p>
                )}
                <p className="muted">修改意见：「{feedback.trim().slice(0, 60)}{feedback.trim().length > 60 ? '…' : ''}」</p>
              </div>
              <div className="fn-actions">
                <button type="button" className="btn solid" autoFocus
                        onClick={doRevise}>
                  确认，重新提炼
                </button>
                <button type="button" className="btn ghost"
                        onClick={() => { setReviseConfirm(false); setShowPrompt(true) }}>
                  我先直接改提示词
                </button>
                <button type="button" className="btn ghost"
                        onClick={() => setReviseConfirm(false)}>
                  取消
                </button>
              </div>
            </div>
          </div>
          )
        })()}

        {/* ★ 提炼结果强确认弹窗（队列）：多任务并行时逐个弹，确认一个再弹下一个。
            盖住整个工坊，只能点「确认」关闭（无遮罩关闭、Esc 在 capture 阶段被拦截） */}
        {donePopupQueue.length > 0 && (() => {
          const dp = donePopupQueue[0]
          return (
          <div className="forge-notice-mask" style={{ zIndex: 80 }} role="alertdialog"
               aria-modal="true" aria-label="提炼结果">
            <div className="forge-notice">
              <header className="fn-head">
                <span className="fn-title">
                  {dp.cancelled ? '⚪ 提炼已中止' : dp.ok ? '✅ 提炼成功' : '⛔ 提炼失败'}
                  {donePopupQueue.length > 1 && (
                    <span className="muted">　（还有 {donePopupQueue.length - 1} 个结果待确认）</span>
                  )}
                </span>
              </header>
              <div className="fn-body">
                {dp.cancelled ? (
                  <p>「{dp.name}」的提炼已按你的要求中止，本次没有保存任何版本。</p>
                ) : dp.ok ? (
                  <p>
                    「{dp.name}」第 {dp.version} 版已入库
                    （用时 {Math.round(dp.elapsed || 0)}s）。
                  </p>
                ) : (
                  <p>{dp.error || '提炼未完成'}。可调整图片或理论后重新提炼。</p>
                )}
                <p className="muted">请点下方「确认」继续。</p>
              </div>
              <div className="fn-actions">
                <button type="button" className="btn solid" autoFocus
                        onClick={() => setDonePopupQueue((q) => q.slice(1))}>
                  确认
                </button>
              </div>
            </div>
          </div>
          )
        })()}

        {/* 选图做家族示例：复用作品仓库，单选取一张（放在确认弹窗之后，避免被遮挡） */}
        {pickExample && (
          <GalleryModal pickMode
                        pickHint="设为该家族示例图"
                        onPick={doSetExample}
                        onClose={() => setPickExample(false)}
                        flash={flash} />
        )}
      </div>
    </div>
  )
}
