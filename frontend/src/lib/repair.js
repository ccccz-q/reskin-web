// 修复域的纯逻辑 —— 从 App.jsx 原样搬出，逻辑与文案一字未改。
//
// 为什么要单独成文件：这些函数决定「用户点了会发生什么」，
// 是一旦改错就直接烧额度、或者让修复清单悄悄丢一条的地方。
// 之前它们埋在组件体内，只能靠手点页面验证；抽出来后可以对着
// 正常 / 边界 / 非法三类输入做断言。

/** 修复面板的维度清单。顺序即UI 顺序，改动会影响用户已有的肌肉记忆。 */
export const REPAIR_CATS = [
  '构图与视角',
  '光线层级',
  '色彩与曝光',
  '材质与质感',
  '人物姿态',
  '商业化感',
  '其他',
]

// 一次最多修几处。后端会把每一行编号成一条外科指令，
// 改动再多就互相打架、不可控了。
export const MAX_REPAIR_LINES = 3

// ★ 从一句话修复描述猜维度 —— AI 找问题返回的是自然语言，让人再归类一次
//   纯属折腾；猜错了也无害（用户可以自己改），猜对了就少点两下。
//   注意表是**顺序敏感**的：「曝光过度」同时命中光线与色彩，
//   靠前的「光线层级」先赢 —— 这是当初手调出来的优先级，别重排。
const CAT_TABLE = [
  ['构图与视角', ['构图', '视角', '角度', '朝向', '转向', '位置']],
  ['光线层级', ['光', '阴影', '亮度', '曝光过度', '打光']],
  ['色彩与曝光', ['色', '曝光', '饱和度', '色调', '对比度', '偏色']],
  ['材质与质感', ['材质', '质感', '纹理', '笔触', '颗粒', '浮雕', '体素', '球']],
  ['人物姿态', ['人', '姿态', '动作', '手势', '脸', '五官', '手指']],
  ['商业化感', ['商业', '广告', '按钮', '水印', '文字', 'UI', 'logo', '文案']],
]

export const FALLBACK_CAT = '其他'

/** 猜维度；空输入/无命中一律回落到「其他」，绝不返回 undefined。 */
export function guessCat(text) {
  const t = text || ''
  for (const [cat, words] of CAT_TABLE) {
    if (words.some((w) => t.includes(w))) return cat
  }
  return FALLBACK_CAT
}

/** 维度多选：勾上就加、再点就摘。传入的数组不会被改。 */
export function toggleCat(cats, c) {
  return cats.includes(c) ? cats.filter((x) => x !== c) : [...cats, c]
}

/** 把 textarea 的原文拆成「一行一处」的干净列表（去空行、去行首尾空白）。 */
export function splitRepairLines(note) {
  return (note || '').split('\n').map((s) => s.trim()).filter(Boolean)
}

/**
 * 点选一条 AI 候选 → 追加为一行；再点一次取消。
 * 返回新字符串；返回 null 表示「拒绝写入」，调用方负责弹上限提示。
 *
 * 为什么不返回 {note, ok}：调用点原本就是「拿不到新值就 flash」，
 * 用 null 当哨兵可以少一层对象分配，也不用改原来的分支形状。
 */
export function nextRepairNote(cur, txt) {
  const lines = splitRepairLines(cur)
  if (lines.includes(txt)) return lines.filter((s) => s !== txt).join('\n')
  if (lines.length >= MAX_REPAIR_LINES) return null      // 超上限，调用方 flash
  return [...lines, txt].join('\n')
}

/** 顺手勾上猜出的维度；已勾过就不重复加（保证维度列表无重复项）。 */
export function withGuessedCat(cats, txt) {
  const c = guessCat(txt)
  return cats.includes(c) ? cats : [...cats, c]
}

/**
 * 修复前置校验：返回阻断原因文案，null 表示可以放行。
 * 抽成纯函数是为了让「四道闸门的先后顺序」可测 —— 顺序本身就是行为：
 * busy 时报「任务结束后再微调」比报「先写清哪里不对」更贴近用户此刻的处境。
 */
export function repairBlockedReason({ busy, note, quota, result }) {
  if (busy) return '正在进行的任务结束后再微调'
  if (!splitRepairLines(note).length) return '先写清哪里不对（可勾选维度 + 点选 AI 候选）'
  if (quota?.exhausted) return '本会话额度已用完'
  if (!result?.url) return '先出一张图，才能对它做局部修复'
  return null
}

/** 修复成功后要压栈的历史版本快照。字段与 result 对齐，回退时可直接当result 用。 */
export function pushUndoEntry(stack, result) {
  return [...stack, { url: result.url, size: result.size, family_id: result.family_id }]
}

/** 回到上一版：取出栈顶并返回新栈；栈空返回 null（调用方直接 return，什么都不做）。 */
export function popUndoEntry(stack) {
  if (!stack.length) return null
  return { entry: stack[stack.length - 1], stack: stack.slice(0, -1) }
}

/** AI 找问题的结果 → 要展示的候选列表（最多 3 条）；无有效漂移返回 null。 */
export function pickDrifts(r) {
  if (r?.ok && (r.drifts || []).length) return r.drifts.slice(0, 3)
  return null
}

/** 修复成功后的新成品：家族沿用旧结果（局部修复不改风格），只换图与尺寸。 */
export function nextResultAfterRepair(r, prevResult) {
  return { url: r.image_url, family_id: prevResult.family_id, size: r.size }
}