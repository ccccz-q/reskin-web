import { describe, expect, it } from 'vitest'
import {
  downloadModeFor,
  downloadName,
  extFromMime,
  zipDownloadName,
} from './download.js'

describe('extFromMime —— 从 MIME 推扩展名', () => {
  it('常用类型直出', () => {
    expect(extFromMime('image/png')).toBe('png')
    expect(extFromMime('image/webp')).toBe('webp')
    expect(extFromMime('image/jpg')).toBe('jpg')
  })

  it('★ jpeg → jpg（用户不认 .jpeg 这个后缀）', () => {
    expect(extFromMime('image/jpeg')).toBe('jpg')
  })

  it('★ 拿不到子类型 → 回落 jpg，不产出 .undefined', () => {
    expect(extFromMime('')).toBe('jpg')
    expect(extFromMime('image/')).toBe('jpg')
    expect(extFromMime(null)).toBe('jpg')
    expect(extFromMime(undefined)).toBe('jpg')
  })

  it('只取第一段：application/json → json', () => {
    expect(extFromMime('application/json')).toBe('json')
  })
})

describe('downloadName —— 成品图文件名', () => {
  it('形如 travelnote-YYYY-MM-DD.jpg', () => {
    expect(downloadName('image/jpeg')).toMatch(/^travelnote-\d{4}-\d{2}-\d{2}\.jpg$/)
  })

  it('扩展名跟着 MIME 走', () => {
    expect(downloadName('image/png').endsWith('.png')).toBe(true)
    expect(downloadName('image/webp').endsWith('.webp')).toBe(true)
  })

  it('★ 同一天第二次下载加序号，避免浏览器当同一个文件覆盖', () => {
    expect(downloadName('image/png', 2)).toMatch(/-2\.png$/)
    expect(downloadName('image/png', 1)).toMatch(/-1\.png$/)
  })

  it('非法输入：MIME 为空仍然给得出合法文件名', () => {
    expect(downloadName(undefined)).toMatch(/\.jpg$/)
    expect(downloadName(null)).toMatch(/\.jpg$/)
  })
})

describe('zipDownloadName —— 打包文件名', () => {
  it('形如 作品-YYYY-MM-DD.zip', () => {
    expect(zipDownloadName()).toMatch(/^作品-\d{4}-\d{2}-\d{2}\.zip$/)
  })

  it('★ 同一天先单张后打包，两个文件名不同，不会互相覆盖', () => {
    expect(zipDownloadName()).not.toBe(downloadName('image/jpeg'))
  })
})

describe('downloadModeFor —— 单张直下还是打包', () => {
  it('0 张 → none（不该有下载入口）', () => {
    expect(downloadModeFor(0)).toBe('none')
  })

  it('1 张 → single（省掉后端一次 zip 合成）', () => {
    expect(downloadModeFor(1)).toBe('single')
  })

  it('2 张以上 → zip', () => {
    expect(downloadModeFor(2)).toBe('zip')
    expect(downloadModeFor(30)).toBe('zip')
  })

  it('非法输入：负数落到 zip 分支（调用方只可能是 0/1/N，不会出现）', () => {
    // count 只来自 Set.size，永远 >= 0。如实锁住现状：
    // 负数不是 0 也不是 1，所以既不是 none 也不是 single。
    expect(downloadModeFor(-1)).toBe('zip')
  })
})