import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import GalleryModal from './GalleryModal'
import { useForgeTask } from './hooks/useForgeTask'
import { useForgeDrafts } from './hooks/useForgeDrafts'
import { failNote } from './lib/forge'
import ForgeInput from './components/ForgeInput'
import ForgeLibrary from './components/ForgeLibrary'
import ForgeResult from './components/ForgeResult'
import ForgeDialogs from './components/ForgeDialogs'

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
 *
 * ★ 拆分说明（2026-10-07）
 * ----------------------
 * 本文件从 875 行降到 300 行出头，四个域各自独立：
 *   - 提炼任务轮询 / 迭代   → hooks/useForgeTask.js（定时器只在这创建与销毁）
 *   - 草稿/ 版本链 / 复制安装 → hooks/useForgeDrafts.js（current 只有一处真相）
 *   - 校验与文案             → lib/forge.js、lib/forgeTasks.js（130+ 个用例锁着）
 *   - 展示                   → components/Forge*.jsx（纯展示，无请求编排）
 *
 * ★★ 定义顺序铁律（别再踩 2026-10-07 那次 TDZ 崩页）：
 *   函数体内任何 const / let / function 的定义，必须排在**所有使用它的位置之前**。
 *   本文件里两个 hook 的调用顺序是硬约束，不是风格偏好：
 *     useForgeDrafts 必须在前 —— useForgeTask 要用到它返回的
 *     setFeedback / current / onRefreshLibrary。写成
 *     `useForgeTask({ setFeedback })` 属于**立即求值**，setFeedback
 *     还没绑定就是 ReferenceError（JSX 属性那种延迟求值才允许在后）。
 */
export default function Workshop({ onClose, onInstalled, flash }) {
  // ── 输入区的三路素材 + 迭代意见（只属于本组件，不进任何 hook）────
  const [theory, setTheory] = useState('')
  const [notes, setNotes] = useState('')
  const [name, setName] = useState('')
  const [stylePrompt, setStylePrompt] = useState('')   // 用户收集的生图 Prompt（第三输入源）
  const [feedback, setFeedback] = useState('')         // 对当前版本的修改意见
  const [images, setImages] = useState([])             // [{url, filename}]
  const [refsDragging, setRefsDragging] = useState(false)
  const fileRef = useRef(null)

  // ── 草稿域（必须先于 useForgeTask 定义：下面要把它的 setter 传进去）──
  const drafts = useForgeDrafts({ flash, onInstalled })
  const {
    current, versions, library, tab, setTab,
    installing, copying, copied, promptSaving, promptEditing, setPromptEditing,
    promptDraft, setPromptDraft, showPrompt, setShowPrompt,
    pickExample, setPickExample,
    openLibrary, loadVersion, removeDraft, adoptDraft, appendVersion,
    savePrompt, install, copyPrompt, setExample,
  } = drafts

  // ── 提炼任务域：后台轮询 + 迭代 + 提交 ──────────────────
  const task = useForgeTask({
    flash,
    current,
    feedback,
    images,
    setFeedback,
    setPromptEditing,
    onDraftReady: adoptDraft,
    onReviseDone: appendVersion,
    onRefreshLibrary: drafts.refreshLibrary,
  })
  const { bgTasks, submitting, reviseConfirm, setReviseConfirm, revising,
    donePopupQueue, setDonePopupQueue,
    doDraft, doCancel, onReviseClick, doRevise } = task

  // ── 弹窗打开期间 Esc 完全失效（capture 阶段拦截，先于对话框的 Esc 关闭逻辑）──
  useEffect(() => {
    if (donePopupQueue.length === 0 && !reviseConfirm) return undefined
    const stop = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation() }
    }
    document.addEventListener('keydown', stop, true)
    return () => document.removeEventListener('keydown', stop, true)
  }, [donePopupQueue.length, reviseConfirm])

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

  // ── 对话框该有的三件事：进来先给焦点、Esc 能关、Tab 不会跑出去 ──
  const dialogRef = useRef(null)
  const restoreRef = useRef(null)
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

  const uploadRefs = useCallback(async (files) => {
    for (const f of Array.from(files || [])) {
      try {
        const r = await api.upload(f)
        setImages((prev) => [...prev, { url: r.url, filename: r.filename }])
      } catch (e) {
        flash?.(e.message || '上传失败')
      }
    }
  }, [flash])

  const submitDraft = useCallback(() => {
    doDraft({ theory, notes, name, stylePrompt, images })
  }, [doDraft, theory, notes, name, stylePrompt, images])

  const openFromLibrary = useCallback((it) => {
    loadVersion(it.id)
    setTab('draft')
  }, [loadVersion])

  const startEditPrompt = useCallback(() => {
    setPromptDraft(current?.prompt_override || current?.prompt || '')
    setPromptEditing(true)
  }, [current, setPromptDraft, setPromptEditing])

  // ── 派生（放在所有 hook 调用之后，纯计算无副作用）────────
  const spec = current?.spec
  const seg = spec?.segments
  // ★ 失败版本识别：后端把失败原因写进了记录的 feedback 字段（【提炼失败】前缀）。
  //   不标记的话，一条「未命名 / 空提示词」的失败版会被当成正常版本，误导用户。
  const failNoteText = failNote(current)

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
          <ForgeLibrary
            library={library}
            bgTasks={bgTasks}
            onOpen={openFromLibrary}
            onRemove={removeDraft}
            onCancelTask={doCancel}
          />
        ) : (
          <div className="ws-body">
            <ForgeInput
              theory={theory}
              notes={notes}
              name={name}
              stylePrompt={stylePrompt}
              images={images}
              fileRef={fileRef}
              refsDragging={refsDragging}
              submitting={submitting}
              hasCurrent={!!current}
              bgTasks={bgTasks}
              onName={setName}
              onTheory={setTheory}
              onNotes={setNotes}
              onStylePrompt={setStylePrompt}
              onImages={setImages}
              onUploadRefs={uploadRefs}
              onDragging={setRefsDragging}
              onDraft={submitDraft}
              onCancelTask={doCancel}
            />
            <ForgeResult
              current={current}
              spec={spec}
              seg={seg}
              failNoteText={failNoteText}
              versions={versions}
              feedback={feedback}
              revising={revising}
              copying={copying}
              copied={copied}
              installing={installing}
              showPrompt={showPrompt}
              promptEditing={promptEditing}
              promptDraft={promptDraft}
              promptSaving={promptSaving}
              onFeedback={setFeedback}
              onLoadVersion={loadVersion}
              onReviseClick={onReviseClick}
              onCopy={copyPrompt}
              onInstall={install}
              onPickExample={() => setPickExample(true)}
              onTogglePrompt={() => setShowPrompt((v) => !v)}
              onEditPrompt={startEditPrompt}
              onCancelEdit={() => setPromptEditing(false)}
              onPromptDraft={setPromptDraft}
              onSavePrompt={savePrompt}
              onRestorePrompt={() => savePrompt('')}
            />
          </div>
        )}

        <ForgeDialogs
          reviseConfirm={reviseConfirm}
          current={current}
          feedback={feedback}
          donePopupQueue={donePopupQueue}
          onConfirmRevise={doRevise}
          onEditInstead={() => { setReviseConfirm(false); setShowPrompt(true) }}
          onCancelRevise={() => setReviseConfirm(false)}
          onAckDone={() => setDonePopupQueue((q) => q.slice(1))}
        />

        {/* 选图做家族示例：复用作品仓库，单选取一张（放在确认弹窗之后，避免被遮挡） */}
        {pickExample && (
          <GalleryModal pickMode
                        pickHint="设为该家族示例图"
                        onPick={setExample}
                        onClose={() => setPickExample(false)}
                        flash={flash} />
        )}
      </div>
    </div>
  )
}
