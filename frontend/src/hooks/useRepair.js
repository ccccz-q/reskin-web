import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import {
  nextRepairNote, toggleCat, withGuessedCat, repairBlockedReason,
  popUndoEntry, pushUndoEntry, pickDrifts, nextResultAfterRepair,
} from '../lib/repair'

const THREAD = 'studio'

/**
 * 局部修复域：面板状态、秒表、AI 找问题、修复提交、版本回退。
 *
 * ★ 修复是「外科手术」不是「重新生成」：参考图 = **刚生成的那张成品**，
 *   提示词 = 纯指令（只改点名的几处），并把家族硬禁令与用户自定义提示词
 *   重申进去 —— 修复不许破坏用户定下的规矩。
 *
 * 纯逻辑（猜维度 / 上限 / 前置校验 / 版本栈）在 lib/repair.js，47 个用例锁着。
 * 本文件只管 state 落地与请求编排。
 */
export function useRepair(deps) {
  const {
    flash, refreshQuota, result, setResult, source, quota, busy,
    extraPrompt, setLatest, undoStack, setUndoStack,
  } = deps

  const [repairOpen, setRepairOpen] = useState(false)
  const [repairCats, setRepairCats] = useState([])   // 要修的维度（可多选）
  const [repairNote, setRepairNote] = useState('')
  const [repairBusy, setRepairBusy] = useState(false)
  const [diagBusy, setDiagBusy] = useState(false)   // AI 找问题（VLM 对比原图与成品）
  // AI 找问题给出的候选，渲染成可点选的卡片 —— 用户挑一条再编辑，而不是被迫
  // 从一段粘贴过来的文本里自己挑重点（实测：直接塞进输入框会被当成最终稿提交）
  const [drifts, setDrifts] = useState([])
  // 修复耗时（秒）：出图是 40s~3min 的重活，给一个正在走的秒数，
  // 用户就不会怀疑「是不是卡死了」而反复点（每点一次都是一张真额度）
  const [repairElapsed, setRepairElapsed] = useState(0)
  // undoStack 由外部注入：它同时被「换原图」「新一轮出图」清空，
  // 属于跨域共享状态，放在 App 层才不会漏掉某一条清理路径。
  // 出图后的一次性引导（可关，关过就不再烦人）
  const [repairTipOff, setRepairTipOff] = useState(
    () => localStorage.getItem('travelogue.repairTipOff') === '1',
  )

  // 修复耗时秒表：出图是 40s~3min 的重活，走秒数是防止用户以为卡死而连点
  useEffect(() => {
    if (!repairBusy) return undefined
    const t = setInterval(() => setRepairElapsed((s) => s + 1), 1000)
    return () => clearInterval(t)
  }, [repairBusy])

  // Esc 关闭微调面板；修复进行中不关（关了用户以为成功了一半）
  useEffect(() => {
    if (!repairOpen) return undefined
    const onKey = (e) => {
      if (e.key !== 'Escape' || repairBusy) return
      setRepairOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [repairOpen, repairBusy])

  const onToggleCat = useCallback((c) => setRepairCats((cur) => toggleCat(cur, c)), [])

  // 点选一条候选 → **追加**为一行（再点一次取消），并顺手勾上猜出的维度。
  // 旧的"替换整个输入框"会吃掉用户已写的行，实测很恼火。
  const pickDrift = useCallback((d) => {
    if (!d) return
    const txt = String(d.change || '').trim()
    if (!txt) return
    // 用函数式更新而不是直接读 repairNote：同一次事件里连点两个候选时，
    // 要基于上一条的**结果**再算，否则第二行会覆盖第一行。
    setRepairNote((cur) => {
      const next = nextRepairNote(cur, txt)
      if (next === null) {
        flash('最多同时修 3 处 —— 先修最要紧的，修完可以再来一轮')
        return cur
      }
      return next
    })
    setRepairCats((cur) => withGuessedCat(cur, txt))
  }, [flash])

  const doRepair = useCallback(async () => {
    const blocked = repairBlockedReason({ busy, note: repairNote, quota, result })
    if (blocked) return flash(blocked)
    setRepairBusy(true)
    setRepairElapsed(0)
    try {
      const r = await api.repair(repairNote.trim(), result.url, THREAD, {
        familyId: result.family_id || '',
        extraPrompt: extraPrompt.trim(),
      })
      if (r?.ok) {
        // ★ 先把当前成品压栈：修坏了可以免费回到上一版
        setUndoStack((s) => pushUndoEntry(s, result))
        // 新图顶替旧图：连续修复（修完一处再修下一处）天然成立
        setResult(nextResultAfterRepair(r, result))
        setLatest({ url: r.image_url, filename: '修复后的成品' })
        setRepairOpen(false)
        setRepairNote('')
        setRepairCats([])
        setDrifts([])
        refreshQuota()          // ★ 修复也扣额度 —— 徽标必须跟着走，否则数字是假的
        flash('修复完成：只改了点名的地方，其余画面保持原样。不满意可以「回到上一版」')
      } else {
        flash(r?.note || '修复没有成功，请稍后再试')
      }
    } catch (e) {
      flash(e?.message || '修复没有成功，请稍后再试')
    } finally {
      setRepairBusy(false)
    }
  }, [busy, repairNote, quota, result, extraPrompt, flash, refreshQuota,
    setResult, setLatest])

  // 回到上一版：纯本地指针回退，不花额度、不用等
  const undoRepair = useCallback(() => {
    const out = popUndoEntry(undoStack)
    if (!out) return
    setUndoStack(out.stack)
    setResult(out.entry)
    flash('已回到上一版（不消耗额度）')
  }, [undoStack, setResult, flash])

  // AI 找问题（repair v1）：VLM 对比原图与成品，产出漂移候选供点选。
  // ★ 必须带上家族 id（2026-10-05 用户实测反馈）：否则模板刻意添加的元素
  //   （如趣味小人的涂鸦人物、手写文案）会被判成"建议修"，一修风格就没了。
  const doDiagnose = useCallback(async () => {
    if (diagBusy || repairBusy) return
    if (!source?.url || !result?.url) {
      return flash('AI 找问题需要原图和生成结果各一张')
    }
    setDiagBusy(true)
    try {
      const r = await api.diagnose(source.url, result.url, result.family_id || '')
      const list = pickDrifts(r)
      if (list) {
        setDrifts(list)
        flash('AI 找出了这些候选 —— 点选要修的（可多选），改改再修')
      } else {
        setDrifts([])
        flash(r?.error || 'AI 没发现明显问题，可以直接手写要修的地方')
      }
    } catch (e) {
      flash(e?.message || 'AI 找问题没有成功')
    } finally {
      setDiagBusy(false)
    }
  }, [diagBusy, repairBusy, source, result, flash])

  // 关掉一次性引导：写进 localStorage，刷新页面也不再来烦
  const dismissTip = useCallback(() => {
    setRepairTipOff(true)
    try { localStorage.setItem('travelogue.repairTipOff', '1') } catch { /* 隐私模式忽略 */ }
  }, [])

  return {
    repairOpen, setRepairOpen, repairCats, repairNote, setRepairNote,
    repairBusy, diagBusy, drifts, repairElapsed, undoStack, setUndoStack,
    repairTipOff, onToggleCat, pickDrift, doRepair, undoRepair, doDiagnose, dismissTip,
  }
}