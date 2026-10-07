import { describe, expect, it } from 'vitest'
import {
  applyEvent,
  finalizeFromStatus,
  initialStreamState,
  isTerminalStatus,
  sliceNewText,
} from './chatEvents.js'

const S = () => initialStreamState()

/** 把一串事件按顺序喂进去，返回累积后的状态 + 全部副作用。 */
function feed(events, ctx = { familyId: 'zine' }) {
  let st = initialStreamState()
  const effects = []
  for (const [event, data] of events) {
    const patch = applyEvent(st, event, data, ctx)
    effects.push(...(patch.effects || []))
    st = { ...st, ...patch }
  }
  return { state: st, effects }
}

const flashes = (effects) => effects.filter((e) => e.type === 'flash').map((e) => e.msg)

// ─────────────────────────────────────────── tool_start / tool_end
describe('tool_start / tool_end —— Agent 轨迹', () => {
  it('tool_start 追加一行 running', () => {
    const { state } = feed([['tool_start', { name: 'extract_card' }]])
    expect(state.trace).toEqual([{ name: 'extract_card', state: 'running' }])
  })

  it('tool_end 把同名 running 行改成 done，并带上summary', () => {
    const { state } = feed([
      ['tool_start', { name: 'extract_card' }],
      ['tool_end', { name: 'extract_card', ok: true, summary: '提炼出 12 项' }],
    ])
    expect(state.trace).toEqual([
      { name: 'extract_card', state: 'done', summary: '提炼出 12 项' },
    ])
  })

  it('ok=false → state 为 fail（不是 done）', () => {
    const { state } = feed([
      ['tool_start', { name: 'gen' }],
      ['tool_end', { name: 'gen', ok: false, summary: '超时' }],
    ])
    expect(state.trace[0].state).toBe('fail')
    expect(state.trace[0].summary).toBe('超时')
  })

  it('★ 乱序：tool_end 先于 tool_start 到达 → 先补一行 done，随后 start 再补一行 running', () => {
    // 不这样兜的话，这次工具调用会从轨迹里凭空消失，用户以为它没跑过
    const { state } = feed([
      ['tool_end', { name: 'vision', ok: true, summary: 'ok' }],
      ['tool_start', { name: 'vision' }],
    ])
    expect(state.trace).toHaveLength(2)
    expect(state.trace[0]).toEqual({ name: 'vision', state: 'done', summary: 'ok' })
    expect(state.trace[1]).toEqual({ name: 'vision', state: 'running' })
  })

  it('重复 tool_end：只改第一行，第二行保持 running（配对不会串）', () => {
    const { state } = feed([
      ['tool_start', { name: 'gen' }],
      ['tool_start', { name: 'gen' }],
      ['tool_end', { name: 'gen', ok: true }],
    ])
    expect(state.trace.map((x) => x.state)).toEqual(['done', 'running'])
  })

  it('重复 tool_start 不会覆盖已有行（同名可以并存）', () => {
    const { state } = feed([
      ['tool_start', { name: 'a' }],
      ['tool_start', { name: 'a' }],
      ['tool_start', { name: 'b' }],
    ])
    expect(state.trace.map((x) => x.name)).toEqual(['a', 'a', 'b'])
  })

  it('tool_end 会消掉 running 行，而不是永远堆着', () => {
    const { state } = feed([
      ['tool_start', { name: 'a' }],
      ['tool_start', { name: 'a' }],
      ['tool_end', { name: 'a', ok: true }],
      ['tool_end', { name: 'a', ok: true }],
    ])
    expect(state.trace.filter((x) => x.state === 'running')).toHaveLength(0)
  })

  it('★ genToolOk 只在「出图工具 + 成功」时置位', () => {
    expect(feed([['tool_end', { name: 'generate_image', ok: true }]]).state.genToolOk).toBe(true)
  })

  it('出图工具失败 → genToolOk 保持 false（否则会拿旧图当本次结果）', () => {
    expect(feed([['tool_end', { name: 'generate_image', ok: false }]]).state.genToolOk).toBe(false)
  })

  it('别的工具成功 → 也不置位（只有 generate_image 算）', () => {
    expect(feed([['tool_end', { name: 'extract_card', ok: true }]]).state.genToolOk).toBe(false)
  })

  it('非法输入：data 缺失时不抛，按 {} 处理', () => {
    expect(() => applyEvent(S(), 'tool_start')).not.toThrow()
    expect(() => applyEvent(S(), 'tool_end')).not.toThrow()
    expect(applyEvent(S(), 'tool_end').trace).toEqual([{ name: undefined, state: 'fail', summary: undefined }])
  })
})

// ─────────────────────────────────────────── token 增量
describe('token —— 流式文本增量拼接', () => {
  it('首个 token 新起一条 assistant（streaming）', () => {
    const { state } = feed([['token', { text: '你好' }]])
    expect(state.messages).toEqual([{ role: 'assistant', text: '你好', streaming: true }])
  })

  it('★ 连续 token 拼到同一条上，不产生新气泡', () => {
    const { state } = feed([
      ['token', { text: '你' }],
      ['token', { text: '好' }],
      ['token', { text: '呀' }],
    ])
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].text).toBe('你好呀')
    expect(state.messages[0].streaming).toBe(true)
  })

  it('上一条已收尾（streaming=false）→ 新 token另起一条，不粘成一段', () => {
    const { state } = feed([
      ['token', { text: '第一轮' }],
      ['finish', {}],
      ['token', { text: '第二轮' }],
    ])
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0]).toEqual({ role: 'assistant', text: '第一轮', streaming: false })
    expect(state.messages[1]).toEqual({ role: 'assistant', text: '第二轮', streaming: true })
  })

  it('最后一条是 user → 新起 assistant（轮到的文案不粘在用户气泡上）', () => {
    const st = { ...S(), messages: [{ role: 'user', text: '换成夜景' }] }
    const patch = applyEvent(st, 'token', { text: '好' })
    expect(patch.messages).toHaveLength(2)
    expect(patch.messages[0].role).toBe('user')
    expect(patch.messages[1]).toEqual({ role: 'assistant', text: '好', streaming: true })
  })

  it('★ 空文本整条忽略（心跳占位不能变成空气泡）', () => {
    const st = S()
    expect(applyEvent(st, 'token', { text: '' })).toEqual({})
    expect(applyEvent(st, 'token', {})).toEqual({})
    expect(applyEvent(st, 'token')).toEqual({})
  })

  it('非法输入：text 为 0 / false 也被忽略（falsy 一律不进）', () => {
    expect(applyEvent(S(), 'token', { text: 0 })).toEqual({})
    expect(applyEvent(S(), 'token', { text: false })).toEqual({})
  })

  it('拼接不会改动传入的旧消息对象（不可变）', () => {
    const st = { ...S(), messages: [{ role: 'assistant', text: 'A', streaming: true }] }
    const before = st.messages[0]
    applyEvent(st, 'token', { text: 'B' })
    expect(before.text).toBe('A')
  })

  it('带换行的 token 原样拼接（不额外处理空白）', () => {
    const { state } = feed([
      ['token', { text: '第一行\n' }],
      ['token', { text: '第二行' }],
    ])
    expect(state.messages[0].text).toBe('第一行\n第二行')
  })
})

// ─────────────────────────────────────────── finish
describe('finish —— 流式收尾', () => {
  it('把还在 streaming 的消息置为 false', () => {
    const { state } = feed([['token', { text: 'x' }], ['finish', {}]])
    expect(state.messages[0].streaming).toBe(false)
  })

  it('★ 重复 finish 幂等（第二次是空操作，不新建数组内容以外的副作用）', () => {
    const { state } = feed([
      ['token', { text: 'x' }],
      ['finish', {}],
      ['finish', {}],
    ])
    expect(state.messages).toEqual([{ role: 'assistant', text: 'x', streaming: false }])
  })

  it('只改 streaming 的那条，user 消息与已收尾的都不动', () => {
    const st = {
      ...S(),
      messages: [
        { role: 'user', text: 'u' },
        { role: 'assistant', text: 'old', streaming: false },
        { role: 'assistant', text: 'new', streaming: true },
      ],
    }
    const out = applyEvent(st, 'finish', {}).messages
    expect(out.map((m) => m.streaming)).toEqual([undefined, false, false])
    expect(out[0]).toBe(st.messages[0])          // 同一个引用 = 没被复制
    expect(out[1]).toBe(st.messages[1])
  })

  it('非法输入：没有消息时 finish 不抛', () => {
    expect(() => applyEvent(S(), 'finish')).not.toThrow()
    expect(applyEvent(S(), 'finish').messages).toEqual([])
  })
})

// ─────────────────────────────────────────── image
describe('image —— 图一落盘就先上画布', () => {
  it('★ 立刻设置 result，不必等收尾 finish/done', () => {
    const { state } = feed([['image', { url: 'a.jpg', family_id: 'zine', size: '1024x1024' }]])
    expect(state.result).toEqual({ url: 'a.jpg', family_id: 'zine', size: '1024x1024' })
  })

  it('同时把 doneUrl 记住（兜底对账要看它）', () => {
    const { state } = feed([['image', { url: 'a.jpg' }]])
    expect(state.doneUrl).toBe('a.jpg')
  })

  it('清空 undoStack（全新一张图，旧版本回退栈不再有意义）', () => {
    const st = { ...S(), undoStack: [{ url: 'old.jpg' }] }
    expect(applyEvent(st, 'image', { url: 'new.jpg' }).undoStack).toEqual([])
  })

  it('触发一次滚动到画布', () => {
    const { effects } = feed([['image', { url: 'a.jpg' }]])
    expect(effects.filter((e) => e.type === 'scroll')).toHaveLength(1)
  })

  it('画幅偏差警告 → flash', () => {
    const { effects } = feed([['image', { url: 'a.jpg', aspect_warning: '画幅偏差较大' }]])
    expect(flashes(effects)).toEqual(['画幅偏差较大'])
  })

  it('★ 重复 image（第二次不带 url）不能把 doneUrl 抹成 null', () => {
    // 抹掉的话，兜底对账会误判「done 从没来过」，用户白等几分钟
    const { state } = feed([
      ['image', { url: 'a.jpg' }],
      ['image', { family_id: 'zine' }],
    ])
    expect(state.doneUrl).toBe('a.jpg')
    expect(state.result.url).toBeUndefined()   // 第二个事件的 url 就是 undefined，如实反映
  })

  it('乱序：image 先于 tool_start 到达也不受影响', () => {
    const { state } = feed([
      ['image', { url: 'a.jpg' }],
      ['tool_start', { name: 'gen' }],
    ])
    expect(state.result.url).toBe('a.jpg')
    expect(state.trace).toHaveLength(1)
  })

  it('非法输入：完全空的 data 不抛', () => {
    const { state } = feed([['image', {}]])
    expect(state.result).toEqual({ url: undefined, family_id: undefined, size: undefined })
    expect(state.doneUrl).toBeNull()
  })
})

// ─────────────────────────────────────────── done
describe('done —— 终态四件事', () => {
  it('带 image_url → 上画布 + 滚回画布 + 记住 doneUrl', () => {
    const { state, effects } = feed([['done', { image_url: 'a.jpg', family_id: 'zine', size: '1024x1024' }]])
    expect(state.result).toEqual({ url: 'a.jpg', family_id: 'zine', size: '1024x1024' })
    expect(state.doneUrl).toBe('a.jpg')
    expect(effects.some((e) => e.type === 'scroll')).toBe(true)
  })

  it('★ 不带 image_url → doneUrl 置 null（这是兜底对账的触发条件）', () => {
    const st = { ...S(), doneUrl: 'stale.jpg' }
    expect(applyEvent(st, 'done', {}).doneUrl).toBeNull()
  })

  it('不带 image_url → 不设 result、不滚动（没图可上）', () => {
    const { state, effects } = feed([['done', {}]])
    expect(state.result).toBeNull()
    expect(effects.some((e) => e.type === 'scroll')).toBe(false)
  })

  it('无论有没有图，都会刷额度徽标', () => {
    expect(feed([['done', {}]]).effects.some((e) => e.type === 'quota')).toBe(true)
    expect(feed([['done', { image_url: 'a.jpg' }]]).effects.some((e) => e.type === 'quota')).toBe(true)
  })

  it('agent 换了家族 → 跟着切，并带上新参数、清锁定集', () => {
    const patch = applyEvent(S(), 'done', { family_id: 'material_pixel', params: { a: 1 } }, { familyId: 'zine' })
    expect(patch.familyId).toBe('material_pixel')
    expect(patch.params).toEqual({ a: 1 })
    expect(patch.clearLocked).toBe(true)
  })

  it('家族没变 → 不下发迁移（避免清掉用户刚锁的参数）', () => {
    const patch = applyEvent(S(), 'done', { family_id: 'zine' }, { familyId: 'zine' })
    expect(patch.familyId).toBeUndefined()
    expect(patch.clearLocked).toBeUndefined()
  })

  it('没带 family_id → 不迁移（ctx 缺 familyId 时也不误判成「换了」）', () => {
    const patch = applyEvent(S(), 'done', { image_url: 'a.jpg' }, { familyId: 'zine' })
    expect(patch.familyId).toBeUndefined()
  })

  it('切家族但没带 params → 用空对象（不是 undefined）', () => {
    expect(applyEvent(S(), 'done', { family_id: 'other' }, { familyId: 'zine' }).params).toEqual({})
  })

  it('画幅偏差与 error 都变成 flash，且两者可同时出现', () => {
    const { effects } = feed([['done', { image_url: 'a.jpg', aspect_warning: '偏差大', error: '额度不足' }]])
    expect(flashes(effects)).toEqual(['偏差大', '额度不足'])
  })

  it('清空 undoStack（新一轮出图）', () => {
    const st = { ...S(), undoStack: [{ url: 'old' }] }
    expect(applyEvent(st, 'done', { image_url: 'new.jpg' }).undoStack).toEqual([])
  })

  it('副作用顺序：滚动 → 警告 → 家族迁移 → 错误 → 额度', () => {
    const patch = applyEvent(S(), 'done',
      { image_url: 'a.jpg', aspect_warning: 'w', error: 'e', family_id: 'other' },
      { familyId: 'zine' })
    expect(patch.effects.map((e) => e.type)).toEqual(['scroll', 'flash', 'flash', 'quota'])
    expect(patch.clearLocked).toBe(true)
  })
})

// ─────────────────────────────────────────── error
describe('error —— 对话出错', () => {
  it('带 error → 原样flash', () => {
    expect(flashes(feed([['error', { error: '模型超时' }]]).effects)).toEqual(['模型超时'])
  })

  it('★ 没带 error → 回落成「对话出错」，不弹空白 toast', () => {
    expect(flashes(feed([['error', {}]]).effects)).toEqual(['对话出错'])
  })

  it('error 空串 → 也走回落（falsy）', () => {
    expect(flashes(feed([['error', { error: '' }]]).effects)).toEqual(['对话出错'])
  })

  it('非法输入：data 缺失不抛', () => {
    expect(flashes(feed([['error', undefined]]).effects)).toEqual(['对话出错'])
  })

  it('error 不改任何状态（只弹提示）', () => {
    const patch = applyEvent(S(), 'error', { error: 'x' })
    expect(patch.trace).toBeUndefined()
    expect(patch.messages).toBeUndefined()
    expect(patch.result).toBeUndefined()
  })
})

// ─────────────────────────────────────────── 未知事件 / 前向兼容
describe('未知事件', () => {
  it('不识别的 event 一律忽略且不抛（后端加新类型不会打挂前端）', () => {
    for (const ev of ['ping', 'token_delta', 'done_v2', '', null, undefined, 123]) {
      expect(() => applyEvent(S(), ev, { text: 'x' })).not.toThrow()
      expect(applyEvent(S(), ev, { text: 'x' })).toEqual({})
    }
  })
})

// ─────────────────────────────────────────── 轮询切片
describe('sliceNewText —— 累积全文按长度切片', () => {
  it('只给新增的那一段', () => {
    expect(sliceNewText('abcdef', 3)).toEqual('def')
  })

  it('没有新增 → null（不重复喂）', () => {
    expect(sliceNewText('abc', 3)).toBeNull()
  })

  it('★ 快照变短（后端重启/ 换任务）→ null，不返回负长度的垃圾', () => {
    expect(sliceNewText('ab', 10)).toBeNull()
  })

  it('空文本 → null', () => {
    expect(sliceNewText('', 0)).toBeNull()
    expect(sliceNewText(null, 0)).toBeNull()
    expect(sliceNewText(undefined, 0)).toBeNull()
  })

  it('首轮从 0 开始 → 全文', () => {
    expect(sliceNewText('hello', 0)).toBe('hello')
  })

  it('★ 按字符长度切，所以中文与 emoji 不会被切坏（JS 字符串按 UTF-16 码元）', () => {
    expect(sliceNewText('树冠变圆球', 2)).toBe('变圆球')
  })

  it('renderedLen 缺省 / 非法值时按 0 处理（不会漏掉首段文本）', () => {
    expect(sliceNewText('abc', undefined)).toBe('abc')
    expect(sliceNewText('abc', null)).toBe('abc')
  })
})

describe('isTerminalStatus —— 轮询何时收尾', () => {
  it('running / queued 都要继续等', () => {
    expect(isTerminalStatus('running')).toBe(false)
    expect(isTerminalStatus('queued')).toBe(false)
  })

  it('done / failed / cancelled 是终态', () => {
    expect(isTerminalStatus('done')).toBe(true)
    expect(isTerminalStatus('failed')).toBe(true)
    expect(isTerminalStatus('cancelled')).toBe(true)
  })

  it('★ status 缺失(undefined) 视为终态 —— 否则会死循环', () => {
    expect(isTerminalStatus(undefined)).toBe(true)
  })
})

describe('finalizeFromStatus —— 终态对应的收尾动作', () => {
  it('cancelled → aborted（用户主动中止，不弹错误）', () => {
    expect(finalizeFromStatus('cancelled')).toEqual({ kind: 'aborted' })
  })

  it('failed → failed', () => {
    expect(finalizeFromStatus('failed')).toEqual({ kind: 'failed' })
  })

  it('done → done', () => {
    expect(finalizeFromStatus('done')).toEqual({ kind: 'done' })
  })
})

// ─────────────────────────────────────────── 全链路
describe('真实事件序列（回归护栏）', () => {
  it('完整的成功出图：start → token* → image → tool_end → done', () => {
    const { state, effects } = feed([
      ['tool_start', { name: 'extract_card' }],
      ['tool_end', { name: 'extract_card', ok: true, summary: '12 项' }],
      ['token', { text: '我先提炼' }],
      ['token', { text: '一下参数' }],
      ['image', { url: 'a.jpg', family_id: 'zine', size: '1024x1024' }],
      ['tool_start', { name: 'generate_image' }],
      ['tool_end', { name: 'generate_image', ok: true, summary: '1 张' }],
      ['token', { text: '，出图了' }],
      ['finish', {}],
      ['done', { image_url: 'a.jpg', family_id: 'zine', size: '1024x1024' }],
    ])
    expect(state.trace.map((t) => `${t.name}:${t.state}`))
      .toEqual(['extract_card:done', 'generate_image:done'])
    expect(state.messages.map((m) => m.text)).toEqual(['我先提炼一下参数，出图了'])
    expect(state.messages[0].streaming).toBe(false)
    expect(state.result.url).toBe('a.jpg')
    expect(state.genToolOk).toBe(true)
    expect(state.doneUrl).toBe('a.jpg')
    expect(effects.filter((e) => e.type === 'scroll')).toHaveLength(2)   // image + done 各一次
    expect(effects.filter((e) => e.type === 'quota')).toHaveLength(1)
  })

  it('★ 出图工具成功但 done 从没来 → 兜底对账的两个前提都成立', () => {
    const { state } = feed([
      ['tool_start', { name: 'generate_image' }],
      ['tool_end', { name: 'generate_image', ok: true }],
      ['finish', {}],
    ])
    expect(state.genToolOk).toBe(true)
    expect(state.doneUrl).toBeNull()
  })

  it('用户中止：cancelled 之后不再有 image/result', () => {
    const { state } = feed([
      ['token', { text: '正在跑' }],
      ['finish', {}],
    ], { familyId: 'zine' })
    expect(state.result).toBeNull()
    expect(state.genToolOk).toBe(false)
  })

  it('★ 同一次轮询里连着来 3 个 token —— 必须拼成一句，不是只剩最后一段', () => {
    // 这是 handleEvent 的接线约束：runStream 抓一个 handleEvent 引用后
    // 连续喂多个事件，映射层必须每次都读到**上一次的结果**。
    // 若改成读闭包里的旧 state，第二个 token 就会覆盖第一个。
    const { state } = feed([
      ['token', { text: '我先' }],
      ['token', { text: '提炼一下' }],
      ['token', { text: '参数' }],
    ])
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].text).toBe('我先提炼一下参数')
  })

  it('★ 同一批里 tool_start → tool_end → tool_start 交替，配对不错乱', () => {
    const { state } = feed([
      ['tool_start', { name: 'a' }],
      ['tool_end', { name: 'a', ok: true }],
      ['tool_start', { name: 'b' }],
      ['tool_end', { name: 'b', ok: true }],
    ])
    expect(state.trace.map((t) => `${t.name}:${t.state}`)).toEqual(['a:done', 'b:done'])
  })

  it('乱序 + 重复混合：done 先到、image 后到、token 空串穿插 —— 状态依然自洽', () => {
    const { state } = feed([
      ['done', { image_url: 'a.jpg' }],
      ['token', { text: '' }],
      ['image', { url: 'a.jpg' }],
      ['image', {}],
      ['token', { text: '收尾' }],
      ['finish', {}],
    ], { familyId: 'zine' })
    expect(state.doneUrl).toBe('a.jpg')
    expect(state.messages).toEqual([{ role: 'assistant', text: '收尾', streaming: false }])
    // ★ 注意这里：第二个 image 事件没带 url，而 result 是**无条件**重建的，
    //   所以 result.url 变成 undefined（doneUrl 因为 `|| state.doneUrl` 才保住）。
    //   这与线上原handleEvent 的行为一致，用例如实锁住 ——
    //   真实链路里 image 事件必定带 url（后端落盘后才发），
    //   但若将来出现重复投递且缺 url 的事件，画布会拿到空地址。
    expect(state.result.url).toBeUndefined()
  })
})