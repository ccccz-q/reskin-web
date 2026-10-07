import { imgSrc } from '../api'

/**
 * 画布与结算条 —— 中间这一列只回答「点下去会怎样」。
 *
 * ★ 只展示成品。旧版的「原图/成品对比滑杆」已移除：
 *   两个图层长宽比不一致时（横版原图 + 竖版海报），滑杆比的不是同一个
 *   几何位置，对比没有意义，还会让人误以为「只生成了局部」。
 *
 * ★ 出图是全页唯一的结算动作（还会花掉额度），所以它必须：
 *   ① 紧贴它作用的画布 ② 永远可见 ③ 说清为什么现在点不了。
 *   列不滚动布局（2026-10-03）：操作条固定在画布之下、轨迹之上，
 *   轨迹区自滚动，操作条不再悬浮遮挡任何内容。
 */
export default function CanvasPanel({
  source, result, family, familyId, undoCount,
  busy, quotaExhausted, canGenerate, goHint, repairTipOff,
  trace, downloading, onRipple, onGenerate, onAbort, onUndo,
  onOpenLightbox, onOpenRepair, onDownload, onDismissTip,
}) {
  return (
    <section className="col canvas" id="canvas" aria-label="画布与出图">
      <h2 id="h-canvas" className="rail-title">
        画布
        {source && <span className="muted">{result ? '原图 / 成品' : '只有原图'}</span>}
      </h2>

      <div className="stage">
        {result ? (
          <img className="stage-img" src={imgSrc(result.url)} alt="生成的成品"
               decoding="async"
               onClick={() => onOpenLightbox({ url: result.url, dim: result.size })}
               title="点击查看大图" />
        ) : source ? (
          <img className="stage-img stage-src" src={imgSrc(source.url)} alt="原图"
               decoding="async" />
        ) : <p className="stage-empty">还没有原图 —— 先在左边放一张进来</p>}
      </div>

      {result && (
        <>
          <div className="toolbar">
            {result.size && <span className="tag">{result.size}</span>}
            <span className="muted">{family?.name || familyId}</span>
            {undoCount > 0 && (
              <span className="tag tag-soft" title="这张是修复后的版本">
                已修复 {undoCount} 次
              </span>
            )}
            <span className="tb-spacer" />
            {undoCount > 0 && (
              <button className="btn ghost tb-btn" onClick={onUndo}
                      title="退回修复前的那一版 —— 立刻生效，不消耗额度">
                ↩ 回到上一版
              </button>
            )}
            <button className="btn ghost tb-btn"
                    onClick={() => onOpenLightbox({ url: result.url, dim: result.size })}>
              🔍 查看大图
            </button>
            <button className={`btn tb-btn ${repairTipOff ? 'ghost' : 'hl-open'}`}
                    onClick={onOpenRepair}
                    disabled={busy}
                    title={busy
                      ? '等这次生成结束再修复'
                      : '对刚生成的结果不满意时，只修你点名的一处，其余画面保持不变'}>
              ✚ 局部修复
            </button>
            <button className="btn solid tb-btn" onClick={onDownload} disabled={downloading}>
              {downloading ? '下载中…' : '⬇ 下载图片'}
            </button>
          </div>

          {/* ★ 让「修复」被看见：功能再好，用户不知道等于不存在。
              出图即显示（图先上画布、收尾文案还在跑时也要在——
              否则用户面对灰按钮不知道在等什么）；读过就不再出现。 */}
          {!repairTipOff && (
            <div className="result-tip" role="note">
              <span className="result-tip-body">
                {busy
                  ? <>成品已出 —— 收尾文案还在跑（几秒到半分钟），结束后就能用<b>「局部修复」</b>只修某一处</>
                  : <>某一处不满意不用整张重出 —— 点<b>「局部修复」</b>，说清那一处，
                     其余画面原样保留；不确定哪里不对可以让 AI 先对比原图找问题</>}
              </span>
              {!busy && (
                <button type="button" className="btn ghost result-tip-go"
                        onClick={onOpenRepair}>去修复</button>
              )}
              <button type="button" className="result-tip-x" aria-label="不再提示"
                      title="知道了，不再提示"
                      onClick={onDismissTip}>✕</button>
            </div>
          )}
        </>
      )}

      <div className="actionbar">
        <button
          type="button"
          className="btn-generate"
          onClick={(e) => { onRipple(e); onGenerate() }}
          disabled={busy || !canGenerate || quotaExhausted}
        >
          {busy ? '生成中…' : '生成图片'}
        </button>
        {busy && (
          <button type="button" className="btn ghost" onClick={onAbort}>
            ■ 终止生成
          </button>
        )}
        <p className="actions-hint">{goHint}</p>
      </div>

      <h3 className="rail-title sub">
        Agent 轨迹
        {busy && <span className="muted">进行中…</span>}
      </h3>
      <div className="trace" role="log" aria-live="polite" aria-busy={busy}>
        {trace.length === 0 && !busy && (
          <p className="muted">点「生成图片」后，这里会实时列出每一步</p>
        )}
        {trace.map((t, i) => (
          <div key={i} className={`trace-row ${t.state}`}
               title={`${t.name}${t.summary ? '：' + t.summary : ''}`}>
            <span className="trace-dot" aria-hidden="true" />
            <span className="trace-name">{t.name}</span>
            <span className="trace-sum">{t.summary || (t.state === 'running' ? '执行中…' : '')}</span>
          </div>
        ))}
      </div>
    </section>
  )
}