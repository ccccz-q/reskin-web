// SSE / 轮询事件 → UI 状态更新的纯映射层。
//
// ★ 为什么要从 hook 里搬出来：handleEvent 是整个前端最容易出 bug 的地方
//   —— 它把十几种事件（tool_start / tool_end / token / image / done /
//   finish / error）映射到 6 个状态源上，还夹着两个 ref（genToolOk /
//   doneUrl）和三个副作用（flash / 滚动 / 刷额度）。埋在组件里就只能靠
//   手点页面验证，重复事件与乱序事件根本构造不出来。
//
//   现在它是一个纯函数：吃(state, event, data)，吐出「新状态 + 要执行的
//   副作用描述」。副作用本身（flash / scrollIntoView / refreshQuota）由调用方
//   执行，所以这个文件不需要 DOM，能直接在 Node 里测。
//
// ★ 行为契约：state 里没被这个事件改动的键，不会出现在返回的 patch 里。
//   hook 层据此只 set 被改动的字段，避免「每来一个 token 就重设 trace」这种
//   无谓渲染。

/** 新一轮流开始时的初始状态（每次 runStream 重置）。 */
export function initialStreamState() {
  return {
    trace: [],
    messages: [],
    result: null,
    undoStack: [],
    // 本轮出图工具是否成功返回过（兜底对账用）
    genToolOk: false,
    // done / image 事件是否带来了 image_url
    doneUrl: null,
  }
}

/** tool_start：追加一行「执行中」。同名工具可以有多行并存（按出现顺序配对）。 */
function applyToolStart(state, data) {
  return { trace: [...state.trace, { name: data.name, state: 'running' }] }
}

/**
 * tool_end：把**第一行**同名的 running 改成 done / fail。
 * 找不到对应 running 行时（事件乱序、或 start 丢了）补push 一行——
 *   否则这次工具调用会从轨迹里凭空消失，用户以为它没跑过。
 */
function applyToolEnd(state, data) {
  const next = [...state.trace]
  const i = next.findIndex((x) => x.name === data.name && x.state === 'running')
  const row = { name: data.name, state: data.ok ? 'done' : 'fail', summary: data.summary }
  if (i >= 0) next[i] = row
  else next.push(row)
  const patch = { trace: next }
  // ★ 只有「出图工具 + 成功」才置位：出图工具失败时不能触发兜底对账，
  //   否则会拿仓库里上一张旧图当成本次结果摆上画布。
  if (data.ok && data.name === 'generate_image') patch.genToolOk = true
  return patch
}

/**
 * token增量：优先接在**最后一条流式 assistant** 后面。
 * 只要最后一条不是「流式中的 assistant」（比如用户又发了一句、或上一轮已收尾），
 * 就新起一条 —— 这样「轮到的文案」和「上一句回答」不会粘成一段。
 */
function applyToken(state, data) {
  const m = state.messages
  const last = m[m.length - 1]
  if (last?.role === 'assistant' && last.streaming) {
    const copy = [...m]
    copy[copy.length - 1] = { ...last, text: last.text + data.text }
    return { messages: copy }
  }
  return { messages: [...m, { role: 'assistant', text: data.text, streaming: true }] }
}

/** finish：所有还在流式中的消息收尾。重复调用是安全的（没有 streaming 就原样返回）。 */
function applyFinish(state) {
  return { messages: state.messages.map((x) => (x.streaming ? { ...x, streaming: false } : x)) }
}

/**
 * image：图一落盘就到了（不必等收尾 LLM 讲完）—— 先上画布，文案继续跑。
 * 与 done 分支共用同一段结果构造：图片先到、结果不重画、滚动只发生一次。
 */
function applyImage(state, data) {
  return {
    // ★ `|| state.doneUrl`：重复的 image 事件（重试/ 双通道）不带url 时，
    //   不能把已经记住的地址抹成 null，否则兜底对账会误判「done 从没来过」。
    doneUrl: data.url || state.doneUrl,
    result: { url: data.url, family_id: data.family_id, size: data.size },
    undoStack: [],       // ★ 全新一张图的开始，旧版本的回退栈不再有意义
    effects: [{ type: 'scroll' }, ...(data.aspect_warning ? [{ type: 'flash', msg: data.aspect_warning }] : [])],
  }
}

/**
 * done：终态。一条事件要同时承担四件事，任一件漏掉用户都会遇到"卡住了"。
 *   ① 记住 image_url（哪怕为空，否则兜底对账永远不触发）
 *   ② 上画布 + 滚回画布（实测用户诉求：生成中用户往下看轨迹，完成后停在底部）
 *   ③ agent 切了家族 → 跟着切，并清掉锁定集（旧参数名泄漏到新家族会误跳兜底）
 *   ④ 画幅偏差 / 错误文案 + 刷额度徽标
 */
function applyDone(state, data, ctx) {
  const patch = { doneUrl: data.image_url || null }
  const effects = []
  if (data.image_url) {
    patch.undoStack = []      // ★ 同上：新一轮出图，回退栈清零
    patch.result = { url: data.image_url, family_id: data.family_id, size: data.size }
    effects.push({ type: 'scroll' })
  }
  if (data.aspect_warning) effects.push({ type: 'flash', msg: data.aspect_warning })  // 画幅偏差（最后一道闸）
  if (data.family_id && data.family_id !== ctx.familyId) {
    patch.familyId = data.family_id
    patch.params = data.params || {}
    patch.clearLocked = true
  }
  if (data.error) effects.push({ type: 'flash', msg: data.error })
  // 额度徽标必须跟着走：这一轮对话可能花过额度，不刷就是显示假数字
  effects.push({ type: 'quota' })
  patch.effects = effects
  return patch
}

/**
 * 把一个事件映射成状态补丁。
 *
 * @param state  当前流状态（initialStreamState 的形状）
 * @param event  事件名；不认识的事件一律忽略（返回空补丁），前向兼容后端新增类型
 * @param data   事件载荷；缺失时按 {} 处理，不抛
 * @param ctx    { familyId } —— 用来判断 done 是否换了家族
 * @returns { ...补丁字段, effects?: [] }  空补丁表示「这个事件不改状态」
 */
export function applyEvent(state, event, data, ctx = {}) {
  const d = data || {}
  switch (event) {
    case 'tool_start': return applyToolStart(state, d)
    case 'tool_end': return applyToolEnd(state, d)
    case 'token':
      // ★ 空文本必须整条忽略：token 事件可能带空串（心跳占位），
      //   若也进messages 会多出一条空气泡。
      return d.text ? applyToken(state, d) : {}
    case 'finish': return applyFinish(state)
    case 'image': return applyImage(state, d)
    case 'done': return applyDone(state, d, ctx)
    case 'error':
      return { effects: [{ type: 'flash', msg: d.error || '对话出错' }] }
    default: return {}       // 未知事件：忽略但不抛，后端加新类型不会打挂前端
  }
}

/**
 * 轮询的增量切片：后端给累积全文，前端只把新增的一段喂给事件处理器。
 * 返回 null 表示「这轮没有新文本」（已渲染过or 空串），调用方不要重复喂。
 *
 * 为什么要按字符长度切而不是按行/按事件：后端 text 是完整快照，
 * 两次快照的长度差就是这一轮真正新增的文本 —— 事件列表可能重复投递，
 * 文本快照不会，所以它是唯一可靠的进度尺子。
 */
export function sliceNewText(fullText, renderedLen) {
  if (!fullText || fullText.length <= renderedLen) return null
  return fullText.slice(renderedLen)
}

/** 轮询是否该结束：只有终态才break，running / queued 都要继续等。 */
export function isTerminalStatus(status) {
  return status !== 'running' && status !== 'queued'
}

/** 终态 → 收尾动作。cancelled 不当作失败（用户主动中止，不弹错误）。 */
export function finalizeFromStatus(status) {
  if (status === 'cancelled') return { kind: 'aborted' }
  if (status === 'failed') return { kind: 'failed' }
  return { kind: 'done' }
}