import { describe, expect, it } from 'vitest'
import {
  FALLBACK_CAT,
  MAX_REPAIR_LINES,
  REPAIR_CATS,
  guessCat,
  nextRepairNote,
  nextResultAfterRepair,
  pickDrifts,
  popUndoEntry,
  pushUndoEntry,
  repairBlockedReason,
  splitRepairLines,
  toggleCat,
  withGuessedCat,
} from './repair.js'

describe('guessCat —— 从自然语言猜修复维度', () => {
  it('把明确命中关键词的描述归到对应维度', () => {
    expect(guessCat('构图有点挤')).toBe('构图与视角')
    expect(guessCat('阴影太重了')).toBe('光线层级')
    expect(guessCat('材质看着像塑料')).toBe('材质与质感')
    expect(guessCat('右上角的水印没去掉')).toBe('商业化感')
    expect(guessCat('手指的数量不对')).toBe('人物姿态')
  })

  it('表是顺序敏感的：一个词命中多类时，靠前的类别先赢', () => {
    // 「光」在光线层级，「色」在色彩与曝光 —— 两者都命中，光线排前面所以赢
    expect(guessCat('光和色都很脏')).toBe('光线层级')
    // 反过来把光线表里没有的词补上：只有「色」命中
    expect(guessCat('饱和度爆表')).toBe('色彩与曝光')
    // 「曝光过度」同时在两张表里，仍然是光线层级先命中
    expect(guessCat('曝光过度')).toBe('光线层级')
  })

  it('完全无关的描述回落到「其他」，绝不返回 undefined', () => {
    expect(guessCat('随便整一下')).toBe(FALLBACK_CAT)
    expect(guessCat('zzzz')).toBe(FALLBACK_CAT)
  })

  it('边界：空值 / 空串 / 假值都必须安全回落，不抛', () => {
    for (const bad of [undefined, null, '', 0, false, NaN]) {
      expect(() => guessCat(bad)).not.toThrow()
      expect(guessCat(bad)).toBe(FALLBACK_CAT)
    }
  })

  it('非法输入：非字符串（数字/对象/数组）会抛 —— 锁住「它不是全防御的」这一事实', () => {
    // `text || ''` 只兜得住假值；123 是真值，t.includes 不存在 → TypeError。
    // 调用方永远是 textarea 的字符串，所以不必为它加防御；
    // 但要写明现状，避免有人误以为传什么都安全。
    for (const bad of [123, {}, true]) {
      expect(() => guessCat(bad)).toThrow(TypeError)
    }
    // 反例：数组也有 includes，所以不抛、且能匹配上 —— 说明这里靠的是
    // 「鸭子类型」而不是类型校验，写文档时别夸大成「已校验输入类型」
    expect(guessCat(['构图'])).toBe('构图与视角')
  })

  it('不会改动 REPAIR_CATS（猜维度是只读查询）', () => {
    const before = [...REPAIR_CATS]
    guessCat('构图')
    expect(REPAIR_CATS).toEqual(before)
  })
})

describe('toggleCat —— 维度多选', () => {
  it('勾上就加，再点就摘', () => {
    const once = toggleCat([], '构图与视角')
    expect(once).toEqual(['构图与视角'])
    expect(toggleCat(once, '构图与视角')).toEqual([])
  })

  it('保留既有勾选的相对顺序，新项追加在末尾', () => {
    expect(toggleCat(['光线层级'], '商业化感')).toEqual(['光线层级', '商业化感'])
  })

  it('不修改传入的数组（原数组必须保持不变）', () => {
    const cur = ['材质与质感']
    toggleCat(cur, '其他')
    expect(cur).toEqual(['材质与质感'])
  })

  it('非法输入：空数组起点可用；非法的 c 也能加进去（不做白名单校验）', () => {
    expect(toggleCat([], '不存在的维度')).toEqual(['不存在的维度'])
  })
})

describe('splitRepairLines —— textarea原文拆行', () => {
  it('去掉空行与行首尾空白', () => {
    expect(splitRepairLines('  树冠变圆球了 \n\n\n 删掉英文文案\n')).toEqual([
      '树冠变圆球了',
      '删掉英文文案',
    ])
  })

  it('边界：空串 / 纯换行 / 只有空白 → 空数组', () => {
    expect(splitRepairLines('')).toEqual([])
    expect(splitRepairLines('\n\n\n')).toEqual([])
    expect(splitRepairLines('   \n  \n')).toEqual([])
  })

  it('非法输入：null / undefined 不抛', () => {
    expect(splitRepairLines(null)).toEqual([])
    expect(splitRepairLines(undefined)).toEqual([])
  })
})

describe('nextRepairNote —— 点选候选：追加 / 再点取消 / 上限', () => {
  it('追加为新的一行', () => {
    expect(nextRepairNote('', '树冠变圆球了')).toBe('树冠变圆球了')
  })

  it('再点一次取消该行（幂等：点第三次回到没选的状态）', () => {
    const on = nextRepairNote('', 'A')
    const off = nextRepairNote(on, 'A')
    expect(off).toBe('')
    expect(nextRepairNote(off, 'A')).toBe(on)
  })

  it('已有其他行时，追加到末尾而不是覆盖', () => {
    expect(nextRepairNote('A', 'B')).toBe('A\nB')
  })

  it('追加时会顺手归一化旧行（去掉空行/空白）', () => {
    expect(nextRepairNote('  A  \n\n', 'B')).toBe('A\nB')
  })

  it(`超过 ${MAX_REPAIR_LINES} 处上限时返回 null（调用方据此弹提示）`, () => {
    const three = 'A\nB\nC'
    expect(splitRepairLines(three)).toHaveLength(MAX_REPAIR_LINES)
    expect(nextRepairNote(three, 'D')).toBeNull()
  })

  it('上限边界：正好第 3 处可以进，第 4 处被拒', () => {
    const one = nextRepairNote('', 'A')
    const two = nextRepairNote(one, 'B')
    const three = nextRepairNote(two, 'C')
    expect(three).toBe('A\nB\nC')
    expect(nextRepairNote(three, 'D')).toBeNull()
  })

  it('取消一處后又能再选新的（上限不是累计的）', () => {
    const two = 'A\nB'
    const three = nextRepairNote(two, 'C')
    const backToTwo = nextRepairNote(three, 'A')
    expect(backToTwo).toBe('B\nC')
    expect(nextRepairNote(backToTwo, 'D')).toBe('B\nC\nD')
  })

  it('已存在的一行在满上限时仍可取消（取消永远不被上限拦住）', () => {
    expect(nextRepairNote('A\nB\nC', 'B')).toBe('A\nC')
  })

  it('契约：txt 必须是调用方 trim 过的 —— 没trim 会被当成另一行', () => {
    // 原实现里 trim 发生在 pickDrift 里（String(d.change||'').trim()），
    // 这里的对照行也是 trim 过的，所以两边一致。若调用方忘了 trim，
    // 结果是「多了一行带空白的重复项」—— 用例锁住这个前提。
    const txt = '  A  '.trim()
    expect(txt).toBe('A')
    expect(nextRepairNote('A', txt)).toBe('')
    // 反证：真的不 trim 就会多出一行
    expect(nextRepairNote('A', '  A  ')).toBe('A\n  A  ')
  })

  it('空串：会追加一个空行 —— 但线上走不到（原pickDrift 先 if(!txt) return）', () => {
    // 这里如实锁住 reducer 的真实行为：空串不在 lines 里，长度也够，
    // 于是被当成一条正常内容拼上去，尾部多一个换行。
    // 生产路径不可达，因为 pickDrift 在调它之前就return 掉了空串。
    // 写这个用例是为了：若哪天有人去掉那层 early-return，这里会立刻变红。
    expect(nextRepairNote('A', '')).toBe('A\n')
    expect(splitRepairLines(nextRepairNote('A', ''))).toEqual(['A'])  // 语义上仍是那一处
  })
})

describe('withGuessedCat —— 顺手勾上猜出的维度', () => {
  it('没勾过就加', () => {
    expect(withGuessedCat([], '阴影太重')).toEqual(['光线层级'])
  })

  it('已勾过就不重复加（保证维度列表无重复项）', () => {
    const cur = ['光线层级']
    expect(withGuessedCat(cur, '阴影太重')).toBe(cur)   // 同一个引用：不产生新数组
  })

  it('猜不出时挂到「其他」', () => {
    expect(withGuessedCat(['构图与视角'], '随便整一下')).toEqual(['构图与视角', FALLBACK_CAT])
  })

  it('非法输入：txt 为空串也能用（会挂到「其他」）', () => {
    expect(withGuessedCat([], '')).toEqual([FALLBACK_CAT])
  })
})

describe('repairBlockedReason —— 修复前置四道闸门', () => {
  const ok = { busy: false, note: '改一处', quota: {}, result: { url: 'a.jpg' } }

  it('条件齐备时放行（返回 null）', () => {
    expect(repairBlockedReason(ok)).toBeNull()
  })

  it('busy 优先于其它原因（用户此刻最关心的是「等这次跑完」）', () => {
    expect(repairBlockedReason({ ...ok, busy: true, note: '', quota: { exhausted: true } }))
      .toBe('正在进行的任务结束后再微调')
  })

  it('没写内容 → 提示写清哪里不对', () => {
    expect(repairBlockedReason({ ...ok, note: '' }))
      .toBe('先写清哪里不对（可勾选维度 + 点选 AI 候选）')
    //只有空白与换行等同于没写
    expect(repairBlockedReason({ ...ok, note: '  \n\n ' }))
      .toBe('先写清哪里不对（可勾选维度 + 点选 AI 候选）')
  })

  it('额度用尽 → 明确说额度的事', () => {
    expect(repairBlockedReason({ ...ok, quota: { exhausted: true } })).toBe('本会话额度已用完')
  })

  it('没有成品图 → 提示先出图', () => {
    expect(repairBlockedReason({ ...ok, result: null })).toBe('先出一张图，才能对它做局部修复')
    expect(repairBlockedReason({ ...ok, result: { url: '' } })).toBe('先出一张图，才能对它做局部修复')
  })

  it('顺序：busy > 内容 > 额度 > 有无成品', () => {
    expect(repairBlockedReason({ busy: true, note: '', quota: { exhausted: true }, result: null }))
      .toBe('正在进行的任务结束后再微调')
    expect(repairBlockedReason({ busy: false, note: '', quota: { exhausted: true }, result: null }))
      .toBe('先写清哪里不对（可勾选维度 + 点选 AI 候选）')
    expect(repairBlockedReason({ busy: false, note: 'x', quota: { exhausted: true }, result: null }))
      .toBe('本会话额度已用完')
  })

  it('非法输入：quota 为 undefined 走可选链，不会抛', () => {
    expect(() => repairBlockedReason({ busy: false, note: 'x', result: { url: 'a' } })).not.toThrow()
    expect(repairBlockedReason({ busy: false, note: 'x', result: { url: 'a' } })).toBeNull()
  })
})

describe('undoStack —— 修复版本栈', () => {
  const r1 = { url: 'v1.jpg', size: '1024x1024', family_id: 'zine' }
  const r2 = { url: 'v2.jpg', size: '1024x1024', family_id: 'zine' }

  it('压栈保留 url / size / family_id 三个字段', () => {
    expect(pushUndoEntry([], r1)).toEqual([{ url: 'v1.jpg', size: '1024x1024', family_id: 'zine' }])
  })

  it('连续修复逐层压栈，栈顶是上一版', () => {
    let s = pushUndoEntry([], r1)
    s = pushUndoEntry(s, r2)
    expect(s).toHaveLength(2)
    expect(s[s.length - 1].url).toBe('v2.jpg')
  })

  it('回退弹出栈顶并返回新栈（栈内容不被改动）', () => {
    const s = pushUndoEntry(pushUndoEntry([], r1), r2)
    const out = popUndoEntry(s)
    expect(out.entry.url).toBe('v2.jpg')
    expect(out.stack).toHaveLength(1)
    expect(s).toHaveLength(2)          // 原栈不变
  })

  it('栈空时返回 null（调用方直接 return，不做任何事）', () => {
    expect(popUndoEntry([])).toBeNull()
  })

  it('反复回退到空栈后不再有上一版', () => {
    let s = pushUndoEntry([], r1)
    s = popUndoEntry(s).stack
    expect(popUndoEntry(s)).toBeNull()
  })

  it('非法输入：压入一个没有 url 的结果也要照实记录（不静默丢弃）', () => {
    // 记录下来是有意的：修坏的版本也能回去，丢了就真回不去了
    expect(pushUndoEntry([], { url: '', size: null, family_id: null }))
      .toEqual([{ url: '', size: null, family_id: null }])
  })
})

describe('pickDrifts —— AI 找问题的候选', () => {
  const drift = { change: 'A', kind: 'drift' }
  const adapt = { change: 'B', kind: 'adaptation' }

  it('成功且有漂移 → 返回列表', () => {
    expect(pickDrifts({ ok: true, drifts: [drift] })).toEqual([drift])
  })

  it('★ 最多取 3 条（后端逐条编号下发，改动太多会互相打架）', () => {
    const many = [1, 2, 3, 4, 5].map((n) => ({ change: `c${n}` }))
    expect(pickDrifts({ ok: true, drifts: many })).toHaveLength(3)
    expect(pickDrifts({ ok: true, drifts: many }).map((d) => d.change)).toEqual(['c1', 'c2', 'c3'])
  })

  it('空列表 / 缺字段 → null（调用方改为让用户手写）', () => {
    expect(pickDrifts({ ok: true, drifts: [] })).toBeNull()
    expect(pickDrifts({ ok: true })).toBeNull()
    expect(pickDrifts({ ok: false, drifts: [drift] })).toBeNull()
  })

  it('非法输入：null / undefined / 非对象都不抛', () => {
    for (const bad of [null, undefined, 0, '', 'x']) {
      expect(() => pickDrifts(bad)).not.toThrow()
      expect(pickDrifts(bad)).toBeNull()
    }
  })

  it('适配类（adaptation）也照常返回 —— 由UI 分开展示，不在这里过滤', () => {
    expect(pickDrifts({ ok: true, drifts: [adapt] })).toEqual([adapt])
  })
})

describe('nextResultAfterRepair —— 修复后的新成品', () => {
  it('换图与尺寸，但家族沿用旧的（局部修复不许改风格）', () => {
    expect(nextResultAfterRepair({ image_url: 'new.jpg', size: '2048x2048' },
      { url: 'old.jpg', size: '1024x1024', family_id: 'zine' }))
      .toEqual({ url: 'new.jpg', family_id: 'zine', size: '2048x2048' })
  })

  it('非法输入：后端没给 image_url 也照实返回（由调用方决定是否采用）', () => {
    expect(nextResultAfterRepair({}, { family_id: 'zine' }))
      .toEqual({ url: undefined, family_id: 'zine', size: undefined })
  })
})