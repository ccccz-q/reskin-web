import { describe, expect, it } from 'vitest'
import { MAX_UPLOAD_BYTES, orientationOf, validateUploadFile } from './upload.js'

/** 只提供校验需要的字段，避免在用例里堆无关东西 */
const file = (name, { type = '', size = 1024 } = {}) => ({ name, type, size })

describe('validateUploadFile —— 上传预校验', () => {
  it('合规图片 → null（放行）', () => {
    expect(validateUploadFile(file('a.jpg', { type: 'image/jpeg' }))).toBeNull()
    expect(validateUploadFile(file('a.png', { type: 'image/png' }))).toBeNull()
    expect(validateUploadFile(file('a.webp', { type: 'image/webp' }))).toBeNull()
  })

  it('★ 空文件 → null（放行，由上传接口去报错）', () => {
    expect(validateUploadFile(null)).toBeNull()
    expect(validateUploadFile(undefined)).toBeNull()
    expect(validateUploadFile(0)).toBeNull()
  })

  it('★ 只给扩展名不给 MIME 也能过（截图工具 / Word 粘贴的常见形态）', () => {
    expect(validateUploadFile(file('照片.jpg', { type: '' }))).toBeNull()
    expect(validateUploadFile(file('截图.PNG', { type: '' }))).toBeNull()
    expect(validateUploadFile(file('x.weBp', { type: '' }))).toBeNull()
  })

  it('★ 只给 MIME 不给扩展名也能过（剪贴板 blob 的形态）', () => {
    expect(validateUploadFile(file('', { type: 'image/jpeg' }))).toBeNull()
    expect(validateUploadFile(file(undefined, { type: 'image/png' }))).toBeNull()
  })

  it('扩展名大小写不敏感（JPG / Png 都认）', () => {
    expect(validateUploadFile(file('A.JPG'))).toBeNull()
    expect(validateUploadFile(file('B.JPEG'))).toBeNull()
    expect(validateUploadFile(file('C.JpEg'))).toBeNull()
  })

  it('拒绝非图片类型，且文案带上文件名', () => {
    expect(validateUploadFile(file('简历.pdf', { type: 'application/pdf' })))
      .toBe('「简历.pdf」不是图片 —— 请上传 JPG / PNG / WebP 格式的照片')
  })

  it('★ 类型错优先于大小错 —— 不能把「选错文件」报成「文件太大」', () => {
    const big = file('x.gif', { type: 'image/gif', size: 999 * 1024 * 1024 })
    expect(validateUploadFile(big)).toContain('不是图片')
    expect(validateUploadFile(big)).not.toContain('超过 20MB')
  })

  it('★ 没有扩展名的文件：只看 MIME，MIME 也不对就拒', () => {
    expect(validateUploadFile(file('', { type: 'text/plain' })))
      .toBe('「这个文件」不是图片 —— 请上传 JPG / PNG / WebP 格式的照片')
  })

  it('拒绝 gif（后端白名单只有 JPEG/PNG/WEBP）', () => {
    expect(validateUploadFile(file('a.gif', { type: 'image/gif' }))).toContain('不是图片')
    // 扩展名 gif + 可接受的 MIME → 放行（信任 MIME，与后端两条路都认一致）
    expect(validateUploadFile(file('a.gif', { type: 'image/png' }))).toBeNull()
  })

  it('svg / bmp / tiff 一律拒绝（矢量与位图格式后端不收）', () => {
    for (const ext of ['svg', 'bmp', 'tiff', 'tif', 'heic', 'avif']) {
      expect(validateUploadFile(file(`a.${ext}`, { type: `image/${ext}` }))).toContain('不是图片')
    }
  })

  describe('大小上限', () => {
    it(`正好等于 ${MAX_UPLOAD_BYTES} → 放行（上限是「超过」才拦）`, () => {
      expect(validateUploadFile(file('a.jpg', { size: MAX_UPLOAD_BYTES }))).toBeNull()
    })

    it('超 1 字节 → 拦', () => {
      expect(validateUploadFile(file('a.jpg', { size: MAX_UPLOAD_BYTES + 1 }))).toContain('超过 20MB 上限')
    })

    it('★ 文案用 MB 而不是字节数，并保留一位小数', () => {
      expect(validateUploadFile(file('大.jpg', { size: 25 * 1024 * 1024 })))
        .toBe('「大.jpg」有 25.0MB，超过 20MB 上限 —— 请先压缩或换一张')
      expect(validateUploadFile(file('大.jpg', { size: 20.5 * 1024 * 1024 })))
        .toContain('20.5MB')
    })

    it('size 缺失 / 负数 / 非数字 → 按「不算超限」处理，放行给上传接口报错', () => {
      // undefined > 上限 是 false，所以走到放行。这是有意的：
      // 前端不替后端猜大小，报错由服务端给。
      expect(validateUploadFile(file('a.jpg', { size: undefined }))).toBeNull()
      expect(validateUploadFile(file('a.jpg', { size: -1 }))).toBeNull()
      expect(validateUploadFile(file('a.jpg', { size: NaN }))).toBeNull()
    })

    it('size 为 0（空文件）→ 放行', () => {
      expect(validateUploadFile(file('a.jpg', { size: 0 }))).toBeNull()
    })
  })

  it('非法输入：type 不是字符串时靠正则的 test 转换，不抛', () => {
    expect(() => validateUploadFile({ name: 'a.jpg', type: null, size: 1 })).not.toThrow()
    expect(() => validateUploadFile({ name: 'a.jpg', type: 123, size: 1 })).not.toThrow()
    expect(() => validateUploadFile({ name: null, type: null, size: 1 })).not.toThrow()
  })

  it('name 为空串 → 文案里用「这个文件」兜底，不出现空的「」', () => {
    const msg = validateUploadFile({ name: '', type: 'text/plain', size: 1 })
    expect(msg).toContain('「这个文件」')
    expect(msg).not.toContain('「」')
  })

  it('同一个文件重复校验结果稳定（纯函数，无隐藏状态）', () => {
    const f = file('a.jpg', { size: MAX_UPLOAD_BYTES + 1 })
    expect(validateUploadFile(f)).toBe(validateUploadFile(f))
    const good = file('a.jpg')
    expect(validateUploadFile(good)).toBeNull()
    expect(validateUploadFile(good)).toBeNull()
  })
})

describe('orientationOf —— 原图朝向', () => {
  it('高>宽 → 竖构图', () => {
    expect(orientationOf({ width: 800, height: 1200 })).toBe('竖构图')
  })

  it('宽>高 → 横构图', () => {
    expect(orientationOf({ width: 1200, height: 800 })).toBe('横构图')
  })

  it('正方 → 方构图', () => {
    expect(orientationOf({ width: 1000, height: 1000 })).toBe('方构图')
  })

  it('没有原图 → null（不显示标注）', () => {
    expect(orientationOf(null)).toBeNull()
    expect(orientationOf(undefined)).toBeNull()
  })

  it('尺寸缺失时按 0 比，不抛且归到方构图', () => {
    expect(orientationOf({})).toBe('方构图')
  })
})