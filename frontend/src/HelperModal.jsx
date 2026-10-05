import { useCallback, useEffect, useRef, useState } from 'react'
import { api, imgSrc } from './api'

/**
 * 小助手 🧭
 *
 * 两个能力：
 *   ① 功能讲解 —— 用大白话讲清每个功能在哪、怎么用（不谈内部实现）
 *   ② 看图推荐 —— 上传一张照片，告诉你它适合哪几个风格，并说明原因；
 *      推荐结果可直接一键切到该风格
 */
const GUIDE = [
  {
    icon: '🖼',
    title: '放一张原图进来',
    body: [
      '把照片**拖进**中间的画布、点画布选择文件，或者直接 **Ctrl+V 粘贴**剪贴板里的截图，三种都行。',
      '放好之后，左边选风格、右边调参数，然后点「生成图片」。',
    ],
  },
  {
    icon: '🎨',
    title: '选一个风格',
    body: [
      '左侧是风格家族列表，点一下家族名会展开它的**示例图**—— 一眼就能看出这个风格长什么样。',
      '示例图可以单击放大看细节，放大后点右上角 ✕ 关闭。',
      '选中家族后，右侧参数面板可以按你的喜好微调（配色、质感、版式等），调完会自动生效。',
    ],
  },
  {
    icon: '✨',
    title: '生成与成品',
    body: [
      '点「生成图片」后，下方会实时显示每一步在做什么，等一会儿画布上就会出现成品。',
      '成品可以点开看大图、下载保存；不满意可以用「微调重出」针对某一点重做一次。',
      '生成中途想停，点「终止生成」即可。',
    ],
  },
  {
    icon: '🧭',
    title: '不知道用哪个风格？直接问',
    body: [
      '在这个对话框里说一句（可以直接**附一张照片**），小助手会告诉它适合哪几个风格，以及**为什么**。',
      '推荐结果里点「用这个风格」，会自动切到该风格，直接就能生成。',
    ],
  },
  {
    icon: '🏛',
    title: '作品仓库',
    body: [
      '顶栏「作品仓库」里是你所有生成过的图，按日期排好，可以放大看、单张下载，也可以勾选多张打包。',
    ],
  },
  {
    icon: '🧪',
    title: '模板工坊：做出你自己的风格',
    body: [
      '顶栏「模板工坊」可以把你喜欢的某种画风做成新风格：放几张同类型的参考图，愿意的话再写点风格说明或贴上你收集的提示词，点「开始提炼」即可。',
      '提炼一般要几分钟，期间可以去别的页面，完成后会弹窗提醒你（必须点确认）。可以同时提交多组，一起跑。',
      '提炼好的版本会出现在「我的库」，点开后可继续迭代；满意了点「安装为家族」，它就会出现在左侧风格列表里，和内置风格一样使用。',
      '**给自己的风格配示例图**：在工坊里打开某一版，点「安装为家族」旁边的 **🖼 设置示例图**，从作品仓库里选一张你生成过的图即可 —— 这一版必须已经提炼出风格内容（有名字和提示词）时按钮才可用。',
      '库里每一版都可以删除；提炼失败的版本不会入库，只会在弹窗里告诉你原因。',
    ],
  },
  {
    icon: '⚙',
    title: '设置',
    body: [
      '顶栏「⚙ 设置」里有三组：**主题颜色**（配色方案 + 自定义背景色/文字色 + 恢复默认）、**自定义背景**（🖼 上传背景图，可移除）、**作品流内容**（页面顶部那条流显示什么）。',
      '想换**应用界面**的背景图，就在设置里的「自定义背景」上传。',
      '顶栏的状态标记会显示服务是否就绪；你的额度目前是**不限次数**。',
    ],
  },
]

export default function HelperModal({ onClose, flash, onPickFamily }) {
  const [tab, setTab] = useState('chat')        // chat | guide ：进来就是小助手对话
  const [dragging, setDragging] = useState(false)
  const [preview, setPreview] = useState(null) // {url, filename}
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const fileRef = useRef(null)
  const dialogRef = useRef(null)

  // ── 对话持久化：存 localStorage，保留最近 24 小时；关闭页面再打开，
  //    历史还在；若关闭时有一条提问没来得及收到回答，重开会自动重发。
  const HKEY = 'helperThread'
  const loadSaved = () => {
    try {
      const d = JSON.parse(localStorage.getItem(HKEY) || 'null')
      if (d && Date.now() - d.ts < 24 * 3600e3) return d
    } catch { /* ignore */ }
    return null
  }
  const saved = loadSaved()
  const [thread, setThread] = useState(saved?.thread || [])   // [{role, text, image?}]
  const [draft, setDraft] = useState('')
  const [attach, setAttach] = useState(null)    // 待发送的图片 {url, filename}
  const pendingRef = useRef(saved?.pending || null)  // 未得到回答的提问
  const threadRef = useRef(null)
  const attachRef = useRef(null)

  const QUICK = [
    '在哪里可以加入示例图？',
    '背景图怎么换？',
    '怎么把我喜欢的画风做成新风格？',
    '生成的图不满意怎么办？',
  ]

  // 对话与待答问题落盘
  useEffect(() => {
    try {
      localStorage.setItem(HKEY, JSON.stringify({
        ts: Date.now(), thread: thread.slice(-40), pending: pendingRef.current,
      }))
    } catch { /* 存储满就算了 */ }
  }, [thread])

  // 重开页面时若有没答完的提问 → 自动重发
  useEffect(() => {
    const p = pendingRef.current
    if (p) {
      pendingRef.current = null
      ask(p.text, p.image)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const clearThread = useCallback(() => {
    setThread([])
    pendingRef.current = null
    try { localStorage.removeItem(HKEY) } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    // 滚到最新一条
    const el = threadRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [thread, busy])

  // Esc 关闭 + 进来先聚焦
  useEffect(() => {
    dialogRef.current?.querySelector('button')?.focus()
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const ask = useCallback(async (text, imageUrl) => {
    const q = (text || '').trim()
    if (!q || busy) return
    const userMsg = { role: 'user', text: q, image: imageUrl || null }
    setThread((prev) => [...prev, userMsg])
    setDraft('')
    setAttach(null)
    setBusy(true)
    // 标记"有一问未答"——用户此刻关页面，重开后会自动重发这一问
    pendingRef.current = { text: q, image: imageUrl || null }
    try {
      // 历史转成后端要的形状；图片只挂在最后一条提问上（后端也是这么处理的）
      const history = thread.map((m) => ({ role: m.role, content: m.text }))
      const r = await api.helperChat([...history, { role: 'user', content: q }],
                                     imageUrl || '')
      pendingRef.current = null
      setThread((prev) => [...prev, { role: 'assistant', text: r.reply || '（没有回答）' }])
    } catch (e) {
      pendingRef.current = null
      flash?.(e.message || '小助手回答失败')
      setThread((prev) => [...prev, { role: 'assistant', text: `没回答上来：${e.message}` }])
    } finally {
      setBusy(false)
    }
  }, [busy, flash, thread])

  const pickAttach = useCallback(async (file) => {
    if (!file) return
    setBusy(true)
    try {
      const saved = await api.upload(file)
      setAttach({ url: saved.url, filename: saved.filename })
    } catch (e) {
      flash?.(e.message || '图片上传失败')
    } finally {
      setBusy(false)
    }
  }, [flash])

  const onDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    pickAttach(e.dataTransfer?.files?.[0])
  }

  return (
    <div className="ws-mask" onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="ws hl" ref={dialogRef} role="dialog" aria-modal="true" aria-label="小助手">
        <header className="ws-head">
          <div>
            <span className="ws-title">🧭 小助手</span>
            <span className="ws-sub">功能讲解 · 看图推荐风格</span>
          </div>
          <div className="ws-tabs" role="group" aria-label="小助手视图">
            <button type="button" className={`ws-tab ${tab === 'chat' ? 'on' : ''}`}
                    aria-pressed={tab === 'chat'} onClick={() => setTab('chat')}>
              小助手对话
            </button>
            <button type="button" className={`ws-tab ${tab === 'guide' ? 'on' : ''}`}
                    aria-pressed={tab === 'guide'} onClick={() => setTab('guide')}>
              功能讲解
            </button>
          </div>
          <button type="button" className="ws-close" onClick={onClose} aria-label="关闭小助手">
            关闭
          </button>
        </header>

        {tab === 'guide' ? (
          <div className="ws-body hl-guide">
            {GUIDE.map((g) => (
              <section key={g.title} className="hl-card">
                <h3 className="hl-title"><span aria-hidden="true">{g.icon}</span> {g.title}</h3>
                {g.body.map((line, i) => (
                  <p key={i} className="hl-line"
                     dangerouslySetInnerHTML={{
                       // 只做 **加粗** 一种极简标记，内容全部是本文件里的静态文案
                       __html: line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'),
                     }} />
                ))}
              </section>
            ))}
          </div>
        ) : (
          <div className="ws-body hl-chat">
            {/* ── 对话区 ── */}
            <div className="hl-thread" ref={threadRef} role="log" aria-live="polite"
                 aria-busy={busy}>
              {thread.length === 0 && (
                <div className="hl-empty">
                  <p>关于这个应用的问题都可以问我，例如：</p>
                  <div className="hl-quick">
                    {QUICK.map((q) => (
                      <button key={q} type="button" className="hl-chip"
                              onClick={() => ask(q, null)}>{q}</button>
                    ))}
                  </div>
                  <p className="muted">也可以发一张照片来问「这张图适合哪个风格」。</p>
                </div>
              )}
              {thread.map((m, i) => (
                <div key={i} className={`hl-bubble ${m.role === 'user' ? 'me' : 'bot'}`}>
                  {m.image && <img className="hl-bubble-img" src={imgSrc(m.image)} alt="附上的图片" />}
                  <div className="hl-text">{m.text}</div>
                </div>
              ))}
              {busy && <div className="hl-bubble bot"><div className="hl-text muted">正在想…</div></div>}
            </div>

            {/* ── 输入区 ── */}
            <div className={`hl-input ${dragging ? 'on' : ''}`}
                 onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
                 onDragLeave={() => setDragging(false)}
                 onDrop={onDrop}>
              {attach && (
                <div className="hl-attach">
                  <img src={imgSrc(attach.url)} alt={attach.filename} />
                  <button type="button" className="link" onClick={() => setAttach(null)}
                          aria-label="移除附上的图片">移除</button>
                </div>
              )}
              <div className="hl-input-row">
                <button type="button" className="btn ghost tb-btn"
                        onClick={() => attachRef.current?.click()}
                        title="附上一张照片（可以问它适合哪个风格）">
                  🖼 附图
                </button>
                <input ref={attachRef} type="file" accept="image/*" hidden tabIndex={-1}
                       onChange={(e) => pickAttach(e.target.files?.[0])} />
                <textarea className="hl-entry" rows={2} value={draft}
                          placeholder="问点什么？（例如：这张照片适合哪个风格 / 示例图在哪加）"
                          onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault()
                              ask(draft, attach?.url || '')
                            }
                          }} />
                <button type="button" className="btn solid tb-btn"
                        onClick={() => ask(draft, attach?.url || '')}
                        disabled={busy || !draft.trim()}>
                  发送
                </button>
              </div>
              <p className="muted hl-tip">
                <span>Enter 发送 · Shift+Enter 换行 · 小助手只回答本应用相关问题</span>
                <button type="button" className="link hl-clear"
                        onClick={clearThread}
                        title="清空当前对话记录（不影响任何已生成内容）">
                  🗑 清空对话
                </button>
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
