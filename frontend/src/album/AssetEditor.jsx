import { useState } from 'react'
import { api } from '../api'
import { imgSrc } from '../api'

/**
 * 成品编辑器
 *
 * ★ 用户要求（2026-10-09）：「对于成品是可以进行编辑的，目前没有可编辑的按钮」
 *
 * ★ 编辑分两类（因为「编辑一张图」在工程上有两条完全不同的路）：
 *   ① **改文案** —— 标题 / 地点 / 故事。
 *      这是"这张成品上写什么"，纯前端，立即生效。
 *      ★ 这类是零成本、零风险的，必须先有。
 *   ② **换风格重生成** —— 换家族再跑一次。
 *      这是"用另一套视觉语言重做"，要走后端生图（约 30 秒 + 配额）。
 *      ★ 所以要**明确告知代价**，不能让用户以为是"重新排版"。
 *
 * ★ 为什么不给「局部重绘/擦除」：
 *   那是图像编辑能力的另一套（需要蒙版与重绘通道），
 *   与本项目主张的「结构忠实」相冲突 ——
 *   局部擦除恰恰会破坏结构。**不做，是有理由的，不是没做。**
 */
export default function AssetEditor({ item, families = [], onClose, onSave, onRegenerate }) {
  const [place, setPlace] = useState(item.place || '')
  const [title, setTitle] = useState(item.title || '')
  const [story, setStory] = useState(item.story || '')
  const [fid, setFid] = useState(item.family || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState('text')

  const dirtyText = place !== (item.place || '')
    || title !== (item.title || '')
    || story !== (item.story || '')
  const familyChanged = fid && fid !== item.family

  const saveText = () => {
    onSave?.({ ...item, place: place.trim(), title: title.trim(), story: story.trim() })
    onClose?.()
  }

  const doRegenerate = async () => {
    setBusy(true); setErr('')
    try {
      const res = await api.render(fid, {}, { extraPrompt: story.trim() })
      if (res?.url) onRegenerate?.({ ...item, family: fid, generated: res.url })
      else setErr('生成失败：没有拿到成品')
    } catch (e) {
      setErr(`生成失败：${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="asredt" role="dialog" aria-label="编辑成品"
         onClick={onClose}>
      <div className="asredt-box" onClick={(e) => e.stopPropagation()}>
        <div className="asredt-head">
          <b>编辑成品</b>
          <button type="button" className="asredt-x" onClick={onClose}
                  aria-label="关闭编辑器" title="关闭">✕</button>
        </div>

        <div className="asredt-tabs">
          <button type="button"
                  className={tab === 'text' ? 'is-on' : ''}
                  onClick={() => setTab('text')}>改文案</button>
          <button type="button"
                  className={tab === 'style' ? 'is-on' : ''}
                  onClick={() => setTab('style')}>换风格重做</button>
        </div>

        {tab === 'text' ? (
          <div className="asredt-body">
            <label>
              <span>地点</span>
              <input value={place} onChange={(e) => setPlace(e.target.value)}
                     placeholder="如：江苏 苏州" maxLength={24} />
            </label>
            <label>
              <span>标题</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder="如：水乡 · 石拱桥" maxLength={24} />
            </label>
            <label>
              <span>一句话说明</span>
              <textarea value={story} onChange={(e) => setStory(e.target.value)}
                        rows={4} maxLength={120}
                        placeholder="不超过 120 字，画册里会显示在这里" />
            </label>
            <p className="asredt-note">
              改文案只影响这张成品上的文字，不会重新生图 —— 因此即时生效、零成本。
            </p>
          </div>
        ) : (
          <div className="asredt-body">
            <label>
              <span>换一个风格家族重做</span>
              <select value={fid} onChange={(e) => setFid(e.target.value)}>
                <option value="">请选择…</option>
                {families.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}{f.id === item.family ? '（当前）' : ''}
                  </option>
                ))}
              </select>
            </label>
            <div className="asredt-warn">
              ⚠ 这一步会重新调用生图：约 30 秒，并消耗一次生成额度。
              <br />
              新图会覆盖当前这张（原始成品仍可在作品仓库找回）。
            </div>
            {err && <div className="asredt-err">{err}</div>}
          </div>
        )}

        <div className="asredt-foot">
          {tab === 'text' ? (
            <>
              <button type="button" className="asredt-btn" onClick={onClose}>取消</button>
              <button type="button" className="asredt-btn is-primary"
                      onClick={saveText} disabled={!dirtyText}>
                保存
              </button>
            </>
          ) : (
            <>
              <button type="button" className="asredt-btn" onClick={onClose}>取消</button>
              <button type="button" className="asredt-btn is-primary"
                      onClick={doRegenerate}
                      disabled={!familyChanged || busy}>
                {busy ? '生成中…' : '重新生成'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}