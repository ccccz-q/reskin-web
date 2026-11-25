import { useEffect, useMemo, useState } from 'react'
import { imgSrc } from '../api'
import AlbumViewer from './AlbumViewer'
import FlipCompare from './FlipCompare'
import StageView from './StageView'
import AssetEditor from './AssetEditor'
import './stageview.css'
import './asset-editor.css'

/** 成品集自身的样式内联在下方 <style> 里——
    不再单独引 showcase.css（那会把简单的事变复杂）。 */

/**
 * 成品集 —— 独立入口，展示「实拍照片 → 文创成品」的完整结果。
 *
 * ★★ 为什么是独立入口，而不是在生成后问「要不要做成一组」：
 *   ① 生成后询问 = 每出一张图都要面对这个问题，对主流程是打扰；
 *   ② 独立入口是"主动找"，逻辑清楚；
 *   ③ 最重要：**对现有 Reskin 代码零侵入** ——
 *      原有的单图创作、工坊、仓库全部一行不改。
 *
 * ★ 这里**不展示三段式契约**（那是内部机制，不属于对外呈现）。
 *   对外只讲：原图 → 成品 → 可打开的画册。
 *
 * @param {Array} items [{ file, src, generated, place, title, story, family }]
 */
export default function Showcase({ items = [], onClose, onTryStyle, families = [],
  defaultMode = 'grid', onModeChange }) {
  const [showcaseMode, setShowcaseMode] = useState(defaultMode)
  const [openIdx, setOpenIdx] = useState(-1)   // 打开画册的起始页
  const [cmpIdx, setCmpIdx] = useState(-1)     // 打开原图/成品对比的项
  const [albumOpen, setAlbumOpen] = useState(false)
  // ★ 大图展播（拖动翻页）与成品编辑
  const [stageIdx, setStageIdx] = useState(-1)
  const [editIdx, setEditIdx] = useState(-1)
  // ★ 可编辑的数据源：用户改过的文案要能覆盖 manifest里的原值
  const [overrides, setOverrides] = useState({})
  const [regen, setRegen] = useState({})   // 重新生成的成品覆盖
  // ★ 展示层兜底：记录加载失败的图，换成占位块而不是留空白。
  //   答辩现场什么都可能发生（图被清、路径变了、CDN 抽风），
  //   **白屏或空洞是不可接受的** —— 宁可显示"这张暂不可显示"。
  // ★ 每张卡片的翻页进度（0=实拍 1=成品），默认全看成品
  const [flipPct, setFlipPct] = useState({})
  const [broken, setBroken] = useState(() => new Set())
  const markBroken = (key) =>
    setBroken((prev) => (prev.has(key) ? prev : new Set(prev).add(key)))

  // ★ 合并用户编辑与重新生成的结果 —— 编辑要真的改到数据上，
  //   否则"保存"只是关掉弹窗，用户看不出任何变化。
  const ready = useMemo(
    () => items
      .map((it, i) => ({
        ...it,
        ...(overrides[i] || {}),
        generated: regen[i] || it.generated,
      }))
      .filter((it) => it.generated),
    [items, overrides, regen],
  )
  const cmpItem = cmpIdx >= 0 ? ready[cmpIdx] : null

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape' && !albumOpen && cmpIdx < 0) onClose?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [albumOpen, cmpIdx, onClose])

  if (!ready.length) return null

  // 画册页序：封面 → 内容 → 封底（AlbumViewer 内部会补齐对开）
  // ★ 只放没坏的那几张 —— 否则翻页会翻到一张空白，比少一页糟得多
  const albumPages = ready
    .filter((_, i) => !broken.has(`g${i}`))
    .map((it) => ({
      src: imgSrc(it.generated), place: it.place, title: it.title, story: it.story,
    }))

  return (
    <div className="showcase" role="dialog" aria-label="成品集">
      <style>{`
        .showcase{position:fixed;inset:0;z-index:65;overflow:auto;
              background:
                radial-gradient(110% 70% at 50% 0%, #f6f1e6 0%, #ece5d6 46%, #ddd3c0 100%);
              color:#40382e;animation:sc-fade .3s ease both}
        @keyframes sc-fade{from{opacity:0}to{opacity:1}}
        .showcase-inner{width:min(1180px,100% - 40px);margin:0 auto;padding:34px 0 70px}

        .sc-head{display:flex;align-items:flex-end;gap:20px;flex-wrap:wrap;
              padding-bottom:20px;border-bottom:1px solid #cfc4ae;margin-bottom:30px}
        .sc-head h1{margin:0;font-size:29px;font-weight:600;letter-spacing:.04em}
        .sc-head p{margin:6px 0 0;font-size:13.5px;line-height:1.7;color:#6d6252;max-width:62ch}
        .sc-close{margin-left:auto;border:1px solid #b9ab90;background:transparent;
              color:#5b5142;border-radius:999px;padding:8px 17px;cursor:pointer;font-size:13px}
        .sc-close:hover{background:rgba(255,255,255,.5)}

        /* ★ 入口卡片：画册与系列成品并列，各占一个明确的入口
           —— 用户反馈「画册太隐蔽，藏在小按钮里等于没有」。*/
        .sc-gates{display:grid;gap:14px;margin-bottom:30px;
              grid-template-columns:repeat(auto-fit,minmax(232px,1fr))}
        .sc-gate{display:flex;align-items:center;gap:14px;text-align:left;
              padding:16px 18px;border-radius:4px;cursor:pointer;
              background:linear-gradient(180deg,#fdfaf3,#f3ecdc);
              border:1px solid #d6c9ae;
              box-shadow:0 2px 8px rgb(90 70 40 / .07);
              transition:transform .18s cubic-bezier(.3,0,.2,1),
                         box-shadow .18s ease,border-color .18s}
        .sc-gate:hover:not(:disabled){
              transform:translateY(-2px);border-color:#b8a888;
              box-shadow:0 12px 28px rgb(80 62 36 / .16)}
        .sc-gate:disabled{opacity:.46;cursor:default}
        .sc-gate-ico{font-size:26px;line-height:1;flex:none}
        .sc-gate-txt{display:grid;gap:4px;min-width:0}
        .sc-gate-txt b{font-size:15.5px;font-weight:600;color:#3f382e;letter-spacing:.02em}
        .sc-gate-txt i{font-style:normal;font-size:12px;color:#7d7160;line-height:1.5}
        /* 画册那张稍作强调 —— 它是用户明确要求「独立成功能」的那个 */
        .sc-gate--album{background:linear-gradient(180deg,#fffaf0,#f5ecda);
              border-color:#c0ad8b}
        .sc-gate--try{background:linear-gradient(180deg,#f7f4ec,#efe9dc)}

        .sc-actions{display:flex;gap:11px;flex-wrap:wrap;margin-bottom:28px}
        .sc-btn{border:1px solid #a99578;background:#fdfaf3;color:#4a4133;border-radius:999px;
              padding:10px 19px;cursor:pointer;font-size:13.5px;transition:transform .16s,box-shadow .16s}
        .sc-btn:hover{transform:translateY(-1px);box-shadow:05px 16px rgba(80,62,36,.16)}
        .sc-btn--primary{background:#463d31;color:#f7f2e6;border-color:#463d31}

        .sc-grid{display:grid;gap:26px;
              grid-template-columns:repeat(auto-fill,minmax(258px,1fr))}
        .sc-card{background:#fdfaf3;border:1px solid #ded4c0;border-radius:3px;overflow:hidden;
              display:flex;flex-direction:column;cursor:pointer;
              box-shadow:0 2px 10px rgba(90,70,40,.07);
              transition:transform .2s cubic-bezier(.3,0,.2,1),box-shadow .2s}
        .sc-card:hover{transform:translateY(-4px);box-shadow:0 14px 34px rgba(80,62,36,.19)}
        /* ★ 卡内翻页对比：两张图融合同框，滑动跟手 */
        .flipcmp{position:absolute;inset:0;overflow:hidden;cursor:ew-resize;
              touch-action:pan-y;user-select:none}
        /* ★ 吸附要有动画：松手滑到 0%/100% 是"翻到位"，
           瞬间跳会看起来像卡了一下 */
        .flipcmp__layer,.flipcmp__handle{transition:width .28s ease,left .28s ease}
        /* ★ 但拖动中必须关掉过渡，否则会"追不上手" —— 这是跟手的关键 */
        .flipcmp.is-dragging .flipcmp__layer,
        .flipcmp.is-dragging .flipcmp__handle{transition:none}
        .flipcmp__img{position:absolute;inset:0;width:100%;height:100%;
              object-fit:cover;display:block;pointer-events:none}
        .flipcmp__layer{position:absolute;top:0;left:0;bottom:0;overflow:hidden;
              border-right:2px solid #f7f1e3}
        .flipcmp__ph{position:absolute;inset:0;display:grid;place-items:center;
              background:#efe8d8;color:#8b8071;font-size:12px;letter-spacing:.06em}
        .flipcmp__handle{position:absolute;top:0;bottom:0;width:0;
              display:grid;place-items:center;pointer-events:none;z-index:3}
        .flipcmp__grip{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;
              background:rgba(28,24,18,.74);color:#f5efe1;font-size:12px;
              transform:translateX(-50%);box-shadow:0 2px 8px rgba(0,0,0,.3)}
        .flipcmp__tag{position:absolute;bottom:8px;z-index:4;padding:3px 9px;border-radius:999px;
              background:rgba(28,24,18,.7);color:#f5efe1;font-size:10.5px;letter-spacing:.1em;
              transition:opacity .22s ease;pointer-events:none}
        .flipcmp__tag--l{left:8px}
        .flipcmp__tag--r{right:8px}
        .flipcmp__tag--off{opacity:0}
        /* 卡片上的翻页按钮 */
        .sc-flipbtns{position:absolute;top:9px;right:9px;z-index:6;display:flex;gap:5px}
        .sc-flipbtns button{border:1px solid rgb(255 255 255 /.5);background:rgba(28,24,18,.66);
              color:#f3ecdd;border-radius:999px;padding:4px 9px;font-size:10.5px;
              letter-spacing:.06em;cursor:pointer;backdrop-filter:blur(4px)}
        .sc-flipbtns button:hover{background:rgba(28,24,18,.86)}
        /* 编辑按钮：与两个翻页按钮区分开，避免误触 */
        .sc-flipbtns .sc-flip-edit{background:rgba(70 61 49 / .84)}
        .sc-flipbtns .sc-flip-edit:hover{background:#3f382e}

        .sc-thumb{position:relative;aspect-ratio:3/5;overflow:hidden;background:#efe8d8}
        .sc-thumb img{width:100%;height:100%;object-fit:cover;display:block}
        /* ★ 图片加载失败时的占位 —— 宁可显示这句话，也不留空白 */
        .sc-placeholder{width:100%;height:100%;display:grid;place-items:center;
              background:repeating-linear-gradient(45deg,#efe8d8,#efe8d8 8px,#e7dfcb 8px,#e7dfcb 16px);
              color:#8b8071;font-size:12.5px;letter-spacing:.08em}
        .sc-badge{position:absolute;top:9px;left:9px;background:rgba(28,24,18,.72);color:#f3ecdd;
              font-size:10.5px;letter-spacing:.1em;padding:4px 9px;border-radius:3px}
        .sc-meta{padding:14px 15px 16px}
        .sc-place{font-size:11px;letter-spacing:.16em;color:#93866f}
        .sc-title{margin:5px 0 5px;font-size:16.5px;font-weight:600;line-height:1.32}
        .sc-story{margin:0;font-size:12.5px;line-height:1.66;color:#6b6151}
        .sc-foot{display:flex;gap:8px;padding:0 15px 15px;margin-top:auto}
        .sc-mini{flex:1;border:1px solid #cbbfa8;background:#f6f1e4;color:#5b5142;
              border-radius:4px;padding:7px 0;font-size:12px;cursor:pointer}
        .sc-mini:hover{background:#efe7d5}
        .sc-mini--solid{background:#4a4133;color:#f7f2e6;border-color:#4a4133}

        .sc-note{margin-top:38px;padding:19px 22px;background:rgba(255,253,246,.68);
              border:1px solid #d9cfb8;border-radius:3px;font-size:12.5px;line-height:1.85;color:#6a6151}
        .sc-note b{color:#463d31}

        /* 原图 / 成品 对比 */
        .sc-cmp{position:fixed;inset:0;z-index:75;display:grid;place-items:center;
              background:rgba(20,17,13,.88);backdrop-filter:blur(4px)}
        .sc-cmp-box{position:relative;width:min(880px,92vw)}
        .sc-cmp-box h3{margin:0 0 14px;color:#f2ebdd;font-size:15px;font-weight:500;letter-spacing:.08em}
        .sc-cmp-hint{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
              color:#f7f1e3;font-size:12.5px;letter-spacing:.14em;pointer-events:none;
              background:rgba(20,17,13,.62);padding:7px 15px;border-radius:999px}
        .sc-cmp-x{position:absolute;top:-40px;right:0;border:0;background:transparent;
              color:#efe7d7;font-size:15px;cursor:pointer}

        @media (max-width:640px){
          .sc-head h1{font-size:23px}
        }
      `}</style>

      <div className="showcase-inner">
        <div className="sc-head">
          <div>
            <h1>成品集</h1>
            <p>
              同一趟江南水乡之旅，五处见闻。左侧是实拍摄影，
              右侧是经系统转换后的成品 —— <b>建筑的轮廓、屋脊的走向、桥与塔的相对位置全部保留</b>。
            </p>
          </div>
          <button type="button" className="sc-close" onClick={onClose}>关闭</button>
        </div>

        {/* ★ 两个并列入口卡片：画册是独立功能，不该藏在小按钮里 */}
        <div className="sc-gates">
          <button type="button" className="sc-gate"
                  onClick={() => setShowcaseMode('grid')}>
            <span className="sc-gate-ico">🖼</span>
            <span className="sc-gate-txt">
              <b>系列成品</b>
              <i>{ready.length} 张 · 点开看大图，可拖动翻页</i>
            </span>
          </button>
          <button type="button" className="sc-gate sc-gate--album"
                  disabled={albumPages.length === 0}
                  onClick={() => { setOpenIdx(0); setAlbumOpen(true) }}>
            <span className="sc-gate-ico">📖</span>
            <span className="sc-gate-txt">
              <b>电子画册</b>
              <i>{albumPages.length ? `${albumPages.length} 页 · 双页翻书` : '暂无可用'}</i>
            </span>
          </button>
          {onTryStyle && (
            <button type="button" className="sc-gate sc-gate--try"
                    onClick={() => onTryStyle(ready[0])}>
              <span className="sc-gate-ico">✍</span>
              <span className="sc-gate-txt">
                <b>用这个风格试一张</b>
                <i>回到创作页，预选该家族</i>
              </span>
            </button>
          )}
        </div>

        <div className="sc-grid">
          {ready.map((it, i) => (
            <div key={i} className="sc-card" onClick={() => setStageIdx(i)}>
              {/* ★ 卡内翻页：滑动或点箭头在「实拍 ↔ 成品」之间翻动。
                  两张图融合同框、跟手移动 —— 不用点按钮进弹层。*/}
              <div className="sc-thumb">
                {broken.has(`g${i}`) ? (
                  <div className="sc-placeholder">成品暂不可显示</div>
                ) : (
                  <>
                    <FlipCompare
                      srcLeft={it.file}
                      srcRight={it.generated}
                      pct={flipPct[i] ?? 1}
                      onPct={(v) => setFlipPct((s) => ({ ...s, [i]: v }))}
                      leftBroken={broken.has(`o${i}`)}
                      rightBroken={broken.has(`g${i}`)}
                      onLeftError={() => markBroken(`o${i}`)}
                      onRightError={() => markBroken(`g${i}`)}
                      altLeft={`${it.title} 实拍摄影`}
                      altRight={`${it.title} 系统成品`}
                    />
                    <div className="sc-badge">{it.familyName || '拾景纸刊'}</div>
                    <div className="sc-flipbtns">
                      <button type="button"
                              onClick={(e) => { e.stopPropagation(); setFlipPct((s) => ({ ...s, [i]: 0 })) }}
                              title="看实拍摄影">◀ 实拍</button>
                      <button type="button"
                              onClick={(e) => { e.stopPropagation(); setFlipPct((s) => ({ ...s, [i]: 1 })) }}
                              title="看系统成品">成品 ▶</button>
                      <button type="button" className="sc-flip-edit"
                              onClick={(e) => { e.stopPropagation(); setEditIdx(i) }}
                              title="编辑这张成品">✎</button>
                    </div>
                  </>
                )}
              </div>
              <div className="sc-meta">
                <div className="sc-place">{it.place}</div>
                <div className="sc-title">{it.title}</div>
                <p className="sc-story">{it.story}</p>
              </div>
              <div className="sc-foot">
                <button type="button" className="sc-mini"
                        onClick={(e) => { e.stopPropagation(); setCmpIdx(i) }}>
                  原图对比
                </button>
                <button type="button" className="sc-mini sc-mini--solid"
                        onClick={(e) => { e.stopPropagation(); setOpenIdx(i); setAlbumOpen(true) }}>
                  画册中查看
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="sc-note">
          <b>关于素材</b>：本组照片来自 Pexels 免费图库（Pexels License，可商用），
          作为系统的真实输入样本，用于验证在实拍摄影上的处理效果；
          <b>本作品集的系统产出是「转换后的成品」</b>。
          转换规则与全部家族可在创作页查看与试用。
        </div>
      </div>

      {/* ★ 大图展播：拖动翻页切上一张/下一张 */}
      {stageIdx >= 0 && (
        <StageView
          items={ready.map((it, i) => ({
            ...it,
            flipPct: flipPct[i] ?? 1,
            onFlip: (v) => setFlipPct((s) => ({ ...s, [i]: v })),
            leftBroken: broken.has(`o${i}`),
            onLeftError: () => markBroken(`o${i}`),
            broken: broken.has(`g${i}`),
          }))}
          index={stageIdx}
          onIndex={setStageIdx}
          onClose={() => setStageIdx(-1)}
          onEdit={(it) => setEditIdx(ready.findIndex((x) => x.generated === it.generated))}
          onCompare={(i) => { setStageIdx(-1); setCmpIdx(i) }}
          onOpenAlbum={(i) => { setOpenIdx(i); setStageIdx(-1); setAlbumOpen(true) }}
          onTryStyle={onTryStyle}
        />
      )}

      {/* ★ 成品编辑器 */}
      {editIdx >= 0 && ready[editIdx] && (
        <AssetEditor
          item={ready[editIdx]}
          families={families}
          onClose={() => setEditIdx(-1)}
          onSave={(v) => { setOverrides((s) => ({ ...s, [editIdx]: v })); setEditIdx(-1) }}
          onRegenerate={(v) => { setRegen((s) => ({ ...s, [editIdx]: v.generated })); setEditIdx(-1) }}
        />
      )}

      {cmpItem && <Compare cmp={cmpItem} onClose={() => setCmpIdx(-1)} />}

      {/* ★ 兜底：一张能显示的都没有时，不放空画册，改成一句说明。
          空画册翻出来全是白纸，比"暂不可用"糟糕得多。 */}
      {albumOpen && albumPages.length > 0 && (
        <AlbumViewer pages={albumPages} onClose={() => setAlbumOpen(false)} />
      )}
      {albumOpen && albumPages.length === 0 && (
        <div className="sc-cmp" onClick={() => setAlbumOpen(false)}>
          <div className="sc-cmp-box" onClick={(e) => e.stopPropagation()}>
            <button type="button" className="sc-cmp-x"
                    onClick={() => setAlbumOpen(false)}>✕ 关闭</button>
            <h3 style={{ textAlign: 'center', padding: '40px 0' }}>
              画册暂不可用（没有可显示的成品）
            </h3>
          </div>
        </div>
      )}
    </div>
  )
}

/** 原图 ↔ 成品 滑动对比 —— 让评委亲手验证「地标没被改」 */
function Compare({ cmp, onClose }) {
  const [pct, setPct] = useState(50)
  const [srcErr, setSrcErr] = useState(false)
  const [genErr, setGenErr] = useState(false)
  // ★ 踩过的坑：容器原本写死 aspectRatio:'3/5'，但**素材有横有竖**。
  //   横图（如photo4 是 2000x1500）在 3:5 容器里 object-fit:cover
  //   会被裁得只剩顶部天空 —— 左右两半对不上，对比功能等于废了。
  //   ⇒ 容器比例按**原图实际比例**走，并让成品层完全跟随同一比例。
  const ratio = `${cmp.w || 3} / ${cmp.h || 5}`
  return (
    <div className="sc-cmp" onClick={onClose}>
      <div className="sc-cmp-box" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="sc-cmp-x" onClick={onClose}>✕ 关闭</button>
        <h3>{cmp.place} · {cmp.title} —— 拖动中间竖线对比</h3>
        <div style={{ position: 'relative', aspectRatio: ratio, overflow: 'hidden',
                      borderRadius: 3, background: '#efe8d8',
                      maxHeight: '74vh', margin: '0 auto' }}>
          {/* 底层：原图（坏了就显示占位，不露黑底） */}
          {srcErr ? (
            <div style={{ position: 'absolute', inset: 0, display: 'grid',
                          placeItems: 'center', color: '#9c8f7c', fontSize: 13 }}>
              原图暂不可显示
            </div>
          ) : (
            <img src={imgSrc(cmp.file)} alt="原图"
                 onError={() => setSrcErr(true)}
                 style={{ position: 'absolute', inset: 0, width: '100%', height: '100%',
                          objectFit: 'fill' }} />
          )}
          {/* 上层：成品，按百分比裁切 —— 比例与底层一致，所以能严丝合缝对齐 */}
          <div style={{ position: 'absolute', inset: 0, width: `${pct}%`,
                        overflow: 'hidden', borderRight: '2px solid #f3ecdd' }}>
            {genErr ? (
              <div style={{ position: 'absolute', inset: 0, display: 'grid',
                            placeItems: 'center', color: '#9c8f7c', fontSize: 13 }}>
                成品暂不可显示
              </div>
            ) : (
              <img src={imgSrc(cmp.generated)} alt="成品"
                   onError={() => setGenErr(true)}
                   style={{ position: 'absolute', inset: 0, width: '100%',
                            height: '100%', objectFit: 'fill' }} />
            )}
          </div>
          <div className="sc-cmp-hint">{pct}%</div>
          <input type="range" min="0" max="100" value={pct}
                 onChange={(e) => setPct(Number(e.target.value))}
                 aria-label="对比滑块"
                 style={{ position: 'absolute', left: 0, right: 0, bottom: 0,
                          width: '100%', opacity: 0, cursor: 'ew-resize', height: '100%' }} />
        </div>
        <div style={{ marginTop: 12, fontSize: 12, color: '#bdb2a0', letterSpacing: '.06em' }}>
          左：实拍摄影　右：系统成品
          {cmp.familyName ? `　·　风格：${cmp.familyName}` : ''}
        </div>
      </div>
    </div>
  )
}