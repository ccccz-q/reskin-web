import { describe, expect, it } from 'vitest'
import {
  CANCELLED_FLASH,
  CANCEL_PENDING_FLASH,
  CANCEL_PENDING_PHASE,
  TERMINAL_STATUSES,
  donePopupPayload,
  isTerminal,
  mergeLegacyId,
  parseTaskIds,
  reduceSnapshots,
  settleIds,
  shouldStopWatching,
} from './forgeTasks.js'

/** 一个 running 快照的最小形状。 */
const run = (over = {}) => ({ task_id: 't1', status: 'running', ...over })
const done = (over = {}) => ({ task_id: 't1', status: 'done', result: { name: 'x', version: 2 }, ...over })

describe('isTerminal —— 终态判定', () => {
  it('三个终态都认', () => {
    expect(TERMINAL_STATUSES).toEqual(['done', 'failed', 'cancelled'])
    for (const s of TERMINAL_STATUSES) expect(isTerminal(s)).toBe(true)
  })

  it('running / queued / 空都不是终态（要继续等，不能提前收尾）', () => {
    expect(isTerminal('running')).toBe(false)
    expect(isTerminal('queued')).toBe(false)
    expect(isTerminal('')).toBe(false)
    expect(isTerminal(undefined)).toBe(false)
    expect(isTerminal(null)).toBe(false)
  })

  it('★ 未知状态按「继续等」处理：后端加paused 不会让前端误判完成', () => {
    // 负向断言 —— 不能是 true
    expect(isTerminal('paused')).toBe(false)
    expect(isTerminal('DONE')).toBe(false)       // 大小写敏感，不接受
    expect(isTerminal(' done')).toBe(false)       // 带空格的脏值不算
  })
})

describe('parseTaskIds —— 解析 localStorage 里的 id 列表', () => {
  it('正常 JSON 数组原样返回', () => {
    expect(parseTaskIds('["a","b"]')).toEqual(['a', 'b'])
  })

  it('空/ 缺省 / null 都得到空数组', () => {
    expect(parseTaskIds('')).toEqual([])
    expect(parseTaskIds(null)).toEqual([])
    expect(parseTaskIds(undefined)).toEqual([])
  })

  it('★ 非法 JSON 回落空数组，绝不抛（抛错会连带轮询永不 setState）', () => {
    expect(() => parseTaskIds('{oops')).not.toThrow()
    expect(parseTaskIds('{oops')).toEqual([])
    expect(parseTaskIds('undefined')).toEqual([])
  })

  it('★ JSON 合法但不是数组时也回落 —— 对象/数字/字符串都不放行', () => {
    expect(parseTaskIds('{"a":1}')).toEqual([])
    expect(parseTaskIds('123')).toEqual([])
    expect(parseTaskIds('"abc"')).toEqual([])
    expect(parseTaskIds('null')).toEqual([])
  })

  it('空数组是合法值', () => {
    expect(parseTaskIds('[]')).toEqual([])
  })
})

describe('mergeLegacyId —— 旧单值键迁移进数组键', () => {
  it('legacy 排最前，其余保持原序', () => {
    expect(mergeLegacyId('old', ['a', 'b'])).toEqual(['old', 'a', 'b'])
  })

  it('★ 去重：cur 里已有同一个 legacy 时不重复入列', () => {
    expect(mergeLegacyId('a', ['a', 'b'])).toEqual(['a', 'b'])
  })

  it('当前列表为空时也能迁入', () => {
    expect(mergeLegacyId('old', [])).toEqual(['old'])
  })

  it('当前列表为 undefined 也不抛（视作空）', () => {
    expect(mergeLegacyId('old', undefined)).toEqual(['old'])
  })
})

describe('reduceSnapshots —— 轮询归约（核心状态机）', () => {
  it('正常流转：一个在跑 + 一个刚完成，两边分到对的堆里', () => {
    const ids = ['t1', 't2']
    const r = reduceSnapshots(ids, [run({ task_id: 't1' }), done({ task_id: 't2' })])
    expect(r.running).toHaveLength(1)
    expect(r.running[0].task_id).toBe('t1')
    expect(r.finished.map((t) => t.task_id)).toEqual(['t2'])
    expect(r.gone).toEqual([])
  })

  it('★ 全在跑：finished 与 gone 都空，running 原序透传', () => {
    const snaps = [run({ task_id: 't1' }), run({ task_id: 't2' }), run({ task_id: 't3' })]
    const r = reduceSnapshots(['t1', 't2', 't3'], snaps)
    expect(r.running.map((t) => t.task_id)).toEqual(['t1', 't2', 't3'])
    expect(r.finished).toEqual([])
    expect(r.gone).toEqual([])
  })

  it('★ 重复事件：同一批快照喂两次，finished 完全相同（归约本身无副作用）', () => {
    const ids = ['t1']
    const snaps = [done()]
    const a = reduceSnapshots(ids, snaps)
    const b = reduceSnapshots(ids, snaps)
    expect(b.finished).toEqual(a.finished)
    // 幂等性靠调用方清localStorage 保证：归约不吞事件，
    // 真正的去重在 settleIds + 写回这一步（下面单独测）
    expect(b.finished).toHaveLength(1)
  })

  it('★ 乱序事件：finished 保持 ids 顺序，不被快照顺序带偏', () => {
    // ids 是 t1,t2；快照数组故意反着放内容的位置以外的东西无关，
    // 关键断言是 finished 的顺序跟 ids 走
    const ids = ['z', 'a', 'm']
    const snaps = [done({ task_id: 'z' }), run({ task_id: 'a' }), done({ task_id: 'm' })]
    const r = reduceSnapshots(ids, snaps)
    expect(r.finished.map((t) => t.task_id)).toEqual(['z', 'm'])
    expect(r.running.map((t) => t.task_id)).toEqual(['a'])
  })

  it('★ 乱序投递：快照内容与 ids 下标对不上时，按位配对且总数守恒', () => {
    // 模拟服务端按自己的顺序返回快照。归约按下标配对，
    // 所以 t2 的 running 会被记到下标 0 —— 这是按位配对的必然结果。
    // 真正要锁的是：不崩，且每个 id 恰好进一个堆（不重复、不丢失）。
    const r = reduceSnapshots(
      ['t1', 't2'],
      [done({ task_id: 't2' }), run({ task_id: 't1' })],
    )
    expect(r.running.length + r.finished.length + r.gone.length).toBe(2)
  })

  it('★ 404（快照为 null）进 gone，且不弹失败窗', () => {
    const r = reduceSnapshots(['t1', 't2'], [null, run({ task_id: 't2' })])
    expect(r.gone).toEqual(['t1'])
    expect(r.finished).toEqual([])          // 负向：404 不许变成一次弹窗
    expect(r.running.map((t) => t.task_id)).toEqual(['t2'])
  })

  it('★ 全404：running 与 finished 都空，只有 gone', () => {
    const r = reduceSnapshots(['t1', 't2'], [null, null])
    expect(r).toEqual({ running: [], finished: [], gone: ['t1', 't2'] })
  })

  it('失败与取消都算终态（会进finished 弹窗）', () => {
    const r = reduceSnapshots(['a', 'b'], [
      { task_id: 'a', status: 'failed', error: 'boom' },
      { task_id: 'b', status: 'cancelled' },
    ])
    expect(r.finished.map((t) => t.status)).toEqual(['failed', 'cancelled'])
    expect(r.running).toEqual([])
  })

  it('★ 超时/僵死：status 仍是 running 的快照永远留在 running，不被当成完成', () => {
    // 模拟一个跑了 9999秒还没结束的任务
    const r = reduceSnapshots(['t1'], [{ task_id: 't1', status: 'running', elapsed_sec: 9999 }])
    expect(r.finished).toEqual([])
    expect(r.running).toHaveLength(1)
  })

  it('取消请求已发出但状态仍是 running（协作式中止的中间态）', () => {
    const r = reduceSnapshots(['t1'], [
      { task_id: 't1', status: 'running', cancelling: true, phase: CANCEL_PENDING_PHASE },
    ])
    expect(r.finished).toEqual([])
    expect(r.running[0].cancelling).toBe(true)
  })

  it('ids 为空数组时返回三堆全空', () => {
    expect(reduceSnapshots([], [])).toEqual({ running: [], finished: [], gone: [] })
  })

  it('★ 快照比 ids 短（并发新增任务）：多出来的 id 归到 gone，不会崩', () => {
    const r = reduceSnapshots(['t1', 't2', 't3'], [run({ task_id: 't1' })])
    expect(r.running).toHaveLength(1)
    expect(r.gone).toEqual(['t2', 't3'])
  })

  it('★ 快照比 ids 长：只按 ids 的长度遍历，多余快照被忽略', () => {
    const snaps = [run({ task_id: 't1' }), run({ task_id: 'extra' })]
    const r = reduceSnapshots(['t1'], snaps)
    expect(r.running.map((t) => t.task_id)).toEqual(['t1'])
  })
})

describe('settleIds —— 清掉本轮已了结的 id', () => {
  it('正常：剔掉 gone 与 finished，保留在跑的', () => {
    const left = settleIds(['a', 'b', 'c'], ['a'], [{ task_id: 'b' }])
    expect(left).toEqual(['c'])
  })

  it('★ 幂等：同一批 settled 删两次，第二次不再变化（重复事件不双删）', () => {
    const ids = ['a', 'b', 'c']
    const once = settleIds(ids, ['a'], [{ task_id: 'b' }])
    const twice = settleIds(once, ['a'], [{ task_id: 'b' }])
    expect(twice).toEqual(once)
    expect(twice).toEqual(['c'])
  })

  it('★ 只剔自己这轮的，不误删别人刚提交的（两页同时轮询）', () => {
    // 在册的 a,b,c,d；我这轮只了结了 a 和 c，b/d 可能是另一页刚提交的
    const left = settleIds(['a', 'b', 'c', 'd'], ['a'], [{ task_id: 'c' }])
    expect(left).toEqual(['b', 'd'])
  })

  it('finished 快照没有 task_id 时不会误删全部', () => {
    // 负向：settled 集合里全是 undefined，不能把真 id 都删光
    const left = settleIds(['a', 'b'], [], [{}])
    expect(left).toEqual(['a', 'b'])
  })

  it('没有可清的东西时原样返回（新数组）', () => {
    const ids = ['a']
    const out = settleIds(ids, [], [])
    expect(out).toEqual(['a'])
    expect(out).not.toBe(ids)
  })

  it('remaining 为空数组时返回空数组', () => {
    expect(settleIds([], ['a'], [{ task_id: 'b' }])).toEqual([])
  })
})

describe('shouldStopWatching —— 轮询停不停', () => {
  it('还有在跑的 → 不停', () => {
    expect(shouldStopWatching(1, 1)).toBe(false)
  })

  it('在跑没了但仍在册 → 不停（下一轮可能又有活的）', () => {
    expect(shouldStopWatching(0, 2)).toBe(false)
  })

  it('★ 两边都空才停', () => {
    expect(shouldStopWatching(0, 0)).toBe(true)
  })

  it('负向：在册数量为 0 但还有在跑的，绝不停（否则漏掉刚提交的任务）', () => {
    expect(shouldStopWatching(3, 0)).toBe(false)
  })
})

describe('donePopupPayload —— 强确认弹窗载荷', () => {
  it('成功版：ok=true，带名字/版本/耗时', () => {
    const p = donePopupPayload({ status: 'done', result: { name: '孔版', version: 3 }, elapsed_sec: 12.4 })
    expect(p).toEqual({ ok: true, cancelled: false, name: '孔版', version: 3, elapsed: 12.4, error: undefined })
  })

  it('★ 失败版：ok 与 cancelled 都 false，带 error', () => {
    const p = donePopupPayload({ status: 'failed', error: '视觉模型超时' })
    expect(p.ok).toBe(false)
    expect(p.cancelled).toBe(false)
    expect(p.error).toBe('视觉模型超时')
  })

  it('★ 取消版：cancelled=true，优先级高于 ok', () => {
    const p = donePopupPayload({ status: 'cancelled', result: { name: 'x' } })
    expect(p.cancelled).toBe(true)
    expect(p.ok).toBe(false)
  })

  it('★ result 缺失回落「未命名」，version 为 undefined（弹窗不显示第 undefined 版）', () => {
    const p = donePopupPayload({ status: 'done' })
    expect(p.name).toBe('未命名')
    expect(p.version).toBeUndefined()
  })

  it('★ 空名字 /空对象 result 同样回落「未命名」', () => {
    expect(donePopupPayload({ status: 'done', result: {} }).name).toBe('未命名')
    expect(donePopupPayload({ status: 'done', result: { name: '' } }).name).toBe('未命名')
  })

  it('elapsed 缺失就是 undefined（展示层再兜底成 0）', () => {
    expect(donePopupPayload({ status: 'done' }).elapsed).toBeUndefined()
  })
})

describe('中止相关文案常量（照抄原值，文案改动= 行为改动）', () => {
  it('常量齐全且非空', () => {
    for (const s of [CANCEL_PENDING_PHASE, CANCEL_PENDING_FLASH, CANCELLED_FLASH]) {
      expect(typeof s).toBe('string')
      expect(s.length).toBeGreaterThan(0)
    }
  })

  it('中止待定阶段名说明「不是卡死」', () => {
    expect(CANCEL_PENDING_PHASE).toContain('当前阶段结束后立即终止')
    expect(CANCEL_PENDING_FLASH).toContain('中止')
    expect(CANCELLED_FLASH).toContain('没有保存任何版本')
  })
})
