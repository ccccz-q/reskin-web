/**
 * 底部对话抽屉
 *
 * ★ 为什么做成抽屉：对话是「另一种入口」，不是出图的必经步骤，却很高很长。
 *   塞进右栏会和提示词检查器抢空间；常驻展开又会把首屏挤没。
 *   所以默认只占一条（显示最近一句回复的前 60 字），想聊再拉起来。
 *
 * ★ 对话只调参数、不出图 —— 出图永远是画布下方那个唯一的结算按钮。
 *   所以空态里要把这句话说清楚，免得用户以为发消息就能出图。
 */
export default function ChatDock({ open, messages, input, busy, peek, onToggle, onInput, onSend }) {
  return (
    <div className={`dock ${open ? 'open' : ''}`}>
      <h2 className="sr-only">Agent 对话</h2>
      <button type="button" className="dock-tab"
              aria-expanded={open} aria-controls="dock-body"
              onClick={onToggle}>
        <span className="dock-caret" aria-hidden="true">▲</span>
        <span className="dock-title">Agent 对话</span>
        <span className="dock-peek">
          {peek ? peek.slice(0, 60) : '说出想要的效果，让它自己选家族、调参数'}
        </span>
      </button>

      {open && (
        <div className="dock-body" id="dock-body">
          <div className="chat" role="log" aria-live="polite" aria-busy={busy}>
            {messages.length === 0 && (
              <p className="muted">
                对话只调参数、不出图 —— 要出图用画布下方的「生成图片」。
              </p>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}><span>{m.text}</span></div>
            ))}
            {busy && <div className="msg assistant"><span>工作中…</span></div>}
          </div>

          <div className="composer">
            <label className="sr-only" htmlFor="chat-input">对 Agent 说</label>
            <input id="chat-input" value={input} placeholder="描述你想要的效果…"
                   onChange={(e) => onInput(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && onSend()} />
            <button type="button" className="btn ghost" onClick={onSend}
                    disabled={busy || !input.trim()}
                    title="发送给 Agent（不会出图）">
              发送
            </button>
          </div>
        </div>
      )}
    </div>
  )
}