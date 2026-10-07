import { Component } from 'react'

/**
 * 全应用错误边界 —— 任一组件抛错时的最后一道兜底。
 *
 * ★ 为什么不裸渲染 <App/>：
 *   App 持有 38 个 useState，其中包含用户刚上传的原图、调好的参数、
 *   以及整个修复历史栈。一处渲染异常会让整页白屏，
 *   而用户此时既看不到任何解释，也没法把图抢救出来 ——
 *   白屏对「做一张旅行照片」的现场演示是致命的。
 *   有了边界，最坏情况是一句人话 + 一个刷新按钮，而不是空白页。
 *
 * 为什么用 class 而不是 function：
 *   React 19 仍然支持 class 组件，而getDerivedStateFromError
 *   与 componentDidCatch 这一对组合目前没有等价的 hooks 写法。
 *   react-error-boundary 这类库能省这几行，但为一个错误边界
 *   引入一个新依赖不划算（也让构建产物多一份）。
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
    // 用户点「重试」时用：同一个错误反复渲染必然再炸，
    // 所以清掉 state 让子树带着新的初始状态重新挂载。
    this.handleReset = this.handleReset.bind(this)
  }

  static getDerivedStateFromError(error) {
    // 渲染阶段已经出错了，此刻只能记下错误并切到兜底 UI
    return { error }
  }

  componentDidCatch(error, info) {
    // 真正能定位问题的地方是控制台：页面上只给用户看人话，
    // 堆栈留给开发者（info.componentStack 是 React 记的组件路径）
    console.error('[ErrorBoundary] 界面渲染出错，已切换到兜底页', error, info?.componentStack)
  }

  handleReset() {
    this.setState({ error: null })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    const detail = [
      error?.message,
      error?.stack ? `\n${error.stack}` : '',
    ].join('').trim()

    return (
      <div className="eb-page">
        <div className="eb-card" role="alert">
          <p className="eb-glyph" aria-hidden="true">⚠</p>
          <h1 className="eb-title">页面出了点问题</h1>
          <p className="eb-lead">
            界面渲染时遇到了一个错误，这一页已经停住了。
            你刚才选的风格和参数不会写到服务器去，刷新后需要重新上传原图。
          </p>

          <div className="eb-actions">
            <button type="button" className="btn solid" onClick={this.handleReset}>
              重试一次
            </button>
            <button type="button" className="btn ghost"
                    onClick={() => window.location.reload()}>
              刷新页面
            </button>
          </div>

          {detail && (
            <details className="eb-detail">
              <summary>错误详情（排查用）</summary>
              {/* ★ 只用纯文本渲染：错误 message 里可能带用户上传的文件名等内容，
                  用 innerHTML 展开就等于开了一个 XSS 口子。
                  <pre> 天然转义，不需要任何消毒。 */}
              <pre className="eb-pre">{detail}</pre>
            </details>
          )}
        </div>
      </div>
    )
  }
}