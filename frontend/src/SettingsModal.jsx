import { useCallback, useEffect, useRef, useState } from 'react'
import { api, imgSrc, setAuthToken, getAuthToken } from './api'

/**
 * 设置面板 —— 主题 / 背景 / 作品流来源
 *
 * 持久化策略：全部存 localStorage（'app-settings'），App 启动时读回并应用。
 * 背景图做了一层降采样（最长边 1600px、JPEG q0.8）再存 dataURL ——
 * 原图直存 localStorage 很容易超 5MB 配额，设置悄悄失效是最烦人的那种 bug。
 */

export const PALETTES = [
  { id: 'paper', name: '暖纸', vars: { '--paper': '#f0e8d9', '--paper-2': '#faf5ec', '--ink': '#4a4438', '--ink-soft': '#8a8070', '--line': '#a89a82', '--red': '#c26b4a', '--mustard': '#c9973f', '--teal': '#5b9279', '--sky': '#6b93b8', '--col-overlay': 'rgba(250, 245, 236, .82)' } },
  { id: 'celadon', name: '青瓷', vars: { '--paper': '#e9f0ec', '--paper-2': '#f7fbf8', '--ink': '#37504a', '--ink-soft': '#7e958d', '--line': '#8fa89e', '--red': '#c2704f', '--mustard': '#c2a04c', '--teal': '#4d8a7c', '--sky': '#6795ab', '--col-overlay': 'rgba(247, 251, 248, .82)' } },
  { id: 'mist', name: '雾蓝', vars: { '--paper': '#e9edf4', '--paper-2': '#f7f9fd', '--ink': '#3d4c66', '--ink-soft': '#8290a8', '--line': '#9aa8c0', '--red': '#c07770', '--mustard': '#c4a05c', '--teal': '#6295ad', '--sky': '#5f86b5', '--col-overlay': 'rgba(247, 249, 253, .82)' } },
  { id: 'blush', name: '藕粉', vars: { '--paper': '#f5ece7', '--paper-2': '#fdf6f2', '--ink': '#574442', '--ink-soft': '#a18b86', '--line': '#bfa8a2', '--red': '#bf6a60', '--mustard': '#c49d63', '--teal': '#9a7d80', '--sky': '#ad7f8a', '--col-overlay': 'rgba(253, 246, 243, .82)' } },
  { id: 'ink-dark', name: '玄墨', vars: { '--paper': '#282d33', '--paper-2': '#31373e', '--ink': '#d6dade', '--ink-soft': '#9aa3ab', '--line': '#4d555e', '--red': '#d07a62', '--mustard': '#d3ab5e', '--teal': '#5ea597', '--sky': '#7ba6cc', '--col-overlay': 'rgba(40, 45, 51, .84)' } },
]

const DEFAULTS = {
  palette: 'paper',
  customBg: '',          // 自定义背景色（覆盖主题的 --paper）
  customInk: '',         // 自定义文字色（覆盖主题的 --ink）
  bgImage: '',           // 自定义背景图 dataURL
  stripSource: 'mix',    // mix = 精选+我的生成 | upload = 我的上传
}

export function loadSettings() {
  try { return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem('app-settings') || '{}')) } }
  catch { return { ...DEFAULTS } }
}
export function saveSettings(s) {
  localStorage.setItem('app-settings', JSON.stringify(s))
}

/** 把设置应用到 DOM（CSS 变量）—— App 启动与每次修改都会调用 */
export function applySettings(s) {
  const palette = PALETTES.find((x) => x.id === s.palette) || PALETTES[0]
  const root = document.documentElement
  Object.entries(palette.vars).forEach(([k, v]) => root.style.setProperty(k, v))
  if (s.customBg) root.style.setProperty('--paper', s.customBg)
  if (s.customInk) root.style.setProperty('--ink', s.customInk)
  if (s.bgImage) root.style.setProperty('--app-bg-image', `url("${s.bgImage}")`)
  else root.style.removeProperty('--app-bg-image')
}

/** 降采样：最长边 1600，JPEG q0.8 —— 保 localStorage 配额 */
function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const img = new Image()
      img.onload = () => {
        const scale = Math.min(1, 1600 / Math.max(img.width, img.height))
        const w = Math.round(img.width * scale)
        const h = Math.round(img.height * scale)
        const cv = document.createElement('canvas')
        cv.width = w; cv.height = h
        cv.getContext('2d').drawImage(img, 0, 0, w, h)
        resolve(cv.toDataURL('image/jpeg', 0.8))
      }
      img.onerror = () => reject(new Error('图片解析失败'))
      img.src = reader.result
    }
    reader.onerror = () => reject(new Error('读取失败'))
    reader.readAsDataURL(file)
  })
}

export default function SettingsModal({ onClose, flash, settings, onChange, stripCounts }) {
  const [bgBusy, setBgBusy] = useState(false)
  const [uploadBusy, setUploadBusy] = useState(false)
  const fileRef = useRef(null)
  const wallRef = useRef(null)

  // ── 我的会话（公开版身份）────────────────────────────
  // me: {session_id, code, account, logged_in} | null
  const [me, setMe] = useState(null)
  const [idBusy, setIdBusy] = useState(false)
  const [bindForm, setBindForm] = useState({ username: '', password: '', hint: '' })
  const [restoreCode, setRestoreCode] = useState('')

  const refreshMe = useCallback(async () => {
    try {
      const d = await api.authMe()
      setMe(d)
      // 静默降级：本地令牌已失效（后端返回未登录）→ 清掉，避免每次都白发
      if (d && !d.logged_in && getAuthToken()) setAuthToken('')
    } catch { /* 设置面板打开时网络断了：身份区显示占位即可 */ }
  }, [])

  useEffect(() => { refreshMe() }, [refreshMe])

  const afterAuth = useCallback((token, okMsg) => {
    setAuthToken(token)
    refreshMe()
    flash?.(okMsg)
  }, [refreshMe, flash])

  const onBind = useCallback(async () => {
    if (idBusy) return
    setIdBusy(true)
    try {
      const d = await api.authBind(bindForm.username.trim(), bindForm.password, bindForm.hint.trim())
      setBindForm({ username: '', password: '', hint: '' })
      afterAuth(d.token, `账号绑定成功，以后凭「${bindForm.username.trim()}」就能在任何设备找回作品`)
    } catch (e) {
      flash?.(e.message || '绑定没有成功，请稍后再试')
    } finally { setIdBusy(false) }
  }, [idBusy, bindForm, afterAuth, flash])

  const onLogin = useCallback(async () => {
    if (idBusy) return
    setIdBusy(true)
    try {
      const d = await api.authLogin(bindForm.username.trim(), bindForm.password)
      afterAuth(d.token, '登录成功，你的作品都在')
    } catch (e) {
      // ★ 忘密码必须有**看得见**的出路（2026-10-06）：后端早就提供了管理员重置
      //   （POST /admin/users/reset-password），但用户不知道它存在的话，这个能力
      //   等于没交付 —— 他会以为账号连同作品一起丢了。
      //   所以这里把两条退路都讲明白：访客码当场自救，账号密码找站长。
      const base = e.message || '登录没有成功，请核对用户名或密码'
      flash?.(e.status === 401
        ? `${base}。都想不起来的话，凭本页那串访客码也能找回作品；账号密码可以请站长在管理后台帮你重置。`
        : base)
    } finally { setIdBusy(false) }
  }, [idBusy, bindForm, afterAuth, flash])

  const onRestore = useCallback(async () => {
    if (idBusy) return
    setIdBusy(true)
    try {
      const d = await api.sessionRestore(restoreCode.trim().toUpperCase())
      setRestoreCode('')
      afterAuth(d.token, '找回成功，你的作品都回来了')
    } catch (e) {
      flash?.(e.message || '没有找到对应的记录，请核对访客码')
    } finally { setIdBusy(false) }
  }, [idBusy, restoreCode, afterAuth, flash])

  const onLogout = useCallback(() => {
    setAuthToken('')
    refreshMe()
    flash?.('已退出登录；当前浏览器仍会保留这次会话的作品')
  }, [refreshMe, flash])

  const copyCode = useCallback(async () => {
    if (!me?.code) return
    const code = me.code
    // ★ 为什么要两道手段（2026-10-06）：`navigator.clipboard` 只在**安全上下文**
    //   （HTTPS 或 localhost）里可用 —— 从手机访问 http://192.168.x.x:8000
    //   这种局域网部署时它是 undefined，`writeText` 一并失败。
    //   而访客码是「清了缓存之后唯一的救命稻草」：复制失败 + 用户没记下来
    //   = 作品永久丢失。多花几行换来一条传统通道，这笔账很好算。
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(code)
        flash?.('访客码已复制，建议粘贴保存到备忘录')
        return
      } catch { /* 落到下面的传统通道 */ }
    }
    try {
      const ta = document.createElement('textarea')
      ta.value = code
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      if (ok) {
        flash?.('访客码已复制，建议粘贴保存到备忘录')
        return
      }
    } catch { /* 两条通道都不行，就把码给用户看 */ }
    flash?.(`访客码：${code}（没能自动写入剪贴板，请务必手动记下来）`)
  }, [me, flash])

  const update = (patch) => {
    const next = { ...settings, ...patch }
    onChange(next)
    saveSettings(next)
    applySettings(next)
  }

  const onBgFile = useCallback(async (f) => {
    if (!f) return
    setBgBusy(true)
    try {
      const dataUrl = await fileToDataUrl(f)
      update({ bgImage: dataUrl })
      flash?.('背景已更换')
    } catch (e) {
      flash?.(e.message || '背景设置失败')
    } finally { setBgBusy(false) }
  }, [update, flash])

  /** 设置里直接上传作品流图片 —— 走既有上传接口，落进 storage 变成 kind=upload */
  const onWallFiles = useCallback(async (files) => {
    if (!files?.length) return
    setUploadBusy(true)
    let ok = 0
    for (const f of Array.from(files)) {
      try { await api.upload(f); ok += 1 } catch { /* 单张失败不拖垮整批 */ }
    }
    setUploadBusy(false)
    flash?.(`已上传 ${ok} 张到作品流`)
    wallRef.current?.click()
  }, [flash])

  return (
    <div className="set-mask" onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="set" role="dialog" aria-label="设置">
        <header className="set-head">
          <span className="ws-title">设置</span>
          <button className="ws-close" onClick={onClose}>关闭</button>
        </header>

        <div className="set-body">
          {/* ── 主题 ── */}
          <h3 className="set-title">主题颜色</h3>
          <div className="set-swatches">
            {PALETTES.map((p) => {
              const on = settings.palette === p.id && !settings.customBg && !settings.customInk
              return (
                <button key={p.id}
                        className={`set-swatch ${on ? 'on' : ''}`}
                        onClick={() => update({ palette: p.id, customBg: '', customInk: '' })}
                        title={p.name}>
                  <span className="sw-a" style={{ background: p.vars['--paper'] }} />
                  <span className="sw-b" style={{ background: p.vars['--ink'] }} />
                  <span className="sw-c" style={{ background: p.vars['--red'] }} />
                  <span className="sw-name">{p.name}</span>
                </button>
              )
            })}
          </div>
          <div className="set-row">
            <label className="set-field">
              <span>背景色</span>
              <input type="color" value={settings.customBg || '#efe4d4'}
                     onChange={(e) => update({ customBg: e.target.value, palette: 'custom' })} />
            </label>
            <label className="set-field">
              <span>文字色</span>
              <input type="color" value={settings.customInk || '#16242b'}
                     onChange={(e) => update({ customInk: e.target.value, palette: 'custom' })} />
            </label>
            <button className="btn ghost tb-btn"
                    onClick={() => update({ palette: 'paper', customBg: '', customInk: '', bgImage: '' })}>
              恢复默认
            </button>
          </div>

          {/* ── 背景图 ── */}
          <h3 className="set-title">自定义背景</h3>
          <div className="set-row">
            <input ref={fileRef} type="file" accept="image/*" hidden
                   onChange={(e) => onBgFile(e.target.files?.[0])} />
            <button className="btn ghost tb-btn" disabled={bgBusy}
                    onClick={() => fileRef.current?.click()}>
              {bgBusy ? '处理中…' : '🖼 上传背景图'}
            </button>
            {settings.bgImage && (
              <>
                <img className="set-bg-thumb" src={settings.bgImage} alt="当前背景" />
                <button className="btn ghost tb-btn"
                        onClick={() => update({ bgImage: '' })}>移除</button>
              </>
            )}
          </div>
          <p className="set-note">背景会以半透明纸色垫底，不影响文字阅读；图片会自动压缩后保存在本机。</p>

          {/* ── 作品流 ── */}
          <h3 className="set-title">作品流内容（页面顶部）</h3>
          <div className="set-radios" role="radiogroup" aria-label="作品流来源">
            <label className={`set-radio ${settings.stripSource === 'mix' ? 'on' : ''}`}>
              <input type="radio" name="strip" checked={settings.stripSource === 'mix'}
                     onChange={() => update({ stripSource: 'mix' })} />
              <span>精选作品 + 我的生成</span>
              <em>{stripCounts?.mix ?? '—'} 张</em>
            </label>
            <label className={`set-radio ${settings.stripSource === 'upload' ? 'on' : ''}`}>
              <input type="radio" name="strip" checked={settings.stripSource === 'upload'}
                     onChange={() => update({ stripSource: 'upload' })}
                     disabled={(stripCounts?.upload ?? 0) < 1} />
              <span>只用我的上传</span>
              <em>{stripCounts?.upload ?? '—'} 张</em>
            </label>
          </div>

          <div className="set-row">
            <input ref={wallRef} type="file" accept="image/*" multiple hidden
                   onChange={(e) => onWallFiles(e.target.files)} />
            <button className="btn ghost tb-btn" disabled={uploadBusy}
                    onClick={() => wallRef.current?.click()}>
              {uploadBusy ? '上传中…' : '＋ 上传图片到作品流'}
            </button>
            <span className="set-note-inline">至少上传 1 张后即可切换为「只用我的上传」</span>
          </div>

          {/* ── 我的会话 ── */}
          <h3 className="set-title">我的会话</h3>
          <div className="set-id-box">
            <div className="set-id-code-row">
              <span className="set-id-label">访客码</span>
              <code className="set-id-code">{me?.code || '······'}</code>
              {me?.code && (
                <button className="btn ghost tb-btn set-id-mini" onClick={copyCode}>复制</button>
              )}
            </div>
            <p className="set-note">
              你的对话、作品和上传都保存在这个会话里。换浏览器或清空缓存后，
              凭这串访客码就能全部找回 —— 建议复制保存到备忘录。
            </p>

            {me?.logged_in ? (
              <div className="set-id-acct">
                <span>已绑定账号：<b>{me.account?.username}</b>
                  {me.account?.hint && <em className="set-id-hint">（提示：{me.account.hint}）</em>}
                </span>
                <button className="btn ghost tb-btn set-id-mini" onClick={onLogout}>退出登录</button>
              </div>
            ) : (
              <div className="set-id-forms">
                <div className="set-id-form">
                  <span className="set-id-label">绑定账号 <em>可选 · 绑定后账密登录更省心</em></span>
                  <div className="set-row">
                    <input className="set-id-input" placeholder="用户名（2-20字）" maxLength={40}
                           value={bindForm.username}
                           onChange={(e) => setBindForm({ ...bindForm, username: e.target.value })} />
                    <input className="set-id-input" type="password" placeholder="设置密码（至少 6 位）" maxLength={128}
                           value={bindForm.password}
                           onChange={(e) => setBindForm({ ...bindForm, password: e.target.value })} />
                    <input className="set-id-input" placeholder="密码提示（可不填）" maxLength={100}
                           value={bindForm.hint}
                           onChange={(e) => setBindForm({ ...bindForm, hint: e.target.value })} />
                    <button className="btn ghost tb-btn set-id-mini" disabled={idBusy}
                            onClick={onBind}>绑定</button>
                  </div>
                </div>
                <div className="set-id-form">
                  <span className="set-id-label">找回之前的作品</span>
                  <div className="set-row">
                    <input className="set-id-input set-id-code-in" placeholder="输入 6 位访客码" maxLength={12}
                           value={restoreCode}
                           onChange={(e) => setRestoreCode(e.target.value.toUpperCase())} />
                    <button className="btn ghost tb-btn set-id-mini" disabled={idBusy || restoreCode.trim().length !== 6}
                            onClick={onRestore}>找回</button>
                    <button className="btn ghost tb-btn set-id-mini" disabled={idBusy || !bindForm.username.trim() || !bindForm.password}
                            onClick={onLogin}>用账号登录</button>
                  </div>
                  <p className="set-note">用过这台设备之外的其他浏览器？凭访客码或账号密码都能找回全部作品。</p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
