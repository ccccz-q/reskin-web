import { imgSrc } from '../api'
import { phaseLabel, elapsedLabel } from '../lib/forge'

/**
 * 工坊左栏：三路输入 + 参考图 + 提交 + 后台任务卡。
 *
 * ★ 为什么要拆出来：这块是纯展示（所有状态与动作都由父组件传入），
 *   拆开后Workshop 只剩「状态在哪、动作怎么编排」，
 *   而「输入区长什么样」不会再和轮询逻辑互相牵扯。
 *
 * ★ 纪律：className 与文案逐字照搬原文件。CSS 是按这些类名写的，
 *   改类名等于悄悄改样式，且冒烟测试发现不了。
 */
export default function ForgeInput(props) {
  const {
    theory, notes, name, stylePrompt, images, fileRef, refsDragging,
    submitting, hasCurrent, bgTasks,
    onName, onTheory, onNotes, onStylePrompt,
    onImages, onUploadRefs, onDragging, onDraft, onCancelTask,
  } = props

  return (
    <section className="ws-col ws-in">
      <label className="ws-label" htmlFor="ws-f-name">风格名称（可留空，让模型起名）</label>
      <input id="ws-f-name" className="ws-input" value={name}
             onChange={(e) => onName(e.target.value)}
             placeholder="例如：Risograph 孔版" />

      <label className="ws-label" htmlFor="ws-f-theory">风格理论 / 介绍（<b>可选</b>——不写就完全靠看图）</label>
      <textarea id="ws-f-theory" className="ws-input ws-theory" rows={9} value={theory}
                onChange={(e) => onTheory(e.target.value)}
                placeholder="把这种风格的起源、工艺、配色逻辑、美学主张贴进来。写得越具体，提炼出的三段式越准。" />

      <label className="ws-label" htmlFor="ws-f-sprompt">风格提示词（<b>可选</b>——把你收集的生图 Prompt 整包贴进来，正向词与 SD 反向词都可以）</label>
      <textarea id="ws-f-sprompt" className="ws-input" rows={6} value={stylePrompt}
                onChange={(e) => onStylePrompt(e.target.value)}
                placeholder={'例如：\n我的世界游戏截图，体素方块画风，所有物体由正方形方块拼接构成，方块云朵，明亮晴朗蓝天……\nSD反向词：blurry, deformed, non-block structure, watermark（反向词会被转成禁止项）'} />

      <span className="ws-label" id="ws-f-refs">参考图（<b>主角</b>，推荐同类型 3–6 张；支持拖拽图片到这里，或直接 Ctrl+V 粘贴）</span>
      <div className={`ws-refs ${refsDragging ? 'dragging' : ''}`} role="group" aria-labelledby="ws-f-refs"
           onDragOver={(e) => { e.preventDefault(); onDragging(true) }}
           onDragLeave={(e) => {
             // 只在真正离开容器时熄灭高亮（经过子元素时 relatedTarget 仍在容器内）
             if (!e.currentTarget.contains(e.relatedTarget)) onDragging(false)
           }}
           onDrop={(e) => {
             e.preventDefault()
             onDragging(false)
             if (e.dataTransfer?.files?.length) onUploadRefs(e.dataTransfer.files)
           }}>
        {images.map((im, i) => (
          <div key={i} className="ws-ref">
            <img src={imgSrc(im.url)} alt="" />
            <button type="button" aria-label={`移除第 ${i + 1} 张参考图`}
                    onClick={() => onImages((p) => p.filter((_, j) => j !== i))}>×</button>
          </div>
        ))}
        <button type="button" className="ws-ref-add" aria-label="添加参考图"
                onClick={() => fileRef.current?.click()}>+</button>
        <input ref={fileRef} type="file" accept="image/*" multiple hidden tabIndex={-1}
               onChange={(e) => onUploadRefs(e.target.files)} />
      </div>
      {/* 提炼耗时提示 */}
      <p className="ws-note">
        参考图 + 风格理论 + 风格提示词三路综合：图取色板与结构，理论给美学语义，
        提示词给精确的词汇与质感 —— 三样都是可选的，但至少给一样。
      </p>

      <label className="ws-label" htmlFor="ws-f-notes">补充说明（可留空）</label>
      <input id="ws-f-notes" className="ws-input" value={notes}
             onChange={(e) => onNotes(e.target.value)}
             placeholder="例如：只用于人物照、不要出现文字" />

      <button type="button" className="btn-generate" onClick={onDraft}
              disabled={submitting}>
        {/* ★ 不再因后台任务显示「提炼中…」—— 提炼在后台跑，这个按钮只是
            提交入口；旧文案会让用户把迭代/其它操作误读成「正在提炼」
            （实测 2026-10-03：用户点了迭代，按钮却写提炼中）。 */}
        {submitting ? '提交中…' : hasCurrent ? '重新提炼' : '开始提炼'}
      </button>
      {bgTasks.length > 0 && (
        <p className="ws-note">
          另有 {bgTasks.length} 个提炼任务在后台进行（见下方任务卡），可继续提交下一组。
        </p>
      )}

      {/* ── 后台任务实时卡：中止入口也在这里（2026-10-03 新增）── */}
      {bgTasks.length > 0 && (
        <div className="ws-tasks" role="status">
          {bgTasks.map((t) => (
            <div key={t.task_id} className="lib-task">
              <span className="lib-task-dot" aria-hidden="true" />
              <span className="lib-task-txt">
                {t.name ? `「${t.name}」` : ''}后台提炼中：{phaseLabel(t)}
                （已 {elapsedLabel(t.elapsed_sec)}s）
              </span>
              <button type="button" className="link ws-lib-del"
                      disabled={t.cancelling}
                      title="中止这个提炼任务（正在进行的模型调用跑完即终止）"
                      onClick={() => onCancelTask(t)}>
                {t.cancelling ? '中止中…' : '中止'}
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
