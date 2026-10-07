import { imgSrc } from '../api'

/**
 * 最近生成缩略条 —— 出图后的延续动作：换一张看、回画布细看、进仓库。
 *
 * ★ 为什么放在出图后而不是常驻：之前这里放「作品档案卡」（参数回顾），
 *   实测是摆设 —— 参数就在上面的表单里，没人会回来看一遍自己刚选了什么。
 */
export default function GalleryStrip({ items, currentUrl, familyId, onPick, onOpenAll }) {
  return (
    <div className="recent" role="group" aria-labelledby="h-arch">
      <div className="recent-row">
        {items.map((it) => (
          <button key={it.url}
                  className={`recent-thumb ${it.url === currentUrl ? 'on' : ''}`}
                  onClick={() => onPick({ url: it.url, family_id: it.family_id || familyId })}
                  title="点回画布查看">
            <img src={imgSrc(it.url)} alt={it.filename} decoding="async" />
          </button>
        ))}
      </div>
      <div className="recent-foot">
        <span className="muted">{items.length} 张 · 点缩略图回画布</span>
        <span className="tb-spacer" />
        <button className="btn ghost tb-btn" onClick={onOpenAll}>
          查看全部 →
        </button>
      </div>
    </div>
  )
}