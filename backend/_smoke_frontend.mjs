/* 前端冒烟测试 —— 用真实浏览器验证"页面能渲染"
 *
 * 用法：
 *   node _smoke_frontend.mjs [APP_URL]        默认 http://127.0.0.1:5199
 * 前置：vite dev server + 后端都要起着
 *
 * ★ 为什么必须是浏览器，不能是 vitest
 * -------------------------------------
 * 2026-10-07 出过一次真实故障：登录后整页崩进 ErrorBoundary，
 * 报 `ReferenceError: Cannot access 'flash' before initialization`
 *（App.jsx 里useGallery 排在 const flash 的定义之前 —— const 的 TDZ）。
 *
 * 三道防线**全部没拦住**：
 *   - `vite build`：这是运行时错误，构建期看不见；
 *   - `oxlint`：0 error；
 *   - **179 个单元测试**：它们测的是 `src/lib/` 里的纯函数，**不渲染 App**。
 *
 * 我也试过写"静态检查：定义在使用之后"的脚本，结果**误报 250 处**
 * （JSX 属性是延迟求值，绝大多数不是 TDZ）。一个误报率 99% 的检查器
 * 比没有更糟——它只会让人养成忽略警告的习惯。
 *
 * 所以唯一可靠的防线是**真的把页面渲染一遍**，看有没有抛异常。
 * 这也正是这个脚本的来历：它第一次跑就抓到了上面那个故障。
 *
 *覆盖范围：首屏渲染 + 逐个点开主要入口（工坊/设置/助手/作品/下载），
 * 任何一个入口触发 pageerror 就失败。
 */
// ── 可移植性（2026-10-08 为接CI 而改）──────────────────────
//   原版写死了两样本机特有的东西，CI 上 100% 跑不起来：
//     ① `channel: 'msedge'` —— 只装了 msedge 的机器才有的浏览器通道，
//        GitHub runner 上没有 Edge，只有 Playwright 自带的 chromium；
//     ② import 用 file:/// 绝对路径 —— 换机器 / 换用户就没了。
//   现在：优先用普通 chromium（CI 装 playwright chromium 即可）；
//   本机没装 chromium 时回退到 msedge（保持我本机的既有跑法不变）。
//   不做「自动探测浏览器是否存在」那层间接抽象 —— 试一次失败再回落就够，
//   失败原因会直接打在日志里，比抽象层更好排查。
import { mkdirSync } from 'node:fs'

// ★ playwright 的导入路径不能硬编码：CI 上装在项目的 node_modules 里，
//   本机装在托管 workspace 里。两种都试，谁成功用谁。
//   ⚠️ 第三条是**用环境变量构造**的，不是写死路径：
//   写死 `file:///C:/Users/lenovo/...` 在别人机器与 CI 上必然解析失败，
//   而失败会被 catch 静默吞掉、最后抛一句「找不到 playwright」——
//   看不出真实原因。NODE_PATH 由 CI 显式传入（见 test.yml）。
async function _loadChromium() {
  const fromEnv = process.env.SMOKE_PLAYWRIGHT_PATH
  const candidates = [
    'playwright',                       // CI：装在 frontend/node_modules
    fromEnv,                            // CI 显式指定的绝对路径（可为 undefined）
    'file:///C:/Users/lenovo/.workbuddy/binaries/node/workspace/node_modules/playwright/index.mjs',
  ].filter(Boolean)
  const tried = []
  for (const spec of candidates) {
    try {
      const m = await import(spec)
      if (m?.chromium) {
        if (tried.length) console.log(`[smoke] playwright 来源：${spec}（先前试过 ${tried.length} 个均失败）`)
        return m.chromium
      }
    } catch (e) {
      tried.push(`${spec}（${String(e).split('\n')[0].slice(0, 40)}）`)
    }
  }
  throw new Error(
    '找不到 playwright。试过：\n  ' + tried.join('\n  ') +
    '\n解决办法：`npm i -D playwright && npx playwright install chromium`，' +
    '或设SMOKE_PLAYWRIGHT_PATH 指向已安装的 playwright。'
  )
}

const APP = process.argv[2] || 'http://127.0.0.1:5199'
// ★ 截图目录由环境变量给：CI 失败时要能从 artifact 里看到页面长什么样，
//   本机默认不写盘（不想每次跑都留一堆 png 在仓库里）。
const SHOTS = process.env.SMOKE_SHOT_DIR || ''

const chromium = await _loadChromium()
// 先试标准 chromium；失败（多半是没装）再退回本机的 msedge。
let browser
try {
  browser = await chromium.launch({ headless: true })
} catch {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
}
if (SHOTS) mkdirSync(SHOTS, { recursive: true })
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })

const errors = []
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + (e?.stack || String(e))))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 300))
})

let pass = 0
let fail = 0
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  OK   ${label} ${extra}`) }
  else { fail++; console.log(`  FAIL ${label} ${extra}`) }
}

const crashed = () => page.evaluate(() =>
  document.body.innerText.includes('页面出了点问题'))

console.log(`[smoke] ${APP}`)
await page.goto(APP, { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)

console.log('\n=== 1. 首屏===')
check('首屏没有崩', !(await crashed()))
check('渲染出了主容器', await page.evaluate(() =>
  !!document.querySelector('#root')?.children.length))
check('家族列表已加载', await page.evaluate(() =>
  document.body.innerText.includes('风格') || document.body.innerText.includes('家族')))

console.log('\n=== 2. 逐个点开主要入口 ===')
for (const label of ['工坊', '设置', '小助手', '作品', '下载']) {
  try {
    const el = page.locator(`text=${label}`).first()
    if (!(await el.count())) { console.log(`  --   「${label}」入口不存在（跳过）`); continue }
    await el.click({ timeout: 4000 })
    await page.waitForTimeout(1200)
    const c = await crashed()
    check(`点「${label}」不崩`, !c, c ? '→ 崩了' : '')
    if (c) break
  } catch (e) {
    console.log(`  --   「${label}」点不动（${String(e).slice(0, 50)}）`)
  }
}

console.log('\n=== 3. 真实 API 往返 ===')
const health = await page.evaluate(async () => {
  try {
    const r = await fetch('/api/health')
    return { ok: r.ok, status: r.status }
  } catch (e) { return { ok: false, err: String(e) } }
})
check('前端能拿到 /api/health', health.ok, JSON.stringify(health))

console.log('\n=== 4. 页面错误 ===')
if (!errors.length) console.log('  （无 pageerror / console.error）')
else errors.slice(0, 6).forEach((e) => console.log('  ' + e.split('\n')[0]))

// ★ 失败即留证据（要在 close 之前截，关掉页面就截不到了）。
//   只有 SHOTS 非空时才写盘—— 本机跑不想每次留一堆 png在仓库里，
//   而 CI 上正好靠这个把现场带出来。
if (SHOTS && (fail || errors.length)) {
  try {
    await page.screenshot({ path: `${SHOTS}/fail.png`, fullPage: true })
    // 首屏单独再截一张：失败点常常在首屏，全页截图里它被挤得很小
    console.log(`  截图已保存：${SHOTS}/fail.png`)
  } catch (e) {
    console.log(`  截图失败（不影响结论）：${String(e).slice(0, 80)}`)
  }
}

await browser.close()
console.log(`\n结果：${pass} 通过 / ${fail} 失败${errors.length ? `，${errors.length} 条页面错误` : ''}`)
process.exit(fail || errors.length ? 1 : 0)
