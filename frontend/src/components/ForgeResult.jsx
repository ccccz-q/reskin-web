/**
 * 工坊右栏：当前版本的展示、迭代入口、提示词面板。
 *
 * ★ 为什么要拆出来：这是整个工坊最重的一块展示（版本条 / 家族卡 /
 *   三段式 / 提示词编辑），但它**不含任何请求编排** —— 全部动作
 *   由父组件传入。拆开后「点了会发生什么」在 hook 里、
 *   「长什么样」在这里，两边可以各自审。
 *
 * ★ 纪律：className 与文案逐字照搬原文件。
 */
export default function ForgeResult(props) {
  const {
    current, spec, seg, failNoteText, versions, feedback,
    revising, copying, copied, installing,
    showPrompt, promptEditing, promptDraft, promptSaving,
    onFeedback, onLoadVersion, onReviseClick, onCopy, onInstall,
    onPickExample, onTogglePrompt, onEditPrompt, onCancelEdit,
    onPromptDraft, onSavePrompt, onRestorePrompt,
  } = props

  if (!current) {
    return (
      <section className="ws-col ws-out">
        <p className="muted ws-empty">
          左边填好理论（或传几张参考图）后点「开始提炼」。<br />
          一次调用大概十几秒，会产出一套完整的三段式提示词结构。
        </p>
      </section>
    )
  }

  return (
    <section className="ws-col ws-out">
      <div className="ws-ver-bar" role="group" aria-label="迭代版本">
        {versions.map((v) => (
          <button type="button" key={v.id}
                  className={`ws-ver ${v.id === current.id ? 'on' : ''}`}
                  aria-pressed={v.id === current.id}
                  onClick={() => onLoadVersion(v.id)}>
            v{v.version}
          </button>
        ))}
        <span className="muted">共 {versions.length} 版</span>
      </div>

      <div className="ws-spec">
        <span className="ws-icon" aria-hidden="true">{spec?.icon || '◧'}</span>
        <div>
          <div className="ws-name">{spec?.name || '未命名'}</div>
          <div className="muted">{spec?.description}</div>
          <div className="ws-tags">
            <span className="tag">{spec?.layout}</span>
            <span className="tag">{spec?.forbid_scope}</span>
            <span className="tag">{spec?.id}</span>
          </div>
        </div>
      </div>

      {failNoteText && (
        <div className="notice bad" role="alert">
          ⛔ 这一版提炼失败：{failNoteText}
          <br />瞬时网络错误已支持自动重试 —— 请回到左侧点「重新提炼」再试一次。
        </div>
      )}
      {current.errors?.length > 0 && (
        <div className="notice bad" role="alert">
          校验未通过：{current.errors.join(' / ')}
        </div>
      )}
      {current.warnings?.length > 0 && (
        <div className="notice warn">⚠ {current.warnings.join(' / ')}</div>
      )}

      {seg && (
        <div className="segments">
          <div className="seg preserve">
            <span className="seg-tag">保留</span><p>{seg.preserve}</p>
          </div>
          <div className="seg creative">
            <span className="seg-tag">创作</span><p>{seg.creative}</p>
          </div>
          <div className="seg forbid">
            <span className="seg-tag">禁止</span><p>{seg.forbid}</p>
          </div>
        </div>
      )}

      <label className="ws-label" htmlFor="ws-f-fb">对这一版的修改意见（左侧贴了新图会一并做视觉识别）</label>
      <textarea id="ws-f-fb" className="ws-input" rows={3} value={feedback}
                onChange={(e) => onFeedback(e.target.value)}
                placeholder={'例如：按照理想图保留顶部 YOU DIED 界面的位置、布局和样式，文字纯白、按钮灰色石质。\n左侧贴了新参考图（如理想效果图）时，迭代会以它为识别对象。'} />
      <div className="ws-actions">
        <button type="button" className="btn ghost" onClick={onReviseClick}
                disabled={revising || !feedback.trim() || !!failNoteText || !spec}>
          {revising ? '修改中…' : '迭代一版'}
        </button>
        {/* ★ 提炼中不再锁这里的操作：复制/安装只看这一版自己的数据，
            与后台有没有任务在跑无关 */}
        <button type="button"
                className={`copy-btn ${copied ? 'done' : ''}`}
                onClick={onCopy} disabled={copying || !current.prompt}
                title="复制当前版本的完整提示词">
          <span className="copy-ico" aria-hidden="true">{copied ? '✓' : '⧉'}</span>
          {copied ? '已复制' : '复制提示词'}
        </button>
        <button type="button" className="btn solid" onClick={onInstall}
                disabled={installing || !current.ok}>
          {installing ? '安装中…' : '安装为家族'}
        </button>
        <button type="button" className="btn ghost" onClick={onPickExample}
                disabled={!(spec?.id || current?.family_id)}
                title="从作品仓库选一张图，作为这个家族的示例图">
          🖼 设置示例图
        </button>
        <button type="button" className="ws-prompt-toggle"
                onClick={onTogglePrompt}>
          {showPrompt ? '收起提示词 ▴' : '查看提示词 ▾'}
        </button>
      </div>

      {showPrompt && (
        <div className="ws-prompt-panel">
          <div className="ws-prompt-head">
            <span className={`ws-prompt-badge ${current.prompt_override ? 'manual' : 'auto'}`}>
              {current.prompt_override ? '✎ 已手动修改' : '⚙ 自动渲染'}
            </span>
            {!promptEditing && (
              <span className="ws-prompt-tools">
                <button type="button" className="link-btn"
                        onClick={onEditPrompt}
                        disabled={!current.prompt && !current.prompt_override}>
                  修改提示词
                </button>
                {current.prompt_override && (
                  <button type="button" className="link-btn"
                          onClick={onRestorePrompt}
                          disabled={promptSaving}>
                    恢复自动渲染
                  </button>
                )}
              </span>
            )}
          </div>
          {promptEditing ? (
            <>
              <textarea className="ws-input ws-prompt-editor" rows={14}
                        value={promptDraft}
                        onChange={(e) => onPromptDraft(e.target.value)}
                        spellCheck={false} />
              <div className="ws-prompt-tools" style={{ marginTop: 8 }}>
                <button type="button" className="btn solid" onClick={() => onSavePrompt(promptDraft)}
                        disabled={promptSaving}>
                  {promptSaving ? '保存中…' : '保存修改'}
                </button>
                <button type="button" className="btn ghost" onClick={onCancelEdit}
                        disabled={promptSaving}>
                  取消
                </button>
                <span className="muted">保存后生成图片时直接使用这份文本，无需重新提炼</span>
              </div>
            </>
          ) : current.prompt
            ? <pre className="ws-prompt-text">{current.prompt}</pre>
            : <p className="muted">这一版还没渲染出提示词（可能校验未通过）。</p>}
          <p className="ws-prompt-note">
            {current.prompt_override
              ? '这份提示词被你手动改过 —— 生成图片时直接使用它（参数调节暂不参与）；「恢复自动渲染」可回到模板自动生成。'
              : '这是最终发给生图模型的三段式全文。只改个别片段：点「修改提示词」直接编辑保存，立即生效；整版都不满意才需要「迭代一版」（重新提炼约 4-6 分钟）。'}
          </p>
        </div>
      )}
      <p className="ws-note">
        每一版都会自动存进「我的库」，随时可以翻回更早的版本。
        安装后会写进家族目录，左侧列表立即出现。
      </p>
    </section>
  )
}
