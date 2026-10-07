import { elapsedLabel } from '../lib/forge'

/**
 * 工坊的两个强确认弹窗：重新提炼确认、提炼结果队列。
 *
 * ★ 为什么要拆出来：这两个弹窗是**唯一**会盖住整个工坊、
 *   且Esc 被 capture 阶段拦截的元素（见 Workshop 里的 keydown 守卫）。
 *   它们的结构复杂（队列、倒计时文案、手改提醒）但完全无状态 ——
 *   拆出来后「弹窗长什么样」和「什么条件下弹」彻底分开。
 *
 * ★ 注意：两个弹窗都**没有**遮罩关闭、**没有** Esc 关闭。
 *   这是刻意的（2026-10-03）：提炼要跑 4-6 分钟，
 *   用户误点遮罩把「已完成」的提示关掉，会以为白等了。
 */
export default function ForgeDialogs(props) {
  const {
    reviseConfirm, current, feedback, donePopupQueue,
    onConfirmRevise, onEditInstead, onCancelRevise, onAckDone,
  } = props

  return (
    <>
      {/* ── 重新提炼确认（用户需求 2026-10-03）：重跑 LLM 前，
          告知「可以先直接改提示词」+ 预计耗时；有手改时额外提醒会被替代 ── */}
      {reviseConfirm && (
        <div className="forge-notice-mask" style={{ zIndex: 80 }} role="alertdialog"
             aria-modal="true" aria-label="确认重新提炼">
          <div className="forge-notice">
            <header className="fn-head">
              <span className="fn-title">⟳ 确认重新提炼？</span>
            </header>
            <div className="fn-body">
              <p>重新提炼会由 AI 重写整版提示词并生成新版本，<b>约需 4-6 分钟</b>。</p>
              <p>
                💡 如果只是<b>个别片段不对</b>，不用重新提炼 ——
                点「查看提示词 ▾」→「修改提示词」，直接改字保存即可，<b>立即生效</b>。
              </p>
              {current?.prompt_override && (
                <p className="notice warn">⚠ 这一版有你手动修改过的提示词；
                  重新提炼产生的新版本将从自动渲染重新开始，手改内容不会带过去。</p>
              )}
              <p className="muted">修改意见：「{feedback.trim().slice(0, 60)}{feedback.trim().length > 60 ? '…' : ''}」</p>
            </div>
            <div className="fn-actions">
              <button type="button" className="btn solid" autoFocus
                      onClick={onConfirmRevise}>
                确认，重新提炼
              </button>
              <button type="button" className="btn ghost"
                      onClick={onEditInstead}>
                我先直接改提示词
              </button>
              <button type="button" className="btn ghost"
                      onClick={onCancelRevise}>
                取消
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 提炼结果强确认（队列）：多任务并行时逐个弹，
          确认一个再弹下一个。盖住整个工坊，只能点「确认」关闭 ── */}
      {donePopupQueue.length > 0 && (
        <ForgeDonePopup
          dp={donePopupQueue[0]}
          more={donePopupQueue.length - 1}
          onAck={onAckDone}
        />
      )}
    </>
  )
}

/** 单个结果弹窗。抽成子组件是因为它有自己的局部派生（标题/正文分支）。 */
function ForgeDonePopup({ dp, more, onAck }) {
  return (
    <div className="forge-notice-mask" style={{ zIndex: 80 }} role="alertdialog"
         aria-modal="true" aria-label="提炼结果">
      <div className="forge-notice">
        <header className="fn-head">
          <span className="fn-title">
            {dp.cancelled ? '⚪ 提炼已中止' : dp.ok ? '✅ 提炼成功' : '⛔ 提炼失败'}
            {more > 0 && (
              <span className="muted">　（还有 {more} 个结果待确认）</span>
            )}
          </span>
        </header>
        <div className="fn-body">
          {dp.cancelled ? (
            <p>「{dp.name}」的提炼已按你的要求中止，本次没有保存任何版本。</p>
          ) : dp.ok ? (
            <p>
              「{dp.name}」第 {dp.version} 版已入库
              （用时 {elapsedLabel(dp.elapsed)}s）。
            </p>
          ) : (
            <p>{dp.error || '提炼未完成'}。可调整图片或理论后重新提炼。</p>
          )}
          <p className="muted">请点下方「确认」继续。</p>
        </div>
        <div className="fn-actions">
          <button type="button" className="btn solid" autoFocus onClick={onAck}>
            确认
          </button>
        </div>
      </div>
    </div>
  )
}
