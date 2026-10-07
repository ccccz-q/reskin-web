/**
 * 后台提炼提醒 —— 工坊里发起提炼后，用户去任何页面都能收到「提炼好了」。
 *
 * ★ 为什么是全局弹窗而不是只在工坊里提示：提炼要跑几十秒到几分钟，
 *   用户不会一直守着工坊页。他可能已经回画布改参数了，
 *   弹窗要能跨页面找到他。
 *
 * status 三态：done（入库）/ cancelled（用户中止）/其它（失败）。
 */
export default function ForgeNotice({ notice, onClose, onOpenWorkshop }) {
  if (!notice) return null
  return (
    <div className="forge-notice-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="forge-notice" role="alertdialog" aria-label="模板提炼提醒">
        <header className="fn-head">
          <span className="fn-title">
            {notice.status === 'done'
              ? '✅ 提示词提炼好了'
              : notice.status === 'cancelled'
                ? '⚪ 提炼已中止'
                : '⚠️ 提炼没有成功'}
          </span>
        </header>
        <div className="fn-body">
          {notice.status === 'done' ? (
            <>
              <p>
                「{notice.result?.name || '未命名'}」第 {notice.result?.version} 版已入库
                （用时 {Math.round(notice.elapsed_sec || 0)}s）。
              </p>
              {(notice.result?.warnings || []).length > 0 && (
                <p className="fn-warn">{notice.result.warnings[0]}</p>
              )}
            </>
          ) : notice.status === 'cancelled' ? (
            <p>「{notice.result?.name || '未命名'}」的提炼已按你的要求中止，本次没有保存任何版本。</p>
          ) : (
            <p>{notice.error || '提炼未通过校验'}。可以回工坊调整图片或理论后重试。</p>
          )}
        </div>
        <div className="fn-actions">
          <button className="btn solid" onClick={() => { onClose(); onOpenWorkshop() }}>
            去工坊查看
          </button>
          <button className="btn ghost" onClick={onClose}>知道了</button>
        </div>
      </div>
    </div>
  )
}