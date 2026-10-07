import { imgSrc } from '../api'

/**
 * 风格家族列表（每项自带独立展开的示例卡）。
 *
 * ★ 每个家族独立开合 —— 点开下一个不收上一个，只有各自的「收起」按钮才收起
 *   （这是用户明确要求的交互，不要「优化」成手风琴）。
 *
 * ★ 两段式删除确认：第一击变「确认删除?」，再击执行，3.5 秒不点自动还原。
 *   直接删太危险（会连带清理库里的安装标记），但弹原生 confirm 又太打断。
 */
export default function FamilyList({
  families, familyId, booting, exOpen, delArm,
  builtinIds, onPick, onOpenExample, onCloseExample, onRequestDelete,
}) {
  return (
    <div className="fam-list" role="group" aria-labelledby="h-family">
      {booting && <span className="muted">加载中…</span>}
      {!booting && families.length === 0 && <span className="muted">一个家族都没有</span>}
      {families.map((f) => (
        <div key={f.id} className="fam-slot">
          <button type="button"
                  className={`fam ${f.id === familyId ? 'on' : ''}`}
                  aria-pressed={f.id === familyId}
                  aria-expanded={exOpen.has(f.id)}
                  onClick={() => onPick(f.id)}>
            <span className="fam-icon" aria-hidden="true">{f.icon || '◧'}</span>
            <span className="fam-body">
              <span className="fam-name">{f.name}</span>
              <span className="fam-desc">{f.description}</span>
            </span>
            <span className={`fam-caret ${exOpen.has(f.id) ? 'open' : ''}`} aria-hidden="true">▾</span>
          </button>
          {/* 工坊安装的家族可从主页面删除（内置家族不显示按钮） */}
          {!builtinIds.has(f.id) && (
            <button type="button"
                    className={`fam-del ${delArm === f.id ? 'armed' : ''}`}
                    aria-label={`删除家族 ${f.name}`}
                    title={delArm === f.id ? '再点一次确认删除（会同时清理库里的安装标记）' : '删除这个家族'}
                    onClick={() => onRequestDelete(f)}>
              {delArm === f.id ? '确认删除?' : '×'}
            </button>
          )}
          {/* 示例卡片：常挂载 + class 切换，展开/收起都有平滑动画 */}
          <div className={`fam-ex ${exOpen.has(f.id) ? 'open' : ''}`}
               aria-hidden={!exOpen.has(f.id)}>
            <div className="fam-ex-clip">
              {f.example ? (
                <figure className="fam-ex-card">
                  <img src={imgSrc(f.example)} alt={`「${f.name}」风格示例`}
                       loading="lazy" decoding="async"
                       onClick={() => onOpenExample({ url: f.example })}
                       title="点击查看大图" />
                  <figcaption className="fam-ex-bar">
                    <span className="fam-example-tag">示例</span>
                    <span className="fam-ex-cap">{f.name} · 实际效果跟随你的原图</span>
                    <button type="button" className="fam-ex-hide"
                            onClick={() => onCloseExample(f.id)}
                            title="收起示例图">收起 ▴</button>
                  </figcaption>
                </figure>
              ) : (
                <div className="fam-ex-bar">
                  <span className="muted">暂无示例图 —— 出一张满意的图后可设为该家族示例</span>
                  <button type="button" className="fam-ex-hide"
                          onClick={() => onCloseExample(f.id)}>收起 ▴</button>
                </div>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}