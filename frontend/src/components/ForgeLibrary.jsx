import { elapsedLabel, phaseLabel } from '../lib/forge'

/**
 * 工坊「我的库」页：历史版本列表 + 后台任务提醒。
 *
 * ★ 为什么后台任务卡在两个页签里各出现一次（原实现就是这样）：
 *   用户可能停在「我的库」页签上等结果 —— 那一刻他不会切回「提炼」，
 *   所以这里也必须能看到进度并能中止。抽成组件后两处共用同一套
 *   文案逻辑（phaseLabel / elapsedLabel），不再各写一遍内联表达式。
 */
export default function ForgeLibrary(props) {
  const { library, bgTasks, onOpen, onRemove, onCancelTask } = props

  return (
    <div className="ws-body ws-lib">
      {bgTasks.map((t) => (
        <div key={t.task_id} className="lib-task" role="status">
          <span className="lib-task-dot" aria-hidden="true" />
          <span>
            后台提炼中：{phaseLabel(t)}
            （已 {elapsedLabel(t.elapsed_sec)}s）
            —— 完成后会自动提醒，可以去别的页面
          </span>
          <button type="button" className="link ws-lib-del"
                  disabled={t.cancelling}
                  title="中止这个提炼任务（当前阶段结束后终止）"
                  onClick={() => onCancelTask(t)}>
            {t.cancelling ? '中止中…' : '中止'}
          </button>
        </div>
      ))}
      {library.length === 0 && bgTasks.length === 0 && <p className="muted">还没有创造过模板。</p>}
      {library.map((it) => (
        <div key={it.id} className="ws-lib-row">
          <span className="ws-lib-ver">v{it.version}</span>
          <span className="ws-lib-name">{it.name}</span>
          <span className="muted">{it.family_id}</span>
          {it.installed && <span className="tag">已安装</span>}
          <button type="button" className="link"
                  aria-label={`打开 ${it.name} 第 ${it.version} 版`}
                  onClick={() => onOpen(it)}>
            打开
          </button>
          <button type="button" className="link ws-lib-del"
                  aria-label={`移除 ${it.name} 第 ${it.version} 版`}
                  title="从我的库里移除这一版（不影响已安装的家族文件）"
                  onClick={() => onRemove(it)}>
            移除
          </button>
        </div>
      ))}
    </div>
  )
}
