/**
 * 电子画册 · 翻页引擎（纯 CSS 3D，零依赖）
 *
 * ★★★ 为什么不用 PageFlip 之类的库：
 *   参考项目 HaichaoLihc/create-photo-flipbook-ui 用的是 PageFlip（vendor 内置）。
 *   我选择自己实现，理由有三：
 *   ① 答辩现场网络不可控 —— **画册必须能离线打开**，
 *      而库文件是外部依赖，缺了就白屏（现场翻不出来就是灾难）。
 *   ② 需要按本项目的「拾景纸刊」视觉语言定制（撕纸边、旧纸、手写批注），
 *      库的默认样式要覆盖的比自己写还多。
 *   ③ 核心只是 rotateY + preserve-3d，代码量小、可控。
 *
 * ★★★ 技术可行性已实测（2026-10-09，无头浏览器）：
 *   翻页后 computed transform = matrix3d(-1,0,0,0, 0,1,0,0, 0,0,-1,0, 0,0,0,1)
 *   中途帧可见**页面绕书脊透视旋转、纸张背面露出、书脊阴影仍在**
 *   —— 是真实翻书，不是平移+淡入（后者是最常见的失败）。
 *
 * ★ 参考实现里学到的三条（已读其源码确认，本项目按需要裁剪）：
 *   ① **双页对开**：左右两页同时可见，比单页更像一本摊开的书
 *   ② **书口渐变阴影**：`.--left::before` / `.--right::before`
 *      各向书口方向渐变 —— 这一条是"两页看起来是连着的"的关键
 *   ③ **folio 页码分侧**：verso（偶数页）在左、recto（奇数页）在右
 */

export const DUR = 820// 翻页时长
export const EASE = 'cubic-bezier(0.42, 0, 0.2, 1)'      // 纸张起步稍缓、落下带感

/** 读环境里的「降低动态」偏好 */
export function prefersReducedMotion() {
  return typeof matchMedia === 'function'
    && matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * 页面几何：竖版 3:5（拾景纸刊原生画幅，与海报一致）
 * → 双页对开的书本比例 = 2 * 3 / 5
 */
export const PAGE = {
  w: 420,
  h: 700,
  get bookW() { return this.w * 2 },
  get bookH() { return this.h },
  get ratio() { return this.w / this.h },
}