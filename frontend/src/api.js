// 所有后端请求都从这里出发 —— 单一真源
//
// 旧版 6 处硬编码 `http://localhost:8000`，改端口要改 6 遍。
// 现在统一走同一源：dev 用 vite proxy 转发，prod 同源部署。

const RAW = import.meta.env.VITE_API_BASE ?? ''
const BASE = RAW.replace(/\/$/, '')

// 可选本地令牌：后端配了 LOCAL_TOKEN 时才需要（见 backend/.env.example）。
// 默认留空 —— 后端只监听 127.0.0.1 时不需要它。
const LOCAL_TOKEN = import.meta.env.VITE_LOCAL_TOKEN ?? ''

// ── 会话身份（公开版多用户隔离）──────────────────────────
// 匿名访客：localStorage 里一个 UUID，打开就能用，无需注册。
// 每个会话的对话 / 作品 / 上传 / 安装记录在后端按这个 ID 互相隔离。
// 换浏览器或清缓存后：凭 6 位访客码找回，或用绑定的账号密码登录。
const SID_KEY = 'travelogue.session_id'
const TOKEN_KEY = 'travelogue.auth_token'

const SID_RE = /^[0-9a-f][0-9a-f-]{7,63}$/   // 与后端 identity._SESSION_RE 一致

export function getSessionId() {
  let sid = ''
  try { sid = localStorage.getItem(SID_KEY) || '' } catch { /* 隐私模式下照常用 */ }
  if (!SID_RE.test(sid)) {
    sid = (crypto.randomUUID ? crypto.randomUUID()
                             : `11${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}-4xxx`.replace(/x/g, () => '0123456789abcdef'[Math.floor(Math.random() * 16)]))
    try { localStorage.setItem(SID_KEY, sid) } catch { /* 同上 */ }
  }
  return sid
}

/** 登录 / 绑定 / 凭码找回成功后调用：令牌即会话凭证，之后所有请求自动带上 */
export function setAuthToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* ignore */ }
}
export function getAuthToken() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}

function authHeaders(extra = {}) {
  const h = { ...extra, 'X-Session-Id': getSessionId() }
  if (LOCAL_TOKEN) h['X-Local-Token'] = LOCAL_TOKEN
  const token = getAuthToken()
  if (token) h['X-Auth-Token'] = token      // 后端优先级：令牌 > 会话头
  return h
}

export function apiPath(path) {
  return `${BASE}${path.startsWith('/') ? path : '/' + path}`
}

export function imgSrc(url) {
  if (!url) return ''
  if (/^(https?:)?\/\//.test(url)) return url
  if (url.startsWith('/')) return `${BASE}${url}`
  return `${BASE}/images/${url}` // 兼容只给文件名的老调用方
}

/** 缩略图：装饰性场景（作品流/仓库网格）用它，原图只在大图预览时加载 */
export function thumbSrc(url, w = 320) {
  if (!url) return ''
  return `${BASE}/api/image/thumb?u=${encodeURIComponent(url)}&w=${w}`
}

async function request(path, options = {}) {
  // 先把 headers 解构出来再展开剩余项 —— 旧写法是
  //   { headers: A, ...options, ...(options.headers ? { headers: B } : {}) }
  // 三重展开互相覆盖，能跑但极脆：一旦有人往 options 里再塞个 headers 就静默改行为。
  const { headers: extraHeaders, ...rest } = options
  const resp = await fetch(apiPath(path), {
    ...rest,
    headers: authHeaders({
      'Content-Type': 'application/json',
      ...(extraHeaders || {}),
    }),
  })
  if (!resp.ok) {
    let detail = '刚才的操作没有成功，请稍后再试一次'
    try {
      const body = await resp.json()
      if (body?.code === 'origin_denied') {
        detail = '当前访问来源未被允许 —— 如果你是本机开发者，请把前端端口加进 backend/.env 的 CORS_ORIGINS'
      } else if (body?.code === 'token_required') {
        detail = '这个部署开启了访问口令，需要在前端配置 VITE_LOCAL_TOKEN 才能使用'
      } else if (resp.status >= 500) {
        // 服务端 5xx：状态码对用户没意义，给一句体面的话，细节留给控制台
        console.warn('[api]', resp.status, body)
        detail = body?.detail?.message || '服务暂时有点忙，请稍等片刻再试'
      } else {
        detail = body?.detail?.message || body?.detail || body?.error || detail
      }
    } catch { /* 非 JSON 响应就用兜底文案 */ }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return resp.json()
}

export const api = {
  health: (threadId) => request(`/api/health?thread_id=${encodeURIComponent(threadId)}`),
  families: (full = true) => request(`/api/families?full=${full}`),
  family: (id) => request(`/api/families/${encodeURIComponent(id)}`),
  // 删除一个工坊安装的家族（内置家族后端会拒绝）
  familiesDelete: (id) =>
    request(`/api/families/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  sources: () => request('/api/sources'),
  // ── 历史作品仓库 / 3D 墙 ──────────────────────────
  // force=true 绕过服务端 15s TTL 缓存（生成完成后对账必须用它，否则拿到旧列表）
  gallery: (limit = 200, kinds = '', force = false) =>
    request(`/api/image/gallery?limit=${limit}${kinds ? `&kinds=${kinds}` : ''}${force ? '&force=true' : ''}`),
  // 批量下载走原生 fetch（要拿 blob 存盘，不走统一 request 的 JSON 处理）
  galleryDownload: async (urls) => {
    const resp = await fetch(apiPath('/api/image/gallery/download'), {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ urls }),
    })
    if (!resp.ok) {
      let msg = '打包下载没有成功，请稍后再试'
      try { const b = await resp.json(); msg = b?.detail?.message || b?.detail || msg } catch { /* 保留兜底 */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return resp   // 调用方拿 blob
  },

  // ── 模板工坊 ───────────────────────────────────────
  forgeDraft: (payload) =>
    request('/api/forge/draft', { method: 'POST', body: JSON.stringify(payload) }),
  // 后台提炼：立即返回 {task_id}，提炼在服务端线程继续 —— 用户可离开页面
  forgeDraftAsync: (payload) =>
    request('/api/forge/draft-async', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // 查后台提炼任务：{task_id, status: running|done|failed|cancelled, phase, elapsed_sec, result}
  forgeTask: (taskId) => request(`/api/forge/tasks/${encodeURIComponent(taskId)}`),
  // 中止一个进行中的提炼（协作式：当前阶段结束后终止，几秒内生效）
  forgeCancel: (taskId) =>
    request(`/api/forge/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
  forgeRevise: (forgeId, feedback, imageUrls = []) =>
    request('/api/forge/revise', {
      method: 'POST',
      body: JSON.stringify({ forge_id: forgeId, feedback, image_urls: imageUrls }),
    }),
  forgeLibrary: (limit = 50) => request(`/api/forge/library?limit=${limit}`),
  forgeGet: (id) => request(`/api/forge/${encodeURIComponent(id)}`),
  // 手改提示词：非空=保存并直接生效（生成时跳过 spec 渲染）；空串=恢复自动渲染
  forgeSavePrompt: (id, prompt) =>
    request(`/api/forge/${encodeURIComponent(id)}/prompt`, {
      method: 'PUT',
      body: JSON.stringify({ prompt }),
    }),
  forgeInstall: (id) =>
    request(`/api/forge/${encodeURIComponent(id)}/install`, { method: 'POST' }),
  forgeRender: (id, params = {}) =>
    request(`/api/forge/${encodeURIComponent(id)}/render`, {
      method: 'POST',
      body: JSON.stringify({ params }),
    }),
  forgeDelete: (id) =>
    request(`/api/forge/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // 给家族设置示例图（image_url 必须是本应用存储目录里的图）
  setFamilyExample: (familyId, imageUrl) =>
    request(`/api/families/${encodeURIComponent(familyId)}/example`, {
      method: 'POST', body: JSON.stringify({ image_url: imageUrl }),
    }),

  // ── 小助手：看图推荐适合的风格 ──────────────────────
  helperRecommend: (imageUrl) =>
    request('/api/helper/recommend', {
      method: 'POST', body: JSON.stringify({ image_url: imageUrl }),
    }),
  // ── 小助手：对话问答（只回答本应用相关问题；可带一张图问）──
  helperChat: (messages, imageUrl = '') =>
    request('/api/helper/chat', {
      method: 'POST',
      body: JSON.stringify({ messages, image_url: imageUrl || '' }),
    }),

  policy: (threadId) =>
    request(`/api/chat/policy?thread_id=${encodeURIComponent(threadId)}`),

  // extra: { extraPrompt, extraMode } —— 用户自定义提示词
  // 传对象而不是位置参数：以后再加字段不用改所有调用点
  render: (sourceId, params = {}, extra = {}) =>
    request('/api/image/render', {
      method: 'POST',
      body: JSON.stringify({
        source_id: sourceId,
        params,
        card: {},
        extra_prompt: extra.extraPrompt || '',
        extra_mode: extra.extraMode || 'append',
        locked: extra.locked || [],
      }),
    }),

  // 上传走 FormData，所以不能复用 request()（那会强制 Content-Type: application/json）
  upload: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const resp = await fetch(apiPath('/api/chat/upload'), {
      method: 'POST',
      headers: authHeaders(),   // 不设 Content-Type：让浏览器自己带 multipart 边界
      body: form,
    })
    if (!resp.ok) {
      let msg = '图片上传没有成功，请稍后再试'
      try {
        const b = await resp.json()
        msg = b?.detail?.message || b?.detail || msg
      } catch { /* ignore */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return resp.json()
  },

  // ── 局部修复 / AI 找问题（外科修复直通，均走 FormData）─────────
  // 参考图 = 刚生成的那张成品（前端把 result.url 传回来），只改用户点名的地方
  // opts: { familyId, extraPrompt } —— 让后端把「家族硬禁令 + 用户自定义提示词」
  //       重申进修复指令，修复不许破坏用户定下的规矩
  repair: async (change, reference, threadId = 'studio', opts = {}) => {
    const form = new FormData()
    form.append('change', change)
    form.append('reference', reference)
    form.append('thread_id', threadId)
    if (opts.familyId) form.append('family_id', opts.familyId)
    if (opts.extraPrompt) form.append('extra_prompt', opts.extraPrompt)
    const resp = await fetch(apiPath('/api/image/repair'), {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    })
    if (!resp.ok) {
      let msg = '修复没有成功，请稍后再试'
      try {
        const b = await resp.json()
        msg = b?.detail?.message || b?.detail || msg
      } catch { /* ignore */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return resp.json()
  },

  // VLM 对比原图与成品，返回漂移候选 [{change, kind: 'drift'|'adaptation', why}]
  // familyId 让 VLM 知道模板会刻意加什么（否则模板元素会被误判成"建议修"）
  diagnose: async (original, generated, familyId = '') => {
    const form = new FormData()
    form.append('original', original)
    form.append('generated', generated)
    if (familyId) form.append('family_id', familyId)
    const resp = await fetch(apiPath('/api/image/diagnose'), {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    })
    if (!resp.ok) {
      let msg = 'AI 找问题没有成功，请稍后再试'
      try {
        const b = await resp.json()
        msg = b?.detail?.message || b?.detail || msg
      } catch { /* ignore */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return resp.json()
  },

  resetQuota: (threadId) =>
    request(`/api/chat/reset-quota?thread_id=${encodeURIComponent(threadId)}`, {
      method: 'POST',
    }),

  // ── 身份：访客码 / 绑定账号 / 登录 / 找回（公开版）─────────
  // 当前会话状态：{session_id, code(6位访客码), account, logged_in}
  authMe: () => request('/api/auth/me'),
  // 给当前会话绑定账号密码（绑定后可用账密在任何设备登录回来）
  authBind: (username, password, hint = '') =>
    request('/api/auth/bind', {
      method: 'POST',
      body: JSON.stringify({ username, password, hint }),
    }),
  // 账密登录 → {session_id, token}
  authLogin: (username, password) =>
    request('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  // 凭 6 位访客码找回会话 → {session_id, token}
  sessionRestore: (code) =>
    request('/api/session/restore', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
}

/**
 * SSE 对话 —— 把 Tool-use Loop 的每一步实时推出来
 *
 * 为什么不用 fetch + 手动解析：EventSource 只支持 GET。
 * 这里坚持 POST（消息体里要带 card / 参数），所以手读 ReadableStream。
 */
export async function streamChat({
  message, threadId, imageUrl, card, allowSpend, onEvent, signal,
  extraPrompt = '', extraMode = 'append',
}) {
  const resp = await fetch(apiPath('/api/chat/stream'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      message,
      thread_id: threadId,
      image_url: imageUrl || '',
      card: card || {},
      allow_spend: allowSpend,
      // 用户自定义提示词：请求级字段，原样进最终 prompt
      extra_prompt: extraPrompt,
      extra_mode: extraMode,
    }),
    signal,
  })
  if (!resp.ok) {
    // SSE 端点的错误体是 {detail: {code, message}}，把 message 提出来 ——
    // 否则限流时用户只看到干巴巴的状态码（复审 D-5）
    let msg = '对话暂时没有连上，请稍等片刻再试'
    try {
      const body = await resp.json()
      msg = body?.detail?.message || body?.detail || body?.error || msg
    } catch { /* 非 JSON 就保留兜底 */ }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finalPayload = null      // ← done 事件里的真实结果

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const frames = buffer.split('\n\n')
    buffer = frames.pop() || ''
    for (const frame of frames) {
      const evLine = frame.split('\n').find((l) => l.startsWith('event: '))
      const dataLine = frame.split('\n').find((l) => l.startsWith('data: '))
      if (!evLine || !dataLine) continue
      const event = evLine.slice(7).trim()
      let data = {}
      try { data = JSON.parse(dataLine.slice(6)) } catch { /* 忽略脏帧 */ }
      if (event === 'done') finalPayload = data
      if (event === 'close') return finalPayload ?? data
      onEvent?.(event, data)
    }
  }
  return finalPayload ?? {}
}

/* ══════════════ 异步对话（后台任务 + 轮询）══════════════
 *
 * ★ 为什么不再用 SSE（2026-10-03 线上事故）：
 *   托管平台的反向代理对每个 HTTP 请求有 **60 秒硬超时**，超过直接 504。
 *   而一次真实出图是「对话 + 视觉模型提炼 + 生图」，实测 1~7 分钟 ——
 *   只要它撑在一根 SSE 长连接里，云端必然掐断，表现为
 *   「一直卡在 extract_card，然后弹错」。
 *   本机直连没有反代，所以这个问题本地永远复现不了。
 *
 *   改成：POST 立刻拿到 task_id → 轮询 GET。每个请求都是毫秒级，
 *   60 秒限制从原理上失效，而且一点点质量都不用让。
 */
export async function chatAsync(payload) {
  return request('/api/chat/async', {
    method: 'POST',
    body: JSON.stringify({
      message: payload.message,
      thread_id: payload.threadId || 'default',
      image_url: payload.imageUrl || '',
      card: payload.card || {},
      allow_spend: payload.allowSpend !== false,
      extra_prompt: payload.extraPrompt || '',
      extra_mode: payload.extraMode || 'append',
    }),
  })
}

// 轮询任务：返回 {status, cursor, events, text, text_len, result, error}
export async function chatTask(taskId, cursor = 0) {
  return request(
    `/api/chat/tasks/${encodeURIComponent(taskId)}?cursor=${cursor}`,
  )
}

export async function chatTaskCancel(taskId) {
  return request(`/api/chat/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
  })
}

export async function chatTasksActive() {
  return request('/api/chat/tasks/active')
}
