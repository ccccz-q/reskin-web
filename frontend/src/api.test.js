// api.js 的纯逻辑测试：URL 构造 + 错误解析。
//
// 它读 import.meta.env（vite 注入），并在 authHeaders 里用 localStorage /
// crypto —— 这两个在 Node 环境下不存在，所以先装最小桩。
// 只测纯函数（apiPath / imgSrc / thumbSrc / throwResponseError），
// 不发任何真实请求。
import { beforeAll, describe, expect, it } from 'vitest'

// ── 最小浏览器环境桩 ────────────────────────────
beforeAll(() => {
  if (!globalThis.localStorage) {
    const store = new Map()
    globalThis.localStorage = {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    }
  }
  if (!globalThis.crypto) globalThis.crypto = { randomUUID: () => 'test-uuid-1234' }
})

const { apiPath, imgSrc, thumbSrc, throwResponseError } = await import('./api.js')

/** 造一个最小 Response 桩：只需ok / status / json()。 */
const resp = ({ ok = false, status = 400, body = null, jsonThrows = false }) => ({
  ok,
  status,
  json: async () => {
    if (jsonThrows) throw new SyntaxError('Unexpected token <')
    return body
  },
})

describe('apiPath —— 路径拼接', () => {
  it('保留前导斜杠', () => {
    expect(apiPath('/api/health')).toBe('/api/health')
  })

  it('★ 不带斜杠也补上（调用方写错不会 404）', () => {
    expect(apiPath('api/health')).toBe('/api/health')
  })

  it('只补一个斜杠，不会变成 //', () => {
    expect(apiPath('//api/health')).toBe('//api/health')    // 已是斜杠开头，不重复加
  })

  it('空串 → /', () => {
    expect(apiPath('')).toBe('/')
  })
})

describe('imgSrc —— 图片地址', () => {
  it('空url → 空串（渲染成无 src 的 img，而不是请求当前页）', () => {
    expect(imgSrc('')).toBe('')
    expect(imgSrc(null)).toBe('')
    expect(imgSrc(undefined)).toBe('')
  })

  it('★ 绝对地址原样返回（不拼 BASE）', () => {
    expect(imgSrc('https://cdn.example.com/a.jpg')).toBe('https://cdn.example.com/a.jpg')
    expect(imgSrc('http://cdn.example.com/a.jpg')).toBe('http://cdn.example.com/a.jpg')
    expect(imgSrc('//cdn.example.com/a.jpg')).toBe('//cdn.example.com/a.jpg')
  })

  it('以 / 开头的走 BASE', () => {
    expect(imgSrc('/images/a.jpg')).toBe('/images/a.jpg')
  })

  it('★ 只给文件名 → 补 /images/ 前缀（兼容老调用方）', () => {
    expect(imgSrc('a.jpg')).toBe('/images/a.jpg')
    expect(imgSrc('sub/dir/a.jpg')).toBe('/images/sub/dir/a.jpg')
  })
})

describe('thumbSrc —— 缩略图地址', () => {
  it('拼上 u 与 w 两个query 参数', () => {
    expect(thumbSrc('a.jpg', 360)).toBe('/api/image/thumb?u=a.jpg&w=360')
  })

  it('★ 默认宽度 320', () => {
    expect(thumbSrc('a.jpg')).toBe('/api/image/thumb?u=a.jpg&w=320')
  })

  it('★ url 里的特殊字符要编码（否则 & 会截断参数）', () => {
    expect(thumbSrc('a b&c=d.jpg')).toBe('/api/image/thumb?u=a%20b%26c%3Dd.jpg&w=320')
    expect(thumbSrc('中文名.jpg')).toBe(`/api/image/thumb?u=${encodeURIComponent('中文名.jpg')}&w=320`)
  })

  it('空 url → 空串', () => {
    expect(thumbSrc('')).toBe('')
    expect(thumbSrc(null)).toBe('')
  })
})

describe('throwResponseError —— 统一错误解析', () => {
  it('★ detail 是字符串 → 直接用', async () => {
    await expect(throwResponseError(resp({ body: { detail: '文件太大了' } }), '兜底'))
      .rejects.toThrow('文件太大了')
  })

  it('★ detail 是 {message} → 取message', async () => {
    await expect(throwResponseError(resp({ body: { detail: { message: '缺必填参数' } } }), '兜底'))
      .rejects.toThrow('缺必填参数')
  })

  it('body.error 也认（与 detail 同级）', async () => {
    await expect(throwResponseError(resp({ body: { error: '配额用完' } }), '兜底'))
      .rejects.toThrow('配额用完')
  })

  it('detail 是对象但没有 message → JSON.stringify 整个对象', async () => {
    await expect(throwResponseError(resp({ body: { detail: { field: 'size' } } }), '兜底'))
      .rejects.toThrow('{"field":"size"}')
  })

  it('★ 非JSON 响应（网关错误页）→ 保留兜底文案', async () => {
    await expect(throwResponseError(resp({ jsonThrows: true }), '修复没有成功，请稍后再试'))
      .rejects.toThrow('修复没有成功，请稍后再试')
  })

  it('★ 空 body → 保留兜底文案', async () => {
    await expect(throwResponseError(resp({ body: {} }), '兜底文案')).rejects.toThrow('兜底文案')
    await expect(throwResponseError(resp({ body: null }), '兜底文案')).rejects.toThrow('兜底文案')
  })

  it('空串 detail 不覆盖兜底（falsy）', async () => {
    await expect(throwResponseError(resp({ body: { detail: '' } }), '兜底文案')).rejects.toThrow('兜底文案')
  })

  it('detail 为 null → 走 b.error，都没有就回落兜底', async () => {
    await expect(throwResponseError(resp({ body: { detail: null } }), '兜底文案')).rejects.toThrow('兜底文案')
  })

  it('★ detail 为 0 / false → 因是falsy 被 || 跳过，最终回落兜底（不会被 stringify）', async () => {
    // picked = d?.message || d || b?.error：0 和 false 都被 || 吞掉 → undefined
    await expect(throwResponseError(resp({ body: { detail: 0 } }), '兜底文案')).rejects.toThrow('兜底文案')
    await expect(throwResponseError(resp({ body: { detail: false } }), '兜底文案')).rejects.toThrow('兜底文案')
  })

  it('★ 抛出的一定是 Error 实例（调用方拿 e.message 用得起来）', async () => {
    const err = await throwResponseError(resp({ body: { detail: 'x' } }), '兜底').catch((e) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err.message).toBe('x')
  })

  it('非法输入：body 是字符串而不是对象 → 保留兜底文案', async () => {
    await expect(throwResponseError(resp({ body: 'Internal Server Error' }), '兜底文案'))
      .rejects.toThrow('兜底文案')
  })
})

describe('会话身份', () => {
  it('getSessionId 每次都返回合法 sid（不抛）', async () => {
    const { getSessionId } = await import('./api.js')
    expect(() => getSessionId()).not.toThrow()
    expect(getSessionId()).toMatch(/^[0-9a-f][0-9a-f-]{7,63}$/)
  })

  it('★ 存过的 sid 原样复用（换页不换会话）', async () => {
    const { getSessionId, setAuthToken } = await import('./api.js')
    const first = getSessionId()
    setAuthToken('')
    expect(getSessionId()).toBe(first)
  })

  it('★ 非法的旧 sid 会被换掉（不能把脏值发给后端）', async () => {
    const { getSessionId } = await import('./api.js')
    localStorage.setItem('travelogue.session_id', '坏值!!')
    const sid = getSessionId()
    expect(sid).not.toBe('坏值!!')
    expect(sid).toMatch(/^[0-9a-f][0-9a-f-]{7,63}$/)
  })

  it('setAuthToken 写入 / 清空', async () => {
    const { getAuthToken, setAuthToken } = await import('./api.js')
    setAuthToken('tok-123')
    expect(getAuthToken()).toBe('tok-123')
    setAuthToken('')
    expect(getAuthToken()).toBe('')
  })

  it('★ localStorage 抛异常时照常用（隐私模式不白屏）', async () => {
    const { getSessionId, getAuthToken } = await import('./api.js')
    const real = globalThis.localStorage
    globalThis.localStorage = {
      getItem: () => { throw new Error('SecurityError') },
      setItem: () => { throw new Error('SecurityError') },
      removeItem: () => {},
    }
    try {
      expect(() => getSessionId()).not.toThrow()
      expect(() => getAuthToken()).not.toThrow()
      expect(getAuthToken()).toBe('')
    } finally {
      globalThis.localStorage = real
    }
  })
})