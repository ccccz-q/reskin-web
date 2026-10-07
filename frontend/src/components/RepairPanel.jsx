import { imgSrc } from '../api'
import { REPAIR_CATS } from '../lib/repair'

/**
 * 局部修复面板（模态）。
 *
 * ★ 修复是「外科手术」：参考图 = 当前成品，不是用户原图。
 *   所以面板顶部把修复对象的缩略图亮出来 —— 眼见为实，
 *   不用猜「我现在修的是原图还是成品」（2026-10-05 用户要求确认）。
 *
 * ★ 进行中不许关：用户关了面板会以为「成功了一半」，
 *   实际上后端还在跑。所以遮罩点击与关闭按钮都看 repairBusy。
 */
export default function RepairPanel({
  result, source, family, quota,
  cats, note, busy, diagBusy, drifts, elapsed,
  onNoteChange, onToggleCat, onPickDrift, onDiagnose, onRepair, onClose,
}) {
  const lineCount = note.split('\n').filter((s) => s.trim()).length
  return (
    <div className="repair-mask" onClick={(e) => {
      if (e.target === e.currentTarget && !busy) onClose()
    }}>
      <div className="repair" role="dialog" aria-modal="true" aria-label="局部修复">
        <header className="repair-head">
          <span className="repair-glyph">🩹</span>
          <div className="repair-titles">
            <span className="ws-title">局部修复</span>
            <span className="ws-sub">以当前这张成品为底，只改你点名的地方</span>
          </div>
          <button className="ws-close" onClick={() => !busy && onClose()}
                  disabled={busy} aria-label="关闭">关闭</button>
        </header>

        {/* ★ 修复对象亮明身份：缩略图就是被修的那张成品 */}
        <div className="repair-target">
          <img src={imgSrc(result?.url)} alt="修复对象：当前成品" />
          <div className="repair-target-text">
            <b>修复对象</b>
            <span>当前这张成品（不是你的原图）。原图只用来给 AI 做对比参考。</span>
            {result?.size && <span className="tag">{result.size}</span>}
          </div>
        </div>

        <div className="repair-body">
          <div className="ws-actions">
            <button className="btn ghost" onClick={onDiagnose}
                    disabled={diagBusy || busy || !source?.url || !result?.url}>
              {diagBusy ? 'AI 正在对比原图找问题…' : '🔍 不确定哪里不对？让 AI 对比原图找问题'}
            </button>
            <span className="set-note-inline">
              走一次视觉模型（不消耗出图额度）；已按「{family?.name || result?.family_id || '当前风格'}」
              识别模板刻意添加的元素，不会把它们误报成问题
            </span>
          </div>

          {/* ★ AI 的候选是「勾选清单」：点一下加入要修清单，再点一下取消 ——
              多处小问题可以一次勾上，由后端逐条编号下发（上限 3 条）。 */}
          {drifts.length > 0 && (
            <div className="drift-list" role="group" aria-label="AI 找到的候选问题">
              {drifts.map((d, i) => {
                const isDrift = d.kind !== 'adaptation'
                const on = note.split('\n').map((s) => s.trim())
                  .includes(String(d.change || '').trim())
                return (
                  <button key={i} type="button"
                          className={`drift-item ${isDrift ? 'drift' : 'adapt'} ${on ? 'on' : ''}`}
                          title={`${d.why || ''}${on ? '（已选中，再点取消）' : '（点击加入要修清单）'}`}
                          aria-pressed={on}
                          disabled={busy}
                          onClick={() => onPickDrift(d)}>
                    <span className="drift-check">{on ? '✓' : ''}</span>
                    <span className="drift-kind">{isDrift ? '建议修' : '风格适配'}</span>
                    <span className="drift-text">{d.change}</span>
                  </button>
                )
              })}
            </div>
          )}

          <label className="ws-label" htmlFor="repair-cats">
            要修的维度（可多选）
          </label>
          <div className="repair-cats" id="repair-cats">
            {REPAIR_CATS.map((c) => (
              <button key={c} type="button"
                      className={`extra-mode ${cats.includes(c) ? 'on' : ''}`}
                      aria-pressed={cats.includes(c)}
                      disabled={busy}
                      onClick={() => onToggleCat(c)}>{c}</button>
            ))}
          </div>

          <label className="ws-label" htmlFor="repair-note">
            要修哪几处（一行一处，最多 3 处；AI 候选与手写都可以再改）
          </label>
          <textarea id="repair-note" className="ws-input" rows={4} value={note}
                    maxLength={400}
                    disabled={busy}
                    onChange={(e) => onNoteChange(e.target.value)}
                    placeholder={'例如：\n树冠变成圆球了，恢复成方块体素拼接\n右上角多出的英文文案删掉'} />
          <div className="repair-meta">
            <span className="muted">
              一次最多 3 处 —— 太多改动会互相打架，修完可以再来一轮
            </span>
            <span className="tb-spacer" />
            <span className={`repair-count ${note.length > 400 ? 'over' : ''}`}>
              {note.length}/400
            </span>
          </div>
          <div className="ws-actions">
            <button className="btn solid" onClick={onRepair}
                    disabled={busy || !note.trim()}>
              {busy
                ? `修复中… ${elapsed}s`
                : `开始修复（${lineCount || '—'} 处）`}
            </button>
            <span className="set-note-inline">
              {quota?.unlimited
                ? '走 gpt-image 通道（额度不限）。'
                : `会消耗 1 张额度（剩 ${quota?.remaining ?? '—'}）。`}
              一般 40 秒到 2 分钟；修完不满意可以继续修，或用「回到上一版」退回。
              你写过的自定义提示词与风格禁令会自动带上，修复不会破坏它们。
            </span>
          </div>
          {busy && (
            <p className="repair-waitnote" role="status">
              正在按你的清单重画 —— 页面可以离开，别重复点「开始修复」。
            </p>
          )}
        </div>
      </div>
    </div>
  )
}