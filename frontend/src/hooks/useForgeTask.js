import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import {
  CANCELLED_FLASH,
  CANCEL_PENDING_FLASH,
  CANCEL_PENDING_PHASE,
  donePopupPayload,
  mergeLegacyId,
  parseTaskIds,
  reduceSnapshots,
  settleIds,
  shouldStopWatching,
} from '../lib/forgeTasks'
import {
  EMPTY_INPUT_HINT,
  draftReadyFlash,
  hasAnyInput,
  reviseBlockedReason,
  submitFlash,
} from '../lib/forge'

/** 轮询间隔。与拆分前一致（2500ms）—— 改它会直接改变后台任务的刷新手感。 */
const POLL_MS = 2500

/**
 * 提炼任务域：后台任务的提交 / 轮询 / 中止 / 强确认弹窗队列，
 * 外加「迭代一版」这条同步链路。
 *
 * ★ 为什么要独立成 hook（而不是继续留在 Workshop 里）：
 *   1. 定时器与生命周期的**唯一**清理责任方。轮询定时器 + 死区守卫
 *      都只在这一处创建、也只在这一处销毁，改起来不用再翻 800 行组件。
 *   2. 归约决策（谁完成、谁 404、该不该停）已经是 lib/forgeTasks.js 里的
 *      纯函数（43 个用例锁着）。这里只做「取数 → 调纯函数 → 落地 state」。
 *
 * ★ 行为契约（拆分前后必须完全一致，勿「顺手改进」）：
 *   - 轮询只 setBgTasks，**绝不碰 busy/revising**。后台任务状态归bgTasks，
 *     同步操作状态归 revising，两者混用会造成「提炼期间复制按钮被禁」
 *     以及「安装结束误清后台状态」这类互踩（2026-10-03 实测）。
 *   - 弹窗队列逐个弹：多任务并行时必须「确认一个再弹下一个」。
 */
export function useForgeTask(deps) {
  const {
    flash, current, feedback, images,
    setFeedback, setPromptEditing,
    onDraftReady, onReviseDone, onRefreshLibrary,
  } = deps

  const [bgTasks, setBgTasks] = useState([])          // 并行提炼任务的实时快照
  const [donePopupQueue, setDonePopupQueue] = useState([])
  const [submitting, setSubmitting] = useState(false)
  // setSubmitting 是异步的，挡不住同一 tick 里的双击 —— 必须有同步标志
  const submitLock = useRef(false)
  const [reviseConfirm, setReviseConfirm] = useState(false)
  const [revising, setRevising] = useState(false)

  const taskTimerRef = useRef(null)
  // ★ 卸载守卫：轮询 await 回来时组件可能已经卸载（用户关了工坊）。
  //   没有它就是 setState on unmounted component。
  const deadRef = useRef(false)

  const stopTaskTimer = useCallback(() => {
    if (taskTimerRef.current) {
      clearInterval(taskTimerRef.current)
      taskTimerRef.current = null
    }
  }, [])

  useEffect(() => {
    deadRef.current = false
    return () => { deadRef.current = true; stopTaskTimer() }
  }, [stopTaskTimer])

  const readIds = useCallback(() => {
    // 兼容旧的单值键：迁移进新数组键。不迁移的话那次提炼永远没人收尾。
    const legacy = localStorage.getItem('forgeTaskId')
    if (legacy) {
      localStorage.removeItem('forgeTaskId')
      const ids = mergeLegacyId(legacy, parseTaskIds(localStorage.getItem('forgeTaskIds')))
      localStorage.setItem('forgeTaskIds', JSON.stringify(ids))
      return ids
    }
    return parseTaskIds(localStorage.getItem('forgeTaskIds'))
  }, [])

  const writeIds = useCallback((ids) => {
    localStorage.setItem('forgeTaskIds', JSON.stringify(ids))
  }, [])

  // 一个任务跑完后的收尾：入队弹窗 + 成功的话把新草稿拉出来展示
  const finishOne = useCallback((t) => {
    setDonePopupQueue((q) => [...q, donePopupPayload(t)])
    if (t.status === 'done' && t.result?.id) {
      ;(async () => {
        try {
          const row = await api.forgeGet(t.result.id)
          if (deadRef.current) return
          onDraftReady?.(row)
        } catch { /* 草稿详情读不出来不该崩 */ }
        if (deadRef.current) return
        const msg = draftReadyFlash(t.result)
        if (msg) flash?.(msg)
        onRefreshLibrary?.()
      })()
    } else if (t.status === 'cancelled') {
      flash?.(CANCELLED_FLASH)
    } else {
      flash?.(t.error || '提炼失败')
    }
  }, [flash, onDraftReady, onRefreshLibrary])

  const startWatchAll = useCallback(() => {
    stopTaskTimer()
    taskTimerRef.current = setInterval(async () => {
      const ids = readIds()
      if (!ids.length) {
        setBgTasks([])
        stopTaskTimer()
        return
      }
      const snaps = await Promise.all(ids.map((id) => api.forgeTask(id).catch(() => null)))
      if (deadRef.current) return          // ★ await 期间可能已卸载

      const { running, finished, gone } = reduceSnapshots(ids, snaps)
      // 先写回再弹窗：反过来的话，弹窗里的操作会在localStorage
      // 还没清掉时又触发一轮归约。
      const left = settleIds(readIds(), gone, finished)
      if (gone.length || finished.length) writeIds(left)
      for (const t of finished) finishOne(t)
      setBgTasks(running)
      if (shouldStopWatching(running.length, left.length)) stopTaskTimer()
    }, POLL_MS)
  }, [finishOne, readIds, stopTaskTimer, writeIds])

  // 挂载时接管全部未完成任务（用户曾离开再回来）
  useEffect(() => {
    if (readIds().length) startWatchAll()
    return stopTaskTimer
  }, [startWatchAll, stopTaskTimer, readIds])

  // ── 提交一次提炼 ───────────────────────────────────────
  const doDraft = useCallback(async (payload) => {
    if (submitLock.current) return           // 同步守卫：只防提交动作本身的双击
    if (!hasAnyInput(payload)) {
      flash?.(EMPTY_INPUT_HINT)
      return
    }
    submitLock.current = true
    setSubmitting(true)
    try {
      const r = await api.forgeDraftAsync({
        theory: payload.theory,
        image_urls: (payload.images || []).map((i) => i.url),
        user_notes: payload.notes,
        style_prompt: payload.stylePrompt,
        name: payload.name,
      })
      writeIds([...readIds(), r.task_id])
      flash?.(submitFlash(readIds().length))
      startWatchAll()
    } catch (e) {
      flash?.(e.message || '提交失败')
    } finally {
      submitLock.current = false
      setSubmitting(false)
    }
  }, [flash, readIds, startWatchAll, writeIds])

  // ── 中止（协作式：正在跑的模型调用结束后才真正终止）──────
  const doCancel = useCallback(async (t) => {
    try {
      await api.forgeCancel(t.task_id)
      flash?.(CANCEL_PENDING_FLASH)
      setBgTasks((prev) => prev.map((x) => (x.task_id === t.task_id
        ? { ...x, cancelling: true, phase: CANCEL_PENDING_PHASE } : x)))
    } catch (e) {
      flash?.(e.message || '中止失败')
    }
  }, [flash])

  const onReviseClick = useCallback(() => {
    const blocked = reviseBlockedReason({ current, feedback })
    if (blocked) return flash?.(blocked)
    setReviseConfirm(true)
  }, [current, feedback, flash])

  // 「迭代一版」：重跑整条 LLM 链，4-6 分钟。★ 跟后台提炼任务是两套东西，
  // 所以用 revising，不碰 bgTasks。
  const doRevise = useCallback(async () => {
    setReviseConfirm(false)
    const blocked = reviseBlockedReason({ current, feedback })
    if (blocked) return flash?.(blocked)
    if (!(feedback || '').trim()) return
    setRevising(true)
    try {
      // ★ 左侧参考图区若贴了新图（如理想效果图），本轮迭代会对新图做视觉识别
      const r = await api.forgeRevise(
        current.id, feedback, (images || []).map((i) => i.url))
      if (!r.ok) {
        return flash?.('这一版没通过校验，上一版仍然保留 —— 换个说法再试试')
      }
      onReviseDone?.(r)
      setFeedback('')
      setPromptEditing?.(false)
      flash?.(`已更新到第 ${r.version} 版`
        + ((images || []).length > 0
          ? `（已按你贴的 ${images.length} 张新参考图做视觉识别）` : ''))
      onRefreshLibrary?.()
    } catch (e) {
      flash?.(e.message || '迭代失败')
    } finally {
      setRevising(false)
    }
  }, [current, feedback, flash, images, onReviseDone, onRefreshLibrary,
    setFeedback, setPromptEditing])

  return {
    bgTasks, submitting, reviseConfirm, setReviseConfirm, revising,
    donePopupQueue, setDonePopupQueue,
    doDraft, doCancel, onReviseClick, doRevise,
  }
}
