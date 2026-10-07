// 工坊的纯逻辑：校验文案、派生兜底、阶段名映射。
//
// ★ 为什么要从组件里搬出来：
//   这些函数决定「用户点了会发生什么、失败时看到哪句话」。
//   之前它们是组件体内的内联表达式，**没有任何测试覆盖** —— 而
//   2026-10-07 那次崩页正好说明：能通过 build + lint + 179 个单测的
//   代码，运行时照样能整页炸进 ErrorBoundary。纯函数是唯一能被
//   廉价穷举的防线，所以先把它们变成能被穷举的形状。
//
// ★ 纪律：文案与判断顺序与拆分前**逐字一致**。这里只做「搬家」，
//   不做「顺手改进」—— 校验顺序本身就是行为（先报哪个错，
//   用户就以为该先做什么）。

/** 失败版在记录里的标记前缀（后端写入 feedback 字段）。 */
export const FAIL_PREFIX = '【提炼失败】'

/**
 * 失败原因文本；不是失败版则返回空串。
 *
 * ★ 为什么要单独识别失败版：一条「未命名 / 空提示词」的失败记录，
 *   在UI 上和正常版长得几乎一样。不打标的话用户会以为提炼成功了，
 *   然后花 4-6 分钟去「迭代一版」—— 而它必然又失败，因为根因在图片或理论。
 */
export function failNote(record) {
  const fb = record?.feedback
  if (typeof fb !== 'string' || !fb.startsWith(FAIL_PREFIX)) return ''
  return fb.slice(FAIL_PREFIX.length)
}

/** 提炼的三路输入是否至少给了一样（图 / 理论 / 提示词）。全空则不能提交。 */
export function hasAnyInput({ images, theory, stylePrompt } = {}) {
  return (Array.isArray(images) ? images.length : 0) > 0
    || !!(theory || '').trim()
    || !!(stylePrompt || '').trim()
}

export const EMPTY_INPUT_HINT =
  '至少上传一张参考图，或写一段风格理论 / 贴一段风格提示词。'

/**
 * 手改提示词的最小长度。
 * 定20 是因为低于这个长度几乎必然是误操作（误删、只打了一个字），
 * 而手改是要**永久覆盖**自动渲染的结果，写坏了只能再花 4-6 分钟重来。
 */
export const MIN_MANUAL_PROMPT = 20

export const PROMPT_TOO_SHORT_HINT =
  '改后的提示词太短了（至少 20 字）—— 想恢复自动渲染请用「恢复自动」'

/**
 * 保存手改提示词前的校验。
 * @returns 阻断文案；null 表示放行。
 * ★ 空串是**合法输入**而不是错误 —— 它是「恢复自动渲染」的信号。
 */
export function promptSaveBlocked(text, { saving } = {}) {
  if (saving) return null                       // 正在保存：直接忽略重复点击
  // ★ 强制转字符串：调用点是 textarea，但 draft 也可能来自
  //   「恢复自动」那类传 undefined 的路径 —— 一次.trim 报错就能
  //   把整个提示词面板的保存按钮打死。
  const t = (typeof text === 'string' ? text : (text == null ? '' : String(text))).trim()
  if (t && t.length < MIN_MANUAL_PROMPT) return PROMPT_TOO_SHORT_HINT
  return null
}

export const NO_VERSION_HINT =
  '当前没有打开的版本（可能刚被删除）—— 请从「我的库」打开一版再迭代'

/**
 * 点「迭代一版」的前置校验。
 * @returns 阻断文案；null 表示可以弹确认框。
 *
 * ★ 只拦「没有打开的版本」这一种。反馈为空时按钮本来就是灰的，
 *   这里返回 null 是纯兜底 —— 灰按钮已经拦过一次，再弹一次提示
 *   属于重复噪音（原实现就是直接 return，不提示）。
 * ★ 失败版（failNote）也不在这里拦：它在按钮的 disabled 上已经处理了，
 *   原实现没有第二道闸，这里不新增 —— 校验顺序本身就是行为。
 */
export function reviseBlockedReason({ current, feedback }) {
  if (!current?.id) return NO_VERSION_HINT
  if (!(feedback || '').trim()) return null
  return null
}

/**
 * 安装后的提示文案。
 * ★ 为什么要合并 qc_warnings 与 warnings（2026-10-06）：
 *   后端算完QC 结论就丢，前端只报「安装成功」—— 用户以为一切正常，
 *   而「这个家族有隐患」「下次迭代会把缺陷带回来」恰恰是最该说的。
 */
export function installFlash(result) {
  const notes = [...(result?.qc_warnings || []), ...(result?.warnings || [])]
    .filter(Boolean)
  return notes.length
    ? `已安装「${result?.family_id}」，但有 ${notes.length} 处需要注意：${notes.join('；')}`
    : `已安装「${result?.family_id}」，可以在左侧家族列表里用了`
}

/** 后台提炼阶段名 → 展示文案。原实现是 `t.phase || '进行中'`，抽出来是为了能测空值。 */
export function phaseLabel(task) {
  return task?.phase || '进行中'
}

/** 秒数展示：一律四舍五入，且null/NaN 归0（后端偶尔不返 elapsed_sec）。 */
export function elapsedLabel(sec) {
  const n = Math.round(Number(sec) || 0)
  return n < 0 ? 0 : n
}

/** 提炼完成后的 toast 文案；version 为 null 时不发（失败版不该报「第 null 版」）。 */
export function draftReadyFlash(result) {
  if (result?.version == null) return null
  return `「${result.name}」第 ${result.version} 版已就绪`
}

/** 提交后的 toast：并行任务数决定语气，单任务时给出「可以离开」的提示。 */
export function submitFlash(parallelCount) {
  return parallelCount > 1
    ? `已开始提炼（现有 ${parallelCount} 个任务并行）—— 可以继续上传下一组，完成后会逐个提醒`
    : '已在后台开始提炼 —— 可以继续提交下一组或去别的页面，完成后会提醒你'
}
