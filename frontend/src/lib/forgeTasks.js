// 提炼任务的**状态机**：把「一堆 task_id + 一批轮询快照」归约成
// 「哪些还在跑、哪些刚跑完、哪些该从 localStorage 清掉」。
//
// ★ 为什么要从 hook 里搬出来（而不是留在 useForgeTask 里）：
//   轮询归约是整个工坊最容易出静默 bug 的地方 —— 它决定
//   「一个任务会不会被弹两次窗」「服务重启后会不会永远轮询一个 404」。
//   埋在组件里，这两种情况只能靠手点页面复现：重复事件要构造，
//   乱序事件要靠运气，超时更是要等 4-6 分钟。而2026-10-07 那次
//   `Cannot access 'flash' before initialization` 崩页，179 个单测
//   一个都没拦住，因为它们不渲染组件。
//
//   现在归约本身是纯函数：喂( ids, snaps ) → 吐出决策，
//   真正碰localStorage / 发请求的只有 hook 一层。
//   所以「同一批快照喂两次会不会重复弹窗」可以直接断言。
//
// ★ 行为契约：id 的顺序**由输入决定，不由本函数重排**。
//   App 的全局哨兵与工坊页会同时读 forgeTaskIds，两边都靠
//   `filter(x => !settled.includes(x))` 做删除，重排会让两边的
//   视图对不上（用户在工坊里看到的顺序和弹窗顺序会不一致）。

/** 终态集合。★ 只有这三个算「跑完了」—— queued/running/未知状态一律继续等。 */
export const TERMINAL_STATUSES = ['done', 'failed', 'cancelled']

/** 终态判定。未知状态（如后端新增 'paused'）返回 false，即继续等，不会误判成完成。 */
export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status)
}

/**
 * 旧版单值键 → 新数组键的迁移。
 *
 * 为什么要留：老版本（2026-10-05 之前）把 id 存在 forgeTaskId（单值）里。
 * 用户升级后本地还留着那个键，不迁移的话那次提炼就永远没人收尾。
 * 行为照抄原实现：新的排前面，且去重（cur 里已有的 legacy 不再重复入列）。
 * ★ 缺参不抛：与 parseTaskIds 同一纪律 —— 纯函数宁可给一个保守的
 *   空列表，也不能让调用方（localStorage 读取）把整个轮询打挂。
 */
export function mergeLegacyId(legacy, current = []) {
  return [legacy, ...(current || []).filter((x) => x !== legacy)]
}

/**
 * 从 localStorage 原始字符串解析 id 列表。
 * 非法 JSON 一律回落空数组 —— 宁可漏一个任务，也不能让一个坏值
 * 把整个工坊的轮询打挂（抛错会连带 setBgTasks 永不执行）。
 */
export function parseTaskIds(raw) {
  try {
    const v = JSON.parse(raw || '[]')
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

/**
 * 归约的核心：把一轮轮询的结果分成三堆。
 *
 * @param ids   当前在册的 task_id 数组（顺序 = 展示/弹窗顺序）
 * @param snaps 与 ids **等长同序**的快照数组；抓失败的位为 null
 * @returns { running, finished, gone }
 *   - running  还在跑的快照（直接透传，调用方拿去 setBgTasks）
 *   - finished 本轮刚到终态的快照（每个都要弹一次窗）
 *   - gone     本轮查不到的任务（404 / 网络失败），静默清掉
 *
 * ★ 为什么 404 要单独分一堆而不是当失败弹窗：
 *   服务重启后任务注册表就没了，此时弹「提炼失败」是**撒谎** ——
 *   用户会去 My 库里找一版根本不存在的草稿。原实现选择静默丢弃，
 *   这里保持一致，但把它显式建模成一个可断言的返回值。
 */
export function reduceSnapshots(ids, snaps) {
  const running = []
  const finished = []
  const gone = []
  for (let i = 0; i < ids.length; i++) {
    const t = snaps[i]
    if (!t) { gone.push(ids[i]); continue }          // 404：服务重启丢了任务，清掉
    if (isTerminal(t.status)) { finished.push(t); continue }
    running.push(t)
  }
  return { running, finished, gone }
}

/**
 * 本轮结束后 localStorage 里还该剩下哪些 id。
 * 语义是「从 remaining 里剔掉所有 settled 的」，而不是「只保留 running 的」——
 * 后者会在两个页面同时轮询时误删对方刚提交的任务。
 */
export function settleIds(remaining, gone, finished) {
  const settled = new Set([...gone, ...finished.map((t) => t.task_id)])
  return remaining.filter((x) => !settled.has(x))
}

/**
 * 轮询要不要停。
 * ★ 两个条件缺一不可：既没有在跑的，也没有在册的。
 * 只看running.length 会漏掉「刚被 App 那边清掉最后一个 id」的情况；
 * 只看在册数量则会在「这轮全 404」时多空转一轮（下一轮才发现没活了）。
 * 返回 true = 该clearInterval。
 */
export function shouldStopWatching(runningCount, remainingCount) {
  return !runningCount && !remainingCount
}

/**
 * 终态快照 → 强确认弹窗的载荷。
 * 逐字照抄原组件里的字段，弹窗文案一个字都不能变。
 */
export function donePopupPayload(t) {
  return {
    ok: t.status === 'done',
    cancelled: t.status === 'cancelled',
    name: t.result?.name || '未命名',
    version: t.result?.version,
    elapsed: t.elapsed_sec,
    error: t.error,
  }
}

/** 中止是协作式的：先给用户一句「可能要等几十秒」，否则他会以为按钮坏了。 */
export const CANCEL_PENDING_PHASE = '已请求中止（当前阶段结束后立即终止）'

export const CANCEL_PENDING_FLASH =
  '已请求中止 —— 正在进行的模型调用跑完后任务立即终止（通常几秒到一分钟）'

export const CANCELLED_FLASH = '提炼已中止，本次没有保存任何版本'
