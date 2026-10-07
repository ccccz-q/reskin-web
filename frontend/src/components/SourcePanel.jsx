import { imgSrc } from '../api'

/**
 * 原图上传区（拖拽 / 点选 / 粘贴的落点）。
 *
 * ★ 为什么「选文件」是真正的 <button> 而不是 div + onClick：
 *   div 上的 onClick 键盘永远够不到 —— 拖拽能用，键盘用户直接被卡死。
 *   拖拽监听仍在整块上（dragover 拿不到可靠的「是否在元素内」判断），
 *   所以 div 保留 dropzone 角色，键盘入口走真按钮。
 */
export default function SourcePanel({ source, orientation, dropRef, fileRef, onPick, onClear }) {
  return (
    <div className={`dropzone ${source ? 'has' : 'empty'}`} ref={dropRef}>
      {source ? (
        <div className="src-card">
          <img className="src-thumb" src={imgSrc(source.url)}
               alt={`已上传的原图 ${source.filename}`}
               width={source.width} height={source.height} decoding="async" />
          <div className="src-info">
            <span className="src-name" title={source.filename}>{source.filename}</span>
            <span className="src-dims">
              {source.width}×{source.height} · {orientation} · {source.format}
            </span>
          </div>
        </div>
      ) : (
        <div className="dz-hint">
          <span className="dz-icon" aria-hidden="true">＋</span>
          <span className="dz-main">把照片拖到这里</span>
          <span className="dz-sub">JPG / PNG / WebP，≤20MB</span>
        </div>
      )}
      <div className="dz-actions">
        <button type="button" className="btn ghost dz-pick"
                onClick={() => fileRef.current?.click()}>
          {source ? '更换' : '选择文件'}
        </button>
        {source && (
          <button type="button" className="btn ghost dz-clear" onClick={onClear}>
            移除
          </button>
        )}
      </div>
      <input ref={fileRef} id="file" type="file" accept="image/*" hidden tabIndex={-1}
             onChange={(e) => onPick(e.target.files?.[0])} />
    </div>
  )
}