import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import ParamForm from './ParamForm'
import Workshop from './Workshop'
import GalleryModal from './GalleryModal'
import HelperModal from './HelperModal'
import HeroRing from './HeroRing'
import SettingsModal, { loadSettings, applySettings } from './SettingsModal'
import { useChatStream } from './hooks/useChatStream'
import { useUpload } from './hooks/useUpload'
import { useRepair } from './hooks/useRepair'
import { useGallery } from './hooks/useGallery'
import { useFamilies } from './hooks/useFamilies'
import GalleryStrip from './components/GalleryStrip'
import Lightbox from './components/Lightbox'
import RepairPanel from './components/RepairPanel'
import ForgeNotice from './components/ForgeNotice'
import SourcePanel from './components/SourcePanel'
import FamilyList from './components/FamilyList'
import CanvasPanel from './components/CanvasPanel'
import ChatDock from './components/ChatDock'
import './App.css'

export default function App() {
  const [params, setParams] = useState({})
  const [rendered, setRendered] = useState(null)
  const [renderErr, setRenderErr] = useState('')

  const [result, setResult] = useState(null)   // {url,family_id,size}
  //★ 后台提炼的全局提醒：工坊里发起提炼后，用户去任何页面都能收到"提炼好了"的弹窗
  const [forgeNotice, setForgeNotice] = useState(null)
  // ★ 版本栈：每次成功修复前把当前成品压栈。修复不满意可「回到上一版」，
  //   既不花额度也不等—— 没有它，用户一旦修坏就得从头再出一张。
  //   三个域都会写它（修复压栈、换原图清空、新一轮出图清零），
  //   所以它是跨域共享状态，留在 App 层由各 hook 注入。
  const [undoStack, setUndoStack] = useState([])
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
  // ★ USER-LOCKED：用户在 UI 里显式动过的参数名。
  //   这些参数渲染时跳过默认值/auto 兜底 —— 用户的选择 > 系统的好意。
  const lockedRef = useRef(new Set())
  const [toast, setToast] = useState('')

  const renderTimer = useRef(null)
  const aliveRef = useRef(true)
  const toastTimer = useRef(null)   // toast 自动消失的定时器（卸载时要清）

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

  // ── 作品浏览（最近生成 / 灯箱 / 下载）──────────────────────
  const {
    recent, lightbox, openLightbox, closeLightbox,
    downloading, downloadResult,
  } = useGallery({ resultUrl: result?.url, flash, aliveRef })

  // 卸载时：标记不再 setState，并清掉待执行的定时器。
  // ★ 后台任务**不随组件卸载而取消** —— 它跑在服务端，用户刷新或换页回来还能接着看；
  //   这是异步化相对 SSE 的一个额外好处（SSE 一断连，worker 就被通知收尾了）。
  //   （删除确认的 delArmTimer 由 useFamilies 自己清，不在这里。）
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      // 定时器不取消就会在组件消失后触发 setState：轻则警告，
      // 重则把已经卸载的 toast 又改回去（用户看到「凭空又弹一次」）
      clearTimeout(toastTimer.current)
    }
  }, [])

  const flash = useCallback((msg) => {
    if (!aliveRef.current) return
    setToast(msg)
    // 存timer id：新提示会覆盖旧提示的关闭时机，
    // 否则连点三处错误，三秒后被第一个 timer 提前清掉后两条还在
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast((cur) => (cur === msg ? '' : cur)), 3200)
  }, [])

  // ── 家族域（启动健康检查 / 家族清单 / 选中 / 两段式删除）──────
  const {
    booting, families, familyId, setFamilyId, family,
    quota, ready, reloadFamilies, onRequestDeleteFamily,
    delArm, builtinIds, refreshQuota,
  } = useFamilies({ aliveRef, flash })

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

  // ── 上传（拖拽 / 点选 / 粘贴三入口）────────────────────────
  // 放在 useChatStream 之前：出图要把原图 url 作为 imageUrl 发给后端。
  const {
    source, setSource, dropRef, fileRef, doUpload, clearSource, orientation,
  } = useUpload({
    aliveRef, flash, setResult, setUndoStack, workshopOpen,
  })

  // ── 对话流（轮询 + 事件映射 + 中止）────────────────────────
  const {
    messages, trace, input, setInput, busy, chatOpen, setChatOpen,
    runStream, sendChat, abortTask, lastLine,
  } = useChatStream({
    aliveRef, flash, refreshQuota, source, extraPrompt, extraMode,
    familyId, setResult, setUndoStack, setFamilyId, setParams, lockedRef, setLatest,
  })

  // ── 局部修复（面板状态 / 秒表 / 修复提交 / 版本回退）──────────
  // 放在 useChatStream 之后：前置校验要看busy（对话进行中不许再修）。
  const {
    repairOpen, setRepairOpen, repairCats, repairNote, setRepairNote,
    repairBusy, diagBusy, drifts, repairElapsed, repairTipOff,
    onToggleCat, pickDrift, doRepair, undoRepair, doDiagnose, dismissTip,
  } = useRepair({
    flash, refreshQuota, result, setResult, source, quota, busy,
    extraPrompt, setLatest, undoStack, setUndoStack,
  })

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

          <SourcePanel
            source={source} orientation={orientation}
            dropRef={dropRef} fileRef={fileRef}
            onPick={doUpload} onClear={clearSource}
          />

          <h2 id="h-family" className="rail-title">风格家族</h2>
          {/* ★ 风格示例：每个家族按钮下独立展开的卡片（可多开，各有关闭钮）。
             点击家族 = 选中并展开它的示例；点别的家族不影响已展开的。 */}
          <FamilyList
            families={families} familyId={familyId} booting={booting}
            exOpen={exOpen} delArm={delArm} builtinIds={builtinIds}
            onPick={(id) => { pickFamily(id); openExample(id) }}
            onOpenExample={openLightbox}
            onCloseExample={closeExample}
            onRequestDelete={onRequestDeleteFamily}
          />
        </section>

        {/* ══ 中 · 画布与结算（这一列只回答「点下去会怎样」）════════ */}
        <CanvasPanel
          source={source} result={result} family={family} familyId={familyId}
          undoCount={undoStack.length}
          busy={busy} quotaExhausted={quota?.exhausted}
          canGenerate={!!rendered} goHint={goHint} repairTipOff={repairTipOff}
          trace={trace} downloading={downloading}
          onRipple={ripple} onGenerate={generate} onAbort={abortTask}
          onUndo={undoRepair} onOpenLightbox={openLightbox}
          onOpenRepair={() => setRepairOpen(true)}
          onDownload={downloadResult} onDismissTip={dismissTip}
        />

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
            <GalleryStrip items={recent} currentUrl={result.url} familyId={familyId}
                         onPick={setResult}
                         onOpenAll={() => setGalleryOpen(true)} />
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
      <ChatDock
        open={chatOpen} messages={messages} input={input}
        busy={busy} peek={lastLine}
        onToggle={() => setChatOpen((v) => !v)}
        onInput={setInput} onSend={sendChat}
      />

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
      <Lightbox item={lightbox} onClose={closeLightbox} />
      {repairOpen && (
        <RepairPanel
          result={result} source={source} family={family} quota={quota}
          cats={repairCats} note={repairNote} busy={repairBusy}
          diagBusy={diagBusy} drifts={drifts} elapsed={repairElapsed}
          onNoteChange={setRepairNote} onToggleCat={onToggleCat}
          onPickDrift={pickDrift} onDiagnose={doDiagnose} onRepair={doRepair}
          onClose={() => setRepairOpen(false)}
        />
      )}
      <ForgeNotice notice={forgeNotice}
                   onClose={() => setForgeNotice(null)}
                   onOpenWorkshop={() => setWorkshopOpen(true)} />
      <div className="toast-wrap" role="status" aria-live="polite" aria-atomic="true">
        {toast && <div className="toast">{toast}</div>}
      </div>
    </div>
  )
}
