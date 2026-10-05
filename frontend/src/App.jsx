import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, imgSrc, chatAsync, chatTask, chatTaskCancel } from './api'
import ParamForm from './ParamForm'
import Workshop from './Workshop'
import GalleryModal from './GalleryModal'
import HelperModal from './HelperModal'
import HeroRing from './HeroRing'
import SettingsModal, { loadSettings, applySettings } from './SettingsModal'
import './App.css'

const THREAD = 'studio'

export default function App() {
  const [booting, setBooting] = useState(true)
  const [health, setHealth] = useState(null)
  const [families, setFamilies] = useState([])
  const [familyId, setFamilyId] = useState('')
  // ★ 随仓库内置的家族（不可删除）——与后端 BUILTIN_FAMILY_IDS 保持一致
  const BUILTIN_FAMILY_IDS = new Set([
    'doodle_narrators', 'epic_silhouette', 'full_restyle', 'material_pixel',
    'risograph_travel_print', 'second_world', 'split_poster',
    'surreal_collage', 'zine',
  ])
  const [delArm, setDelArm] = useState('')        // 两段式删除确认：当前已武装的家族 id
  const delArmTimer = useRef(null)
  const doDeleteFamily = async (f) => {
    try {
      const r = await api.familiesDelete(f.id)
      flash?.(`已删除家族「${f.name}」` +
        (r.library_unmarked > 0 ? `（库中 ${r.library_unmarked} 条安装标记已同步复位）` : ''))
      const fl = await api.families(true)
      setFamilies(fl.items || [])
      setFamilyId((cur) => (cur === f.id ? '' : cur))
    } catch (e) {
      flash?.(e.message || '删除失败')
    }
  }
  const [params, setParams] = useState({})
  const [rendered, setRendered] = useState(null)
  const [renderErr, setRenderErr] = useState('')

  const [source, setSource] = useState(null)   // {url,width,height,format,filename}
  const [result, setResult] = useState(null)   // {url,family_id,size}
  const [lightbox, setLightbox] = useState(null)   // {url, dim} —— 示例图/画布/仓库共用
  const [repairOpen, setRepairOpen] = useState(false)
  // ★ 后台提炼的全局提醒：工坊里发起提炼后，用户去任何页面都能收到"提炼好了"的弹窗
  const [forgeNotice, setForgeNotice] = useState(null)
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
  // ★ 版本栈：每次成功修复前把当前成品压栈。修复不满意可「回到上一版」，
  //   既不花额度也不等 —— 没有它，用户一旦修坏就得从头再出一张。
  const [undoStack, setUndoStack] = useState([])
  // 出图后的一次性引导（可关，关过就不再烦人）
  const [repairTipOff, setRepairTipOff] = useState(
    () => localStorage.getItem('travelogue.repairTipOff') === '1',
  )
  const [downloading, setDownloading] = useState(false)
  // ★ 微调重出（批次三：CHANGE ONLY / PRESERVE EXACTLY 修复链路）
  //   出图后对某个维度不满意 → 构造外科手术式修复指令，只动被点名的维度。
  //   走既有 extra_prompt(append) 通道，不需要后端新接口。
  // 用户自定义提示词。原样进最终 prompt，不会被模型转述或改写。
  const [extraPrompt, setExtraPrompt] = useState('')
  const [extraMode, setExtraMode] = useState('append')   // append | replace
  const [workshopOpen, setWorkshopOpen] = useState(false)
  const [galleryOpen, setGalleryOpen] = useState(false)
  const [helperOpen, setHelperOpen] = useState(false)     // 🧭 小助手：讲解 + 看图推荐
  // 刚生成的图 → 交给环绕轮播定位亮相
  const [latest, setLatest] = useState(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settings, setSettings] = useState(() => loadSettings())
  const [recent, setRecent] = useState([])   // 最近生成（右栏缩略条）
  // ★ USER-LOCKED：用户在 UI 里显式动过的参数名。
  //   这些参数渲染时跳过默认值/auto 兜底 —— 用户的选择 > 系统的好意。
  const lockedRef = useRef(new Set())
  const [chatOpen, setChatOpen] = useState(false)

  const [messages, setMessages] = useState([])
  const [trace, setTrace] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')

  const abortRef = useRef(null)
  const taskRef = useRef(null)    // 当前后台对话任务的 task_id（用于中止）
  const renderTimer = useRef(null)
  const dropRef = useRef(null)
  const fileRef = useRef(null)
  const aliveRef = useRef(true)
  // ★ done 丢失对账（实测场景：出图 3-5 分钟的长 SSE 连接偶发被中途掐断——
  //   代理/网络抖动都会干这个。此时后端 worker 照样跑完、图已落盘，
  //   但前端收不到 done 事件 → 画布永远不出图，只有 Agent 轨迹里那句「出图 ✓」。
  //   兜底：记录「出图工具成功过」与「done 是否到过」，流结束后对账一次。）
  const genToolOkRef = useRef(false)   // 本轮出图工具是否成功返回过
  const doneUrlRef = useRef(null)      // done 事件是否带来了 image_url

  const family = useMemo(
    () => families.find((f) => f.id === familyId) || null,
    [families, familyId],
  )
  const quota = health?.governance?.quota
  const ready = health?.ready || {}

  // 应用用户设置（主题/背景/作品流来源）
  useEffect(() => { applySettings(settings) }, [settings])   // eslint-disable-line

  // 后台提炼哨兵：每 4s 看一眼 localStorage 里有没有未读完成的后台任务。
  // 工坊页自己也在看（有人看着就不弹窗）——这里兜住"用户已经离开工坊"的情况。
  // ★ 支持多任务并行：forgeTaskIds 是数组，完成的逐个弹（一轮弹一个，下轮接着弹）。
  useEffect(() => {
    let dead = false
    const readIds = () => {
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
    }
    const check = async () => {
      const ids = readIds()
      if (!ids.length) return
      for (let i = 0; i < ids.length; i++) {
        try {
          const t = await api.forgeTask(ids[i])
          if (dead) return
          if (t.status === 'done' || t.status === 'failed' || t.status === 'cancelled') {
            writeIdsLocal(ids.filter((x) => x !== ids[i]))
            setForgeNotice(t)      // 一次弹一个；同轮还有别的完成项，下轮轮询接着弹
            return
          }
        } catch {
          // 404 = 服务重启丢了任务注册表，清掉别永远轮询
          if (dead) return
          writeIdsLocal(readIds().filter((x) => x !== ids[i]))
        }
      }
    }
    const writeIdsLocal = (ids) => localStorage.setItem('forgeTaskIds', JSON.stringify(ids))
    const timer = setInterval(check, 4000)
    return () => { dead = true; clearInterval(timer) }
  }, [])

  // 最近生成：出图后刷新，让右栏缩略条跟上
  useEffect(() => {
    if (!result?.url) return
    let alive = true
    ;(async () => {
      try {
        const r = await api.gallery(12, 'generated')
        if (alive) setRecent((r.items || []).slice(0, 8))
      } catch { /* 非关键路径 */ }
    })()
    return () => { alive = false }
  }, [result?.url])   // eslint-disable-line

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

  // 卸载时：标记不再 setState。
  // ★ 后台任务**不随组件卸载而取消** —— 它跑在服务端，用户刷新或换页回来还能接着看；
  //   这是异步化相对 SSE 的一个额外好处（SSE 一断连，worker 就被通知收尾了）。
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const flash = useCallback((msg) => {
    if (!aliveRef.current) return
    setToast(msg)
    setTimeout(() => setToast((cur) => (cur === msg ? '' : cur)), 3200)
  }, [])

  // 家族清单单独抽出来：模板工坊「安装」之后要立刻刷新列表，
  // 否则用户装完了还得手动刷新页面才看得到新风格。
  const reloadFamilies = useCallback(async () => {
    try {
      const f = await api.families(true)
      if (!aliveRef.current) return
      setFamilies(f.items || [])
      return f.items || []
    } catch (e) {
      flash(`家族列表刷新失败：${e.message}`)
      return []
    }
  }, [flash])

  // ── 启动：健康检查 + 家族清单
  useEffect(() => {
    ;(async () => {
      try {
        const [h, f] = await Promise.all([api.health(THREAD), api.families(true)])
        if (!aliveRef.current) return
        setHealth(h)
        setFamilies(f.items || [])
        if (f.items?.length) setFamilyId(f.items[0].id)
      } catch (e) {
        flash(`服务暂时连不上（${e.message}）—— 请稍后刷新页面再试`)
      } finally {
        if (aliveRef.current) setBooting(false)
      }
    })()
    // ★ 作品仓库预热：启动 2s 后静默拉一次画廊列表。
    //   服务端收到请求后会后台预生成缩略图（fire-and-forget）——
    //   等用户真正点开仓库时，缩略图已在磁盘缓存里，秒开。
    const warm = setTimeout(() => { api.gallery(1, '', true).catch(() => {}) }, 2000)
    return () => clearTimeout(warm)
  }, [flash])

  // ── 参数变化 → 重新渲染提示词（0 成本，毫秒级）
  useEffect(() => {
    if (!familyId) return
    clearTimeout(renderTimer.current)
    renderTimer.current = setTimeout(async () => {
      try {
        const r = await api.render(familyId, params, { extraPrompt, extraMode, locked: [...lockedRef.current] })
        if (!aliveRef.current) return
        setRendered(r)
        setRenderErr('')
      } catch (e) {
        if (!aliveRef.current) return
        setRendered(null)
        setRenderErr(e.detail?.missing?.length
          ? `缺必填参数：${e.detail.missing.join('、')}`
          : e.message)
      }
    }, 220)
    return () => clearTimeout(renderTimer.current)
  }, [familyId, params, extraPrompt, extraMode])

  const refreshQuota = useCallback(() => {
    api.policy(THREAD)
      .then((p) => {
        if (aliveRef.current) {
          setHealth((h) => (h ? { ...h, governance: { ...h.governance, ...p } } : h))
        }
      })
      .catch(() => {})
  }, [])

  const pickFamily = (id) => {
    setFamilyId(id)
    setParams({})          // 换家族不继承旧参数，避免把非法枚举值带过去
    lockedRef.current = new Set()   // 锁定集也随家族清空（参数名空间不同）
    setRendered(null)
  }

  // ★ 风格示例卡片：每个家族独立开合 —— 点开下一个不收上一个，
  //   只有各自的「收起」按钮才收起（这是用户明确要求的交互）。
  const [exOpen, setExOpen] = useState(() => new Set())
  const openExample = useCallback((id) => {
    setExOpen((prev) => {
      if (prev.has(id)) return prev
      const next = new Set(prev)
      next.add(id)
      return next
    })
  }, [])
  const closeExample = useCallback((id) => {
    setExOpen((prev) => {
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [])

  // ── 上传（拖拽 / 点选）
  const doUpload = useCallback(async (file) => {
    if (!file) return
    try {
      const saved = await api.upload(file)
      if (!aliveRef.current) return
      setSource(saved)
      setResult(null)
      flash(`已上传 ${saved.filename}（${saved.width}×${saved.height}）`)
    } catch (e) {
      flash(e.message)
    }
  }, [flash])

  const clearSource = useCallback(() => {
    setSource(null)
    setResult(null)
    setUndoStack([])
    if (fileRef.current) fileRef.current.value = ''
  }, [])

  useEffect(() => {
    const el = dropRef.current
    if (!el) return
    const stop = (e) => { e.preventDefault(); e.stopPropagation() }
    const over = (e) => { stop(e); el.classList.add('dropping') }
    const out = (e) => { stop(e); el.classList.remove('dropping') }
    const drop = (e) => { stop(e); el.classList.remove('dropping'); doUpload(e.dataTransfer?.files?.[0]) }
    el.addEventListener('dragover', over)
    el.addEventListener('dragleave', out)
    el.addEventListener('drop', drop)
    return () => {
      el.removeEventListener('dragover', over)
      el.removeEventListener('dragleave', out)
      el.removeEventListener('drop', drop)
    }
  }, [doUpload])

  // ── 全局粘贴上传：截图后直接 Ctrl+V，原图自动进画布。
  // 只认「图片文件」类型的粘贴项 —— 在输入框里粘贴纯文本完全不受影响。
  // 工坊打开时让位：粘贴的图进工坊参考图区（那边有自己的监听）。
  useEffect(() => {
    const onPaste = (e) => {
      if (workshopOpen) return
      const items = e.clipboardData?.items
      if (!items) return
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const f = it.getAsFile()
          if (f) {
            e.preventDefault()
            doUpload(f)
          }
          break
        }
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [doUpload, workshopOpen])

  // ── SSE 事件处理器（两个入口共用，避免两套逻辑走偏）
  const handleEvent = useCallback((event, data) => {
    if (!aliveRef.current) return
    if (event === 'tool_start') {
      setTrace((t) => [...t, { name: data.name, state: 'running' }])
    } else if (event === 'tool_end') {
      if (data.ok && data.name === 'generate_image') genToolOkRef.current = true
      setTrace((t) => {
        const next = [...t]
        const i = next.findIndex((x) => x.name === data.name && x.state === 'running')
        const row = { name: data.name, state: data.ok ? 'done' : 'fail', summary: data.summary }
        if (i >= 0) next[i] = row
        else next.push(row)
        return next
      })
    } else if (event === 'token' && data.text) {
      setMessages((m) => {
        const last = m[m.length - 1]
        if (last?.role === 'assistant' && last.streaming) {
          const copy = [...m]
          copy[copy.length - 1] = { ...last, text: last.text + data.text }
          return copy
        }
        return [...m, { role: 'assistant', text: data.text, streaming: true }]
      })
    } else if (event === 'finish') {
      setMessages((m) => m.map((x) => (x.streaming ? { ...x, streaming: false } : x)))
    } else if (event === 'image') {
      // ★ 图一落盘就到了（不必等收尾 LLM 讲完）—— 先上画布，文案继续跑。
      //   与 done 分支共用同一段渲染逻辑：图片先到、结果不重画、滚动只发生一次。
      doneUrlRef.current = data.url || doneUrlRef.current
      setResult({ url: data.url, family_id: data.family_id, size: data.size })
      setUndoStack([])      // ★ 全新一张图的开始，旧版本的回退栈不再有意义
      if (data.aspect_warning) flash(data.aspect_warning)
      setTimeout(() => {
        document.getElementById('h-canvas')?.scrollIntoView({
          behavior: 'smooth', block: 'start',
        })
      }, 120)
    } else if (event === 'done') {
      doneUrlRef.current = data.image_url || null
      if (data.image_url) {
        setUndoStack([])    // ★ 同上：新一轮出图，回退栈清零
        setResult({ url: data.image_url, family_id: data.family_id, size: data.size })
        // ★ 生成完成自动回到画布（实测用户诉求）：生成中用户往下看轨迹，
        //   完成后页面停在底部 → 生成图看不全。滚回画布标题，一屏内
        //   正好是「标题 + 完整成品图 + 工具栏」。
        setTimeout(() => {
          document.getElementById('h-canvas')?.scrollIntoView({
            behavior: 'smooth', block: 'start',
          })
        }, 120)
      }
      if (data.aspect_warning) flash(data.aspect_warning)   // 画幅偏差提示（最后一道闸）
      if (data.family_id && data.family_id !== familyId) {
        setFamilyId(data.family_id)
        setParams(data.params || {})
        lockedRef.current = new Set()   // agent 切家族：旧锁定参数名泄漏到新家族会误跳兜底
      }
      if (data.error) flash(data.error)
      refreshQuota()
    } else if (event === 'error') {
      flash(data.error || '对话出错')
    }
  }, [familyId, flash, refreshQuota])

  /* ══════════════════ 后台任务式对话（取代 SSE 长连接）══════════════════
   *
   * ★ 为什么不再用 SSE（2026-10-03 线上事故）：
   *   云端托管层对每个 HTTP 请求有 **60 秒硬超时**，超时直接 504。
   *   真实出图链路 = 对话 + 视觉模型 + 生图，实测 1~7 分钟，
   *   撑在一根 SSE 长连接里必然被掐断 —— 表现就是「卡住不动然后弹错」。
   *
   *   现在改成「提交 → 秒回 task_id → 轮询」：每个请求都是毫秒级，
   *   从原理上碰不到 60 秒那条线，且视觉模型照跑，质量一点不让。
   *
   *   handleEvent 是原来 SSE 的处理器，这里原样复用 —— 异步版把同样的事件
   *   （tool_start / tool_end / finish）攒在任务里，轮询取回来按序喂给它。
   */
  const runStream = useCallback(async (text) => {
    if (busy) return
    setBusy(true)
    setTrace([])
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
        if (snap.text && snap.text.length > renderedLen) {
          handleEvent('token', { text: snap.text.slice(renderedLen) })
          renderedLen = snap.text.length
        }
        for (const ev of snap.events || []) handleEvent(ev.event, ev.data)
        cursor = snap.cursor || 0

        if (snap.status !== 'running' && snap.status !== 'queued') break
      }

      // ③ 收尾：终态里补上 SSE 时代由最后一个事件承担的动作
      handleEvent('finish', {})
      if (snap.status === 'cancelled') { aborted = true; flash('已中止这次任务') }
      else if (snap.status === 'failed') flash(snap.error || '这次没有跑完，请再试一次')
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
      //   （长 SSE 连接被代理/网络中途掐断的典型症状）。
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
  }, [busy, handleEvent, flash, source, extraPrompt, extraMode, familyId, refreshQuota])

  // 下载成品。同源（走 vite 代理）所以 fetch 不涉及 CORS。
  // 用 blob 而不是直接 <a href>：后者在某些浏览器会导航而不是保存。
  const downloadResult = useCallback(async () => {
    if (!result?.url) return
    setDownloading(true)
    try {
      const resp = await fetch(imgSrc(result.url))
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      const ext = (blob.type.split('/')[1] || 'jpg').replace('jpeg', 'jpg')
      a.download = `travelnote-${new Date().toISOString().slice(0, 10)}.${ext}`
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
  }, [result, flash])

  // 点击波纹（showcase: button-ripple）：以点击坐标为圆心扩散
  const ripple = useCallback((e) => {
    const el = e.currentTarget
    const r = el.getBoundingClientRect()
    const d = Math.max(r.width, r.height) * 1.1
    const s = document.createElement('span')
    s.className = 'ripple'
    s.style.width = s.style.height = `${d}px`
    s.style.left = `${e.clientX - r.left - d / 2}px`
    s.style.top = `${e.clientY - r.top - d / 2}px`
    el.appendChild(s)
    setTimeout(() => s.remove(), 600)
  }, [])

  // ★ 从一句话修复描述猜维度 —— AI 找问题返回的是自然语言，让人再归类一次
  //   纯属折腾；猜错了也无害（用户可以自己改），猜对了就少点两下。
  const guessCat = (text) => {
    const t = text || ''
    const table = [
      ['构图与视角', ['构图', '视角', '角度', '朝向', '转向', '位置']],
      ['光线层级', ['光', '阴影', '亮度', '曝光过度', '打光']],
      ['色彩与曝光', ['色', '曝光', '饱和度', '色调', '对比度', '偏色']],
      ['材质与质感', ['材质', '质感', '纹理', '笔触', '颗粒', '浮雕', '体素', '球']],
      ['人物姿态', ['人', '姿态', '动作', '手势', '脸', '五官', '手指']],
      ['商业化感', ['商业', '广告', '按钮', '水印', '文字', 'UI', 'logo', '文案']],
    ]
    for (const [cat, words] of table) {
      if (words.some((w) => t.includes(w))) return cat
    }
    return '其他'
  }

  // ★ 维度是多选（2026-10-05 用户反馈）：勾几个维度、写几行修哪几处，
  //   后端会把每一行编号成一条外科指令（上限 3 条，改动再多就不可控了）。
  const toggleCat = (c) => {
    setRepairCats((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]))
  }

  // 点选一条候选 → **追加**为一行（再点一次取消），并顺手勾上猜出的维度。
  // 旧的"替换整个输入框"会吃掉用户已写的行，实测很恼火。
  const pickDrift = (d) => {
    if (!d) return
    const txt = String(d.change || '').trim()
    if (!txt) return
    setRepairNote((cur) => {
      const lines = cur.split('\n').map((s) => s.trim()).filter(Boolean)
      if (lines.includes(txt)) return lines.filter((s) => s !== txt).join('\n')
      if (lines.length >= 3) {
        flash('最多同时修 3 处 —— 先修最要紧的，修完可以再来一轮')
        return cur
      }
      return [...lines, txt].join('\n')
    })
    setRepairCats((cur) => {
      const c = guessCat(txt)
      return cur.includes(c) ? cur : [...cur, c]
    })
  }

  const doRepair = async () => {
    if (busy) { return flash('正在进行的任务结束后再微调') }
    if (!repairNote.trim()) {
      return flash('先写清哪里不对（可勾选维度 + 点选 AI 候选）')
    }
    if (quota?.exhausted) {
      return flash('本会话额度已用完')
    }
    // ★ 外科修复（2026-10-05 重构）：旧机制是把修复词注入 extra_prompt 后
    //   以用户原图为参考整体重出 —— 那是"重新生成"，改一处动全身。
    //   新机制走 /api/image/repair：参考图 = **刚生成的那张成品**，
    //   提示词 = 纯外科指令（只改点名的几处），并把家族硬禁令与用户
    //   自定义提示词重申进去 —— 修复不许破坏用户定下的规矩。
    if (!result?.url) {
      return flash('先出一张图，才能对它做局部修复')
    }
    setRepairBusy(true)
    setRepairElapsed(0)
    try {
      const r = await api.repair(repairNote.trim(), result.url, THREAD, {
        familyId: result.family_id || '',
        extraPrompt: extraPrompt.trim(),
      })
      if (r?.ok) {
        // ★ 先把当前成品压栈：修坏了可以免费回到上一版
        setUndoStack((s) => [...s, { url: result.url, size: result.size, family_id: result.family_id }])
        // 新图顶替旧图：连续修复（修完一处再修下一处）天然成立
        const nextResult = { url: r.image_url, family_id: result.family_id, size: r.size }
        setResult(nextResult)
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
  }

  // 回到上一版：纯本地指针回退，不花额度、不用等
  const undoRepair = () => {
    const prev = undoStack[undoStack.length - 1]
    if (!prev) return
    setUndoStack((s) => s.slice(0, -1))
    setResult(prev)
    flash('已回到上一版（不消耗额度）')
  }

  // AI 找问题（repair v1）：VLM 对比原图与成品，产出漂移候选供点选。
  // ★ 必须带上家族 id（2026-10-05 用户实测反馈）：否则模板刻意添加的元素
  //   （如趣味小人的涂鸦人物、手写文案）会被判成"建议修"，一修风格就没了。
  const doDiagnose = async () => {
    if (diagBusy || repairBusy) return
    if (!source?.url || !result?.url) {
      return flash('AI 找问题需要原图和生成结果各一张')
    }
    setDiagBusy(true)
    try {
      const r = await api.diagnose(source.url, result.url, result.family_id || '')
      if (r?.ok && (r.drifts || []).length) {
        const list = r.drifts.slice(0, 3)
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
  }

  const generate = () => {
    if (!source) return flash('先上传一张原图')
    if (!rendered) return flash('提示词还没渲染好')
    if (quota?.exhausted) return flash('本会话额度已用完')
    const tail = extraPrompt.trim()
      ? '另外，我已填写了自定义提示词，它已经合进提示词里了，出图时请一并遵守。'
      : ''
    runStream(
      `请用 ${familyId} 家族渲染并出图，参数为 ${JSON.stringify(params)}。${tail}`,
    )
  }

  const sendChat = () => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    runStream(text)
  }

  const orientation = source
    ? source.height > source.width ? '竖构图' : source.width > source.height ? '横构图' : '方构图'
    : null

  // 为什么现在不能出图 —— 与其让按钮灰着不说话，不如把原因写在按钮旁边
  const blocked = !source
    ? '先上传一张原图'
    : !rendered
      ? '提示词还没渲染好'
      : quota?.exhausted
        ? '本会话额度已用完'
        : null
  const goHint = blocked || (busy
    ? '正在生成，看下面的轨迹…'
    : quota?.unlimited
      ? `将用「${family?.name || familyId}」出图`
      : `将用「${family?.name || familyId}」出图，消耗 1 张额度（剩 ${quota?.remaining ?? '—'} 张）`)

  const lastLine = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant' && messages[i].text.trim()) return messages[i].text
    }
    return ''
  }, [messages])

  return (
    <div className={`app ${chatOpen ? 'dock-open' : ''}`}>
      <a className="skip-link" href="#canvas">跳到画布</a>

      <header className="topbar">
        <div className="brand">
          <h1 className="brand-mark">换颜</h1>
          <p className="brand-sub">照片不动，换一种视觉身份</p>
        </div>
        <div className="badges" role="status" aria-live="polite">
          <span className={`badge ${ready.llm ? 'ok' : 'warn'}`}>LLM {ready.llm ? '就绪' : '未配置'}</span>
          <span className={`badge ${ready.generation ? 'ok' : 'warn'}`}>生图 {ready.generation ? '就绪' : '未配置'}</span>
          {quota && (
            <span className={`badge ${quota.exhausted ? 'bad' : 'ok'}`}>
              {quota.unlimited
                ? `额度 不限（已用 ${quota.used} 张）`
                : `额度 ${quota.remaining}/${quota.limit}`}
            </span>
          )}
        </div>
        <div className="topbar-actions">
          <button type="button" className="btn ghost ws-open hl-open"
                  onClick={() => setHelperOpen(true)}
                  title="功能讲解 · 上传照片让它推荐适合的风格">
            🧭 小助手
          </button>
          <button type="button" className="btn ghost ws-open" onClick={() => setGalleryOpen(true)}
                  title="历史生成图，按日期归档，可多选打包下载">
            🕘 作品仓库
          </button>
          <button type="button" className="btn ghost ws-open" onClick={() => setSettingsOpen(true)}
                  title="主题颜色 · 自定义背景 · 作品流来源">
            ⚙ 设置
          </button>
          <button type="button" className="btn ghost ws-open" onClick={() => setWorkshopOpen(true)}
                  title="用风格理论 + 参考图提炼一套新家族">
            模板工坊
          </button>
        </div>
      </header>

      {!ready.generation && !ready.llm && !booting && (
        <div className="banner">
          后端还没读到 API Key。把 <code>.env.example</code> 复制成 <code>.env</code> 并填入即可；
          预览和提示词渲染不依赖 Key，现在就能用。
        </div>
      )}

      <HeroRing latest={latest} source={settings.stripSource} />

      <main className="grid">
        {/* ══ 左 · 素材与风格（这一列只回答「用什么」）══════════════ */}
        <section className="col rail" aria-label="素材与风格">
          <h2 id="h-source" className="rail-title">原图</h2>

          {/* 拖拽仍在整块上生效，但「选文件」是真正的 <button>：
              div + onClick 键盘永远够不到，这是硬伤。 */}
          <div className={`dropzone ${source ? 'has' : 'empty'}`} ref={dropRef}>
            {source ? (
              <div className="src-card">
                <img className="src-thumb" src={imgSrc(source.url)}
                     alt={`已上传的原图 ${source.filename}`}
                     width={source.width} height={source.height} decoding="async" />
                <div className="src-info">
                  <span className="src-name" title={source.filename}>{source.filename}</span>
                  <span className="src-dims">
                    {source.width}×{source.height} · {orientation} · {source.format}
                  </span>
                </div>
              </div>
            ) : (
              <div className="dz-hint">
                <span className="dz-icon" aria-hidden="true">＋</span>
                <span className="dz-main">把照片拖到这里</span>
                <span className="dz-sub">JPG / PNG / WebP，≤20MB</span>
              </div>
            )}
            <div className="dz-actions">
              <button type="button" className="btn ghost dz-pick"
                      onClick={() => fileRef.current?.click()}>
                {source ? '更换' : '选择文件'}
              </button>
              {source && (
                <button type="button" className="btn ghost dz-clear" onClick={clearSource}>
                  移除
                </button>
              )}
            </div>
            <input ref={fileRef} id="file" type="file" accept="image/*" hidden tabIndex={-1}
                   onChange={(e) => doUpload(e.target.files?.[0])} />
          </div>

          <h2 id="h-family" className="rail-title">风格家族</h2>
          <div className="fam-list" role="group" aria-labelledby="h-family">
            {booting && <span className="muted">加载中…</span>}
            {!booting && families.length === 0 && <span className="muted">一个家族都没有</span>}
            {families.map((f) => (
              <div key={f.id} className="fam-slot">
                <button type="button"
                        className={`fam ${f.id === familyId ? 'on' : ''}`}
                        aria-pressed={f.id === familyId}
                        aria-expanded={exOpen.has(f.id)}
                        onClick={() => { pickFamily(f.id); openExample(f.id) }}>
                  <span className="fam-icon" aria-hidden="true">{f.icon || '◧'}</span>
                  <span className="fam-body">
                    <span className="fam-name">{f.name}</span>
                    <span className="fam-desc">{f.description}</span>
                  </span>
                  <span className={`fam-caret ${exOpen.has(f.id) ? 'open' : ''}`} aria-hidden="true">▾</span>
                </button>
                {/* 工坊安装的家族可从主页面删除（内置家族不显示按钮）。
                    两段式确认：第一击变「确认删除?」，再击执行，3.5 秒不点自动还原 */}
                {!BUILTIN_FAMILY_IDS.has(f.id) && (
                  <button type="button"
                          className={`fam-del ${delArm === f.id ? 'armed' : ''}`}
                          aria-label={`删除家族 ${f.name}`}
                          title={delArm === f.id ? '再点一次确认删除（会同时清理库里的安装标记）' : '删除这个家族'}
                          onClick={() => {
                            if (delArm === f.id) { doDeleteFamily(f); setDelArm('') }
                            else {
                              setDelArm(f.id)
                              clearTimeout(delArmTimer.current)
                              delArmTimer.current = setTimeout(() => setDelArm(''), 3500)
                            }
                          }}>
                    {delArm === f.id ? '确认删除?' : '×'}
                  </button>
                )}
                {/* 示例卡片：常挂载 + class 切换，展开/收起都有平滑动画 */}
                <div className={`fam-ex ${exOpen.has(f.id) ? 'open' : ''}`}
                     aria-hidden={!exOpen.has(f.id)}>
                  <div className="fam-ex-clip">
                    {f.example ? (
                      <figure className="fam-ex-card">
                        <img src={imgSrc(f.example)} alt={`「${f.name}」风格示例`}
                             loading="lazy" decoding="async"
                             onClick={() => setLightbox({ url: f.example })}
                             title="点击查看大图" />
                        <figcaption className="fam-ex-bar">
                          <span className="fam-example-tag">示例</span>
                          <span className="fam-ex-cap">{f.name} · 实际效果跟随你的原图</span>
                          <button type="button" className="fam-ex-hide"
                                  onClick={() => closeExample(f.id)}
                                  title="收起示例图">收起 ▴</button>
                        </figcaption>
                      </figure>
                    ) : (
                      <div className="fam-ex-bar">
                        <span className="muted">暂无示例图 —— 出一张满意的图后可设为该家族示例</span>
                        <button type="button" className="fam-ex-hide"
                                onClick={() => closeExample(f.id)}>收起 ▴</button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* ★ 风格示例：改为**每个家族按钮下独立展开**的卡片（可多开，各有关闭钮）。
             点击家族 = 选中并展开它的示例；点别的家族不影响已展开的。 */}
        </section>

        {/* ══ 中 · 画布与结算（这一列只回答「点下去会怎样」）════════ */}
        <section className="col canvas" id="canvas" aria-label="画布与出图">
          <h2 id="h-canvas" className="rail-title">
            画布
            {source && <span className="muted">{result ? '原图 / 成品' : '只有原图'}</span>}
          </h2>

          <div className="stage">
            {result ? (
              /* ★ 只展示成品。旧版的「原图/成品对比滑杆」已移除：
                 两个图层长宽比不一致时（横版原图 + 竖版海报），
                 滑杆比的不是同一个几何位置，对比没有意义，
                 还会让用户误以为「只生成了局部」。 */
              <img className="stage-img" src={imgSrc(result.url)} alt="生成的成品"
                   decoding="async" onClick={() => setLightbox({ url: result.url, dim: result.size })}
                   title="点击查看大图" />
            ) : source ? (
              <img className="stage-img stage-src" src={imgSrc(source.url)} alt="原图"
                   decoding="async" />
            ) : <p className="stage-empty">还没有原图 —— 先在左边放一张进来</p>}
          </div>

          {result && (
            <>
              <div className="toolbar">
                {result.size && <span className="tag">{result.size}</span>}
                <span className="muted">{family?.name || familyId}</span>
                {undoStack.length > 0 && (
                  <span className="tag tag-soft" title="这张是修复后的版本">
                    已修复 {undoStack.length} 次
                  </span>
                )}
                <span className="tb-spacer" />
                {undoStack.length > 0 && (
                  <button className="btn ghost tb-btn" onClick={undoRepair}
                          title="退回修复前的那一版 —— 立刻生效，不消耗额度">
                    ↩ 回到上一版
                  </button>
                )}
                <button className="btn ghost tb-btn" onClick={() => setLightbox({ url: result.url, dim: result.size })}>
                  🔍 查看大图
                </button>
                <button className={`btn tb-btn ${repairTipOff ? 'ghost' : 'hl-open'}`}
                        onClick={() => setRepairOpen(true)}
                        disabled={busy}
                        title={busy
                          ? '等这次生成结束再修复'
                          : '对刚生成的结果不满意时，只修你点名的一处，其余画面保持不变'}>
                  ✚ 局部修复
                </button>
                <button className="btn solid tb-btn" onClick={downloadResult}
                        disabled={downloading}>
                  {downloading ? '下载中…' : '⬇ 下载图片'}
                </button>
              </div>

              {/* ★ 让「修复」被看见：功能再好，用户不知道等于不存在。
                  出图即显示（图先上画布、收尾文案还在跑时也要在——
                  否则用户面对灰按钮不知道在等什么）；读过就不再出现。 */}
              {!repairTipOff && (
                <div className="result-tip" role="note">
                  <span className="result-tip-body">
                    {busy
                      ? <>成品已出 —— 收尾文案还在跑（几秒到半分钟），结束后就能用<b>「局部修复」</b>只修某一处</>
                      : <>某一处不满意不用整张重出 —— 点<b>「局部修复」</b>，说清那一处，
                         其余画面原样保留；不确定哪里不对可以让 AI 先对比原图找问题</>}
                  </span>
                  {!busy && (
                    <button type="button" className="btn ghost result-tip-go"
                            onClick={() => setRepairOpen(true)}>去修复</button>
                  )}
                  <button type="button" className="result-tip-x" aria-label="不再提示"
                          title="知道了，不再提示"
                          onClick={() => {
                            setRepairTipOff(true)
                            try { localStorage.setItem('travelogue.repairTipOff', '1') } catch { /* 隐私模式忽略 */ }
                          }}>✕</button>
                </div>
              )}
            </>
          )}

          {/* ★ 出图是全页唯一的结算动作（还会花掉额度）。
              它必须：① 紧贴它作用的画布 ② 永远可见 ③ 说清为什么现在点不了。
              ★ 列不滚动布局（2026-10-03）：操作条固定在画布之下、轨迹之上 ——
                轨迹区自滚动，操作条不再悬浮遮挡任何内容。 */}
          <div className="actionbar">
            <button
              type="button"
              className="btn-generate"
              onClick={(e) => { ripple(e); generate() }}
              disabled={busy || !source || !rendered || quota?.exhausted}
            >
              {busy ? '生成中…' : '生成图片'}
            </button>
            {busy && (
              <button
                type="button"
                className="btn ghost"
                onClick={async () => {
                  // 中止后台任务（服务端协作式取消：当前这一步跑完就收尾）
                  const tid = taskRef.current
                  taskRef.current = null
                  if (tid) {
                    try { await chatTaskCancel(tid) } catch { /* 已结束的任务忽略 */ }
                  }
                  flash('已请求终止 —— 正在进行的模型调用会跑完这一步，随后引擎立即收尾')
                }}
              >
                ■ 终止生成
              </button>
            )}
            <p className="actions-hint">{goHint}</p>
          </div>

          <h3 className="rail-title sub">
            Agent 轨迹
            {busy && <span className="muted">进行中…</span>}
          </h3>
          <div className="trace" role="log" aria-live="polite" aria-busy={busy}>
            {trace.length === 0 && !busy && (
              <p className="muted">点「生成图片」后，这里会实时列出每一步</p>
            )}
            {trace.map((t, i) => (
              <div key={i} className={`trace-row ${t.state}`}
                   title={`${t.name}${t.summary ? '：' + t.summary : ''}`}>
                <span className="trace-dot" aria-hidden="true" />
                <span className="trace-name">{t.name}</span>
                <span className="trace-sum">{t.summary || (t.state === 'running' ? '执行中…' : '')}</span>
              </div>
            ))}
          </div>
        </section>

        {/* ══ 右 · 提示词检查器（参数 → 自定义 → 渲染结果，一条因果链）════ */}
        <section className="col inspector" aria-label="提示词检查器">
          <h2 id="h-prompt" className="rail-title">
            提示词
            {rendered && <span className="muted">{family?.name || rendered.family_id}</span>}
          </h2>

          <h3 id="h-params" className="rail-title sub">参数</h3>
          <div className="pf-scroll" role="group" aria-labelledby="h-params">
            <ParamForm family={family} params={params}
                       onChange={(next, changedKey) => {
                         if (changedKey) lockedRef.current.add(changedKey)
                         setParams(next)
                       }} disabled={busy} />
          </div>

          {/* ★ 用户自定义提示词 —— 原样合进 creative 段，不经模型转述 */}
          <h3 id="h-extra" className="rail-title sub">自定义提示词</h3>
          <div className="extra" role="group" aria-labelledby="h-extra">
            <div className="extra-modes" role="group" aria-label="自定义提示词的合并方式">
              <button type="button"
                      className={`extra-mode ${extraMode === 'append' ? 'on' : ''}`}
                      disabled={busy}
                      aria-pressed={extraMode === 'append'}
                      onClick={() => setExtraMode('append')}
                      title="保留家族的创作描述，把你的要求接在后面">
                追加
              </button>
              <button type="button"
                      className={`extra-mode ${extraMode === 'replace' ? 'on' : ''}`}
                      disabled={busy}
                      aria-pressed={extraMode === 'replace'}
                      onClick={() => setExtraMode('replace')}
                      title="用你的文字替换家族的创作描述；保真约束仍然保留">
                替换创作段
              </button>
            </div>
            <label className="sr-only" htmlFor="extra-prompt">自定义提示词内容</label>
            <textarea
              id="extra-prompt"
              className="extra-input"
              rows={3}
              value={extraPrompt}
              disabled={busy}
              placeholder={
                extraMode === 'append'
                  ? '想补充什么就写这里，例如：天空压暗、只保留一个人、加一层胶片颗粒…'
                  : '直接写你想要的画面，例如：雨夜霓虹街头，主角撑透明伞，倒影拉长…'
              }
              onChange={(e) => setExtraPrompt(e.target.value)}
            />
            <p className="extra-note">
              {extraPrompt.trim()
                ? `已生效，${rendered?.extra_applied || '正在合入提示词'}`
                : (extraMode === 'append'
                    ? '留空则只用家族预设'
                    : '替换创作段后，保留项与禁止项仍然生效 —— 那是原图保真的底线')}
            </p>
          </div>

          <h3 id="h-arch" className="rail-title sub">
            {result ? '最近生成' : '风格档案'}
          </h3>
          {renderErr && <div className="notice bad" role="alert">{renderErr}</div>}

          {result ? (
            /* ★ 最近生成 —— 出图后的延续动作：换一张看、回画布细看、进仓库。
               之前放「作品档案卡」（参数回顾），实测是摆设：参数就在上面表单里，
               没人会回来看一遍自己刚选了什么。 */
            <div className="recent" role="group" aria-labelledby="h-arch">
              <div className="recent-row">
                {recent.map((it) => (
                  <button key={it.url}
                          className={`recent-thumb ${it.url === result.url ? 'on' : ''}`}
                          onClick={() => setResult({ url: it.url, family_id: it.family_id || familyId })}
                          title="点回画布查看">
                    <img src={imgSrc(it.url)} alt={it.filename} decoding="async" />
                  </button>
                ))}
              </div>
              <div className="recent-foot">
                <span className="muted">{recent.length} 张 · 点缩略图回画布</span>
                <span className="tb-spacer" />
                <button className="btn ghost tb-btn" onClick={() => setGalleryOpen(true)}>
                  查看全部 →
                </button>
              </div>
            </div>
          ) : family ? (
            /* 没出图时：当前家族的「展签」—— 用用户语言介绍这个风格 */
            <div className="placard" role="group" aria-labelledby="h-arch">
              <div className="placard-title">
                {family.icon ? `${family.icon} ` : ''}{family.name}
              </div>
              <p className="placard-desc">{family.description}</p>
              {family.suitable?.length > 0 && (
                <>
                  <div className="placard-sub">适合这类照片</div>
                  <div className="placard-chips">
                    {family.suitable.map((s) => (
                      <span key={s} className="placard-chip">{s}</span>
                    ))}
                  </div>
                </>
              )}
              {family.variants?.length > 0 && (
                <>
                  <div className="placard-sub">一键试试这些预设</div>
                  <div className="placard-chips">
                    {family.variants.map((v) => (
                      <button key={v.id} type="button" className="placard-chip btn-chip"
                              disabled={busy}
                              onClick={() => setParams((prev) => ({ ...prev, ...(v.params || {}) }))}
                              title="应用这组参数">
                        {v.name}
                      </button>
                    ))}
                  </div>
                </>
              )}
              <p className="placard-note">
                左侧调参数，画布看成品；出图后这里会变成这张图的作品档案。
              </p>
            </div>
          ) : (
            <p className="muted">先选一个风格家族</p>
          )}
        </section>
      </main>

      {/* ══ 底部对话抽屉 ══════════════════════════════════════════
          对话是「另一种入口」，不是出图的必经步骤，却很高很长。
          塞进右栏会和提示词抢空间，所以做成抽屉：默认只占一条，
          想聊再拉起来。 */}
      <div className={`dock ${chatOpen ? 'open' : ''}`}>
        <h2 className="sr-only">Agent 对话</h2>
        <button type="button" className="dock-tab"
                aria-expanded={chatOpen} aria-controls="dock-body"
                onClick={() => setChatOpen((v) => !v)}>
          <span className="dock-caret" aria-hidden="true">▲</span>
          <span className="dock-title">Agent 对话</span>
          <span className="dock-peek">{lastLine ? lastLine.slice(0, 60) : '说出想要的效果，让它自己选家族、调参数'}</span>
        </button>

        {chatOpen && (
          <div className="dock-body" id="dock-body">
            <div className="chat" role="log" aria-live="polite" aria-busy={busy}>
              {messages.length === 0 && (
                <p className="muted">
                  对话只调参数、不出图 —— 要出图用画布下方的「生成图片」。
                </p>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role}`}><span>{m.text}</span></div>
              ))}
              {busy && <div className="msg assistant"><span>工作中…</span></div>}
            </div>

            <div className="composer">
              <label className="sr-only" htmlFor="chat-input">对 Agent 说</label>
              <input id="chat-input" value={input} placeholder="描述你想要的效果…"
                     onChange={(e) => setInput(e.target.value)}
                     onKeyDown={(e) => e.key === 'Enter' && sendChat()} />
              <button type="button" className="btn ghost" onClick={sendChat}
                      disabled={busy || !input.trim()}
                      title="发送给 Agent（不会出图）">
                发送
              </button>
            </div>
          </div>
        )}
      </div>

      {workshopOpen && (
        <Workshop
          onClose={() => setWorkshopOpen(false)}
          onInstalled={reloadFamilies}
          flash={flash}
        />
      )}
      {helperOpen && (
        <HelperModal onClose={() => setHelperOpen(false)} flash={flash}
                     onPickFamily={(fid) => {
                       // 一键切到推荐风格：选中它 + 展开示例图，让用户马上看到效果
                       setFamilyId(fid)
                       setParams({})
                       lockedRef.current = new Set()
                       openExample(fid)
                     }} />
      )}
      {galleryOpen && (
        <GalleryModal onClose={() => setGalleryOpen(false)} flash={flash} />
      )}
      {settingsOpen && (
        <SettingsModal onClose={() => setSettingsOpen(false)} flash={flash}
                       settings={settings} onChange={setSettings} />
      )}
      {/* ── 大图灯箱 / 微调重出：必须在 toast-wrap 之外 ──
          toast-wrap 有 pointer-events:none（让 toast 不挡点击），
          该属性会被子元素继承——除非每层显式重置。灯箱和微调面板
          需要完整鼠标交互，放在里面会导致整层不可点（终审 P0-2）。 */}
      {lightbox && (
        <div className="lightbox" onClick={() => setLightbox(null)} role="dialog"
             aria-label="大图预览">
          <button type="button" className="lightbox-close" aria-label="关闭大图"
                  title="关闭"
                  onClick={(e) => { e.stopPropagation(); setLightbox(null) }}>✕</button>
          <img src={imgSrc(lightbox.url)} alt="大图预览" />
          <div className="lightbox-bar">
            <span className="lightbox-dim">{lightbox.dim || ""}</span>
            <span className="muted">点击图片外任意处或 ✕ 关闭</span>
          </div>
        </div>
      )}
      {repairOpen && (
        <div className="repair-mask" onClick={(e) => {
          if (e.target === e.currentTarget && !repairBusy) setRepairOpen(false)
        }}>
          <div className="repair" role="dialog" aria-modal="true" aria-label="局部修复">
            <header className="repair-head">
              <span className="repair-glyph">🩹</span>
              <div className="repair-titles">
                <span className="ws-title">局部修复</span>
                <span className="ws-sub">以当前这张成品为底，只改你点名的地方</span>
              </div>
              <button className="ws-close" onClick={() => !repairBusy && setRepairOpen(false)}
                      disabled={repairBusy} aria-label="关闭">关闭</button>
            </header>

            {/* ★ 修复对象亮明身份（2026-10-05 用户要求确认）：
                  缩略图就是被修的那张成品 —— 眼见为实，不用猜是不是在修原图 */}
            <div className="repair-target">
              <img src={imgSrc(result?.url)} alt="修复对象：当前成品" />
              <div className="repair-target-text">
                <b>修复对象</b>
                <span>当前这张成品（不是你的原图）。原图只用来给 AI 做对比参考。</span>
                {result?.size && <span className="tag">{result.size}</span>}
              </div>
            </div>

            <div className="repair-body">
              <div className="ws-actions">
                <button className="btn ghost" onClick={doDiagnose}
                        disabled={diagBusy || repairBusy || !source?.url || !result?.url}>
                  {diagBusy ? 'AI 正在对比原图找问题…' : '🔍 不确定哪里不对？让 AI 对比原图找问题'}
                </button>
                <span className="set-note-inline">
                  走一次视觉模型（不消耗出图额度）；已按「{family?.name || result?.family_id || '当前风格'}」
                  识别模板刻意添加的元素，不会把它们误报成问题
                </span>
              </div>

              {/* ★ AI 的候选是「勾选清单」：点一下加入要修清单，再点一下取消 ——
                  多处小问题可以一次勾上，由后端逐条编号下发（上限 3 条）。 */}
              {drifts.length > 0 && (
                <div className="drift-list" role="group" aria-label="AI 找到的候选问题">
                  {drifts.map((d, i) => {
                    const isDrift = d.kind !== 'adaptation'
                    const on = repairNote.split('\n').map((s) => s.trim())
                      .includes(String(d.change || '').trim())
                    return (
                      <button key={i} type="button"
                              className={`drift-item ${isDrift ? 'drift' : 'adapt'} ${on ? 'on' : ''}`}
                              title={`${d.why || ''}${on ? '（已选中，再点取消）' : '（点击加入要修清单）'}`}
                              aria-pressed={on}
                              disabled={repairBusy}
                              onClick={() => pickDrift(d)}>
                        <span className="drift-check">{on ? '✓' : ''}</span>
                        <span className="drift-kind">{isDrift ? '建议修' : '风格适配'}</span>
                        <span className="drift-text">{d.change}</span>
                      </button>
                    )
                  })}
                </div>
              )}

              <label className="ws-label" htmlFor="repair-cats">
                要修的维度（可多选）
              </label>
              <div className="repair-cats" id="repair-cats">
                {['构图与视角', '光线层级', '色彩与曝光', '材质与质感', '人物姿态', '商业化感', '其他'].map((c) => (
                  <button key={c} type="button"
                          className={`extra-mode ${repairCats.includes(c) ? 'on' : ''}`}
                          aria-pressed={repairCats.includes(c)}
                          disabled={repairBusy}
                          onClick={() => toggleCat(c)}>{c}</button>
                ))}
              </div>

              <label className="ws-label" htmlFor="repair-note">
                要修哪几处（一行一处，最多 3 处；AI 候选与手写都可以再改）
              </label>
              <textarea id="repair-note" className="ws-input" rows={4} value={repairNote}
                        maxLength={400}
                        disabled={repairBusy}
                        onChange={(e) => setRepairNote(e.target.value)}
                        placeholder={'例如：\n树冠变成圆球了，恢复成方块体素拼接\n右上角多出的英文文案删掉'} />
              <div className="repair-meta">
                <span className="muted">
                  一次最多 3 处 —— 太多改动会互相打架，修完可以再来一轮
                </span>
                <span className="tb-spacer" />
                <span className={`repair-count ${repairNote.length > 400 ? 'over' : ''}`}>
                  {repairNote.length}/400
                </span>
              </div>
              <div className="ws-actions">
                <button className="btn solid" onClick={doRepair}
                        disabled={repairBusy || !repairNote.trim()}>
                  {repairBusy
                    ? `修复中… ${repairElapsed}s`
                    : `开始修复（${repairNote.split('\n').filter((s) => s.trim()).length || '—'} 处）`}
                </button>
                <span className="set-note-inline">
                  {quota?.unlimited
                    ? "走 gpt-image 通道（额度不限）。"
                    : `会消耗 1 张额度（剩 ${quota?.remaining ?? '—'}）。`}
                  一般 40 秒到 2 分钟；修完不满意可以继续修，或用「回到上一版」退回。
                  你写过的自定义提示词与风格禁令会自动带上，修复不会破坏它们。
                </span>
              </div>
              {repairBusy && (
                <p className="repair-waitnote" role="status">
                  正在按你的清单重画 —— 页面可以离开，别重复点「开始修复」。
                </p>
              )}
            </div>
          </div>
        </div>
      )}
      {forgeNotice && (
        <div className="forge-notice-mask" onClick={(e) => e.target === e.currentTarget && setForgeNotice(null)}>
          <div className="forge-notice" role="alertdialog" aria-label="模板提炼提醒">
            <header className="fn-head">
              <span className="fn-title">
                {forgeNotice.status === 'done'
                  ? '✅ 提示词提炼好了'
                  : forgeNotice.status === 'cancelled'
                    ? '⚪ 提炼已中止'
                    : '⚠️ 提炼没有成功'}
              </span>
            </header>
            <div className="fn-body">
              {forgeNotice.status === 'done' ? (
                <>
                  <p>
                    「{forgeNotice.result?.name || '未命名'}」第 {forgeNotice.result?.version} 版已入库
                    （用时 {Math.round(forgeNotice.elapsed_sec || 0)}s）。
                  </p>
                  {(forgeNotice.result?.warnings || []).length > 0 && (
                    <p className="fn-warn">{forgeNotice.result.warnings[0]}</p>
                  )}
                </>
              ) : forgeNotice.status === 'cancelled' ? (
                <p>「{forgeNotice.result?.name || '未命名'}」的提炼已按你的要求中止，本次没有保存任何版本。</p>
              ) : (
                <p>{forgeNotice.error || '提炼未通过校验'}。可以回工坊调整图片或理论后重试。</p>
              )}
            </div>
            <div className="fn-actions">
              <button className="btn solid" onClick={() => { setForgeNotice(null); setWorkshopOpen(true) }}>
                去工坊查看
              </button>
              <button className="btn ghost" onClick={() => setForgeNotice(null)}>知道了</button>
            </div>
          </div>
        </div>
      )}
      <div className="toast-wrap" role="status" aria-live="polite" aria-atomic="true">
        {toast && <div className="toast">{toast}</div>}
      </div>
    </div>
  )
}
