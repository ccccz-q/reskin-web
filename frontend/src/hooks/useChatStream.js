import { useCallback, useMemo, useRef, useState } from 'react'
import { api, chatAsync, chatTask, chatTaskCancel } from '../api'
import {
  applyEvent, finalizeFromStatus, isTerminalStatus, sliceNewText,
} from '../lib/chatEvents'

const THREAD = 'studio'

/**
 * 后台任务式对话（取代 SSE 长连接）
 *
 * ★ 为什么不再用 SSE（2026-10-03 线上事故）：
 *   云端托管层对每个 HTTP 请求有 **60 秒硬超时**，超时直接 504。
 *   真实出图链路 = 对话+ 视觉模型 + 生图，实测 1~7 分钟，
 *   撑在一根 SSE 长连接里必然被掐断 —— 表现就是「卡住不动然后弹错」。
 *
 *   现在改成「提交 → 秒回 task_id → 轮询」：每个请求都是毫秒级，
 *   从原理上碰不到 60 秒那条线，且视觉模型照跑，质量一点不让。
 *
 * 事件映射的纯逻辑在 lib/chatEvents.js（65 个用例锁住重复/乱序/空事件），
 * 本文件只负责轮询节奏、状态落地与副作用。
 *
 * @param deps.aliveRef卸载哨兵：轮询循环与 finally 里的收尾都靠它，
 *        组件消失后既不 setState 也不去对账（否则 React 警告 + 白跑一次请求）
 * @param deps.lockedRef 用户锁定参数名集合 —— done 事件换家族时要清空，
 *        但它属于参数域，所以由外部持有，这里只调用 clearLocked
 */
export function useChatStream(deps) {
  const {
    aliveRef, flash, refreshQuota, source, extraPrompt, extraMode,
    familyId, setResult, setUndoStack, setFamilyId, setParams, lockedRef, setLatest,
  } = deps

  const [messages, setMessages] = useState([])
  const [trace, setTrace] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)

  const taskRef = useRef(null)          // 当前后台任务的 task_id（用于中止）
  const genToolOkRef = useRef(false)    // 本轮出图工具是否成功返回过
  const doneUrlRef = useRef(null)       // done 事件是否带来了 image_url

  // ★ 为什么事件映射要读 ref 而不是 state：
  //   runStream 在提交时抓一个 handleEvent 引用，然后握着它轮询几秒。
  //   如果映射靠闭包里的 trace/messages，同一轮里连续到达的多个 token
  //   看到的都是**同一个旧快照** —— 第二个 token 会覆盖第一个，
  //   流式文本只剩最后一个片段。所以这里用 ref 同步跟着补丁走，
  //   语义等价于原来那个函数式 setState（每次都读到最新值）。
  const flowRef = useRef({ trace: [], messages: [] })

  // ── SSE/轮询事件 → UI 状态 ──
  const handleEvent = useCallback((event, data) => {
    if (!aliveRef.current) return
    const patch = applyEvent(
      {
        trace: flowRef.current.trace,
        messages: flowRef.current.messages,
        // result / undoStack 在纯映射里是「只写不读」——
        // image/done 事件只发出 patch.undoStack = []，从不读旧值，
        // 所以这里传占位即可（真实值由 setResult/setUndoStack 落地）。
        result: null,
        undoStack: null,
        genToolOk: genToolOkRef.current,
        doneUrl: doneUrlRef.current,
      },
      event,
      data,
      { familyId },
    )
    if (patch.genToolOk !== undefined) genToolOkRef.current = patch.genToolOk
    if (patch.doneUrl !== undefined) doneUrlRef.current = patch.doneUrl
    if (patch.trace !== undefined) { flowRef.current.trace = patch.trace; setTrace(patch.trace) }
    if (patch.messages !== undefined) { flowRef.current.messages = patch.messages; setMessages(patch.messages) }
    if (patch.result !== undefined) setResult(patch.result)
    if (patch.undoStack !== undefined) setUndoStack(patch.undoStack)
    if (patch.familyId !== undefined) setFamilyId(patch.familyId)
    if (patch.params !== undefined) setParams(patch.params)
    if (patch.clearLocked) lockedRef.current = new Set()   // agent 切家族：旧锁定参数名泄漏到新家族会误跳兜底
    for (const fx of patch.effects || []) {
      if (fx.type === 'flash') flash(fx.msg)
      else if (fx.type === 'quota') refreshQuota()
      else if (fx.type === 'scroll') {
        setTimeout(() => {
          document.getElementById('h-canvas')?.scrollIntoView({
            behavior: 'smooth', block: 'start',
          })
        }, 120)
      }
    }
  }, [familyId, setResult, setUndoStack, setFamilyId, setParams,
    lockedRef, flash, refreshQuota])

  const runStream = useCallback(async (text) => {
    if (busy) return
    setBusy(true)
    setTrace([])
    // ref 必须跟 state 一起重置：新一轮的 token 要从空轨迹接上，
    // 否则会拼到上一轮留下的行上。
    flowRef.current.trace = []
    genToolOkRef.current = false
    doneUrlRef.current = null
    setMessages((m) => [...m, { role: 'user', text }])

    let taskId = null
    let aborted = false
    try {
      // ① 提交：这一步是毫秒级的，永远不会被网关掐
      const sub = await chatAsync({
        message: text,
        threadId: THREAD,
        imageUrl: source?.url,
        allowSpend: true,
        extraPrompt,
        extraMode,
      })
      taskId = sub?.task_id
      if (!taskId) throw new Error('没有拿到任务号，请再试一次')
      taskRef.current = taskId

      // ② 轮询：按 cursor 增量取事件，文本按已渲染长度切片
      let cursor = 0
      let renderedLen = 0
      let snap = null
      for (;;) {
        if (!aliveRef.current) return          // 组件已卸载
        await new Promise((r) => setTimeout(r, 900))
        snap = await chatTask(taskId, cursor)

        // 流式文本：后端给累积全文，这里只把新增的一段喂给事件处理器
        const delta = sliceNewText(snap.text, renderedLen)
        if (delta !== null) {
          handleEvent('token', { text: delta })
          renderedLen = snap.text.length
        }
        for (const ev of snap.events || []) handleEvent(ev.event, ev.data)
        // ★ cursor 只在成功取到这一轮快照之后才推进。
        //   上一行 chatTask 抛错的话根本走不到这里 —— 下一轮仍从旧 cursor
        //   重取，宁可重复收一遍事件，也不能跳过没读的那段。
        cursor = snap.cursor || 0

        if (isTerminalStatus(snap.status)) break
      }

      // ③ 收尾：终态里补上 SSE 时代由最后一个事件承担的动作
      handleEvent('finish', {})
      const fin = finalizeFromStatus(snap.status)
      if (fin.kind === 'aborted') { aborted = true; flash('已中止这次任务') }
      else if (fin.kind === 'failed') flash(snap.error || '这次没有跑完，请再试一次')
      else handleEvent('done', snap.result || {})

      const final = snap.result || {}
      if (final?.image_url) {
        setLatest({ url: final.image_url, filename: '刚生成' })
        refreshQuota()
      }
    } catch (e) {
      flash(e?.message || '操作没有成功，请稍后再试')
    } finally {
      if (aliveRef.current) {
        setBusy(false)
        setMessages((m) => m.map((x) => (x.streaming ? { ...x, streaming: false } : x)))
      }
      taskRef.current = null
      // ★ 兜底对账：出图工具成功过、但 done 始终没送来 image_url
      //   （长连接被代理/网络中途掐断的典型症状）。
      //   后端日志证实这种情况下图已落盘成功 —— 那就去作品仓库把最新一张
      //   生成图直接摆上画布，用户不会白等几分钟。
      if (aliveRef.current && !aborted && genToolOkRef.current && !doneUrlRef.current) {
        try {
          const g = await api.gallery(1, 'generated', true)   // force：绕过 TTL，必须看到刚落盘的图
          const it = g?.items?.[0]
          if (it?.url) {
            setResult({ url: it.url, family_id: familyId })
            // 兜底出图同样滚回画布（图已经落盘成功，用户不该自己去翻仓库）
            setTimeout(() => {
              document.getElementById('h-canvas')?.scrollIntoView({
                behavior: 'smooth', block: 'start',
              })
            }, 120)
            setLatest({ url: it.url, filename: it.filename || '刚生成' })
            refreshQuota()
          }
        } catch { /* 对账失败就静默，不打扰用户 */ }
      }
    }
  }, [busy, handleEvent, flash, source, extraPrompt, extraMode, familyId,
    refreshQuota, aliveRef, setResult, setLatest])

  const sendChat = useCallback(() => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    runStream(text)
  }, [input, busy, runStream])

  /** 中止后台任务（服务端协作式取消：当前这一步跑完就收尾） */
  const abortTask = useCallback(async () => {
    const tid = taskRef.current
    taskRef.current = null
    if (tid) {
      try { await chatTaskCancel(tid) } catch { /* 已结束的任务忽略 */ }
    }
    flash('已请求终止 —— 正在进行的模型调用会跑完这一步，随后引擎立即收尾')
  }, [flash])

  // 抽屉标签上那一行预览：最近一条有内容的 assistant 回复
  const lastLine = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant' && messages[i].text.trim()) return messages[i].text
    }
    return ''
  }, [messages])

  return {
    messages, setMessages, trace, input, setInput, busy, chatOpen, setChatOpen,
    runStream, sendChat, abortTask, lastLine,
  }
}