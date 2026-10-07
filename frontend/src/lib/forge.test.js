import { describe, expect, it } from 'vitest'
import {
  EMPTY_INPUT_HINT,
  FAIL_PREFIX,
  MIN_MANUAL_PROMPT,
  NO_VERSION_HINT,
  PROMPT_TOO_SHORT_HINT,
  draftReadyFlash,
  elapsedLabel,
  failNote,
  hasAnyInput,
  installFlash,
  phaseLabel,
  promptSaveBlocked,
  reviseBlockedReason,
  submitFlash,
} from './forge.js'

describe('failNote —— 失败版原因提取', () => {
  it('带前缀的feedback 剥掉前缀', () => {
    expect(failNote({ feedback: `${FAIL_PREFIX}视觉模型连续超时` })).toBe('视觉模型连续超时')
  })

  it('正常版返回空串（不是 undefined —— 组件里直接当条件用）', () => {
    expect(failNote({ feedback: '这一版挺好的' })).toBe('')
    expect(failNote({ feedback: '' })).toBe('')
  })

  it('★ 负向：字段缺失 / 类型不对一律空串，绝不抛', () => {
    for (const bad of [null, undefined, {}, { feedback: null }, { feedback: 123 },
      { feedback: ['x'] }, { feedback: {} }]) {
      expect(() => failNote(bad)).not.toThrow()
      expect(failNote(bad)).toBe('')
    }
  })

  it('★ 前缀必须在前缀位置：中间出现不算失败版', () => {
    expect(failNote({ feedback: `前面有字${FAIL_PREFIX}后面` })).toBe('')
  })

  it('只有前缀、后面没内容时返回空串（不返回 undefined）', () => {
    const v = failNote({ feedback: FAIL_PREFIX })
    expect(v).toBe('')
  })
})

describe('hasAnyInput —— 三路输入至少给一样', () => {
  it('三样都空 → false', () => {
    expect(hasAnyInput({ images: [], theory: '', stylePrompt: '' })).toBe(false)
  })

  it('★ 只有空白字符等同于没给（trim 必须生效）', () => {
    expect(hasAnyInput({ images: [], theory: '   \n\t ', stylePrompt: '  ' })).toBe(false)
  })

  it('任一样非空即true', () => {
    expect(hasAnyInput({ images: [{ url: 'a' }], theory: '', stylePrompt: '' })).toBe(true)
    expect(hasAnyInput({ images: [], theory: '赛博朋克', stylePrompt: '' })).toBe(true)
    expect(hasAnyInput({ images: [], theory: '', stylePrompt: 'voxel' })).toBe(true)
  })

  it('★ 负向：参数整个缺失时按「什么都没给」处理，不抛', () => {
    expect(() => hasAnyInput({})).not.toThrow()
    expect(hasAnyInput({})).toBe(false)
    expect(hasAnyInput()).toBe(false)
  })

  it('images 为 null / 非数组时按空处理', () => {
    expect(hasAnyInput({ images: null, theory: '', stylePrompt: '' })).toBe(false)
    expect(hasAnyInput({ images: 'notarray', theory: '', stylePrompt: '' })).toBe(false)
  })

  it('提示文案非空且指明三条路', () => {
    expect(EMPTY_INPUT_HINT).toContain('参考图')
    expect(EMPTY_INPUT_HINT).toContain('风格理论')
    expect(EMPTY_INPUT_HINT).toContain('风格提示词')
  })
})

describe('promptSaveBlocked —— 手改提示词校验', () => {
  it('正常：足够长放行', () => {
    expect(promptSaveBlocked('a'.repeat(MIN_MANUAL_PROMPT))).toBeNull()
    expect(promptSaveBlocked('这是一段足够长的手改提示词内容，改了顶部的排版')).toBeNull()
  })

  it('★ 空串放行 —— 它是「恢复自动渲染」的信号，不是错误', () => {
    expect(promptSaveBlocked('')).toBeNull()
    expect(promptSaveBlocked('   ')).toBeNull()
    expect(promptSaveBlocked(null)).toBeNull()
    expect(promptSaveBlocked(undefined)).toBeNull()
  })

  it('★ 边界：正好 20 字放行，19 字拦下', () => {
    expect(promptSaveBlocked('a'.repeat(20))).toBeNull()
    expect(promptSaveBlocked('a'.repeat(19))).toBe(PROMPT_TOO_SHORT_HINT)
  })

  it('★ 边界按 trim 后的长度算：前后空白不算字数', () => {
    expect(promptSaveBlocked('  ' + 'a'.repeat(20) + '  ')).toBeNull()
    expect(promptSaveBlocked('  ' + 'a'.repeat(19) + '  ')).toBe(PROMPT_TOO_SHORT_HINT)
  })

  it('★ 负向：单个字符 / 纯标点 拦下', () => {
    expect(promptSaveBlocked('x')).toBe(PROMPT_TOO_SHORT_HINT)
    expect(promptSaveBlocked('...')).toBe(PROMPT_TOO_SHORT_HINT)
    expect(promptSaveBlocked(12345)).toBe(PROMPT_TOO_SHORT_HINT)
  })

  it('正在保存时一律放行（忽略重复点击）', () => {
    expect(promptSaveBlocked('x', { saving: true })).toBeNull()
    expect(promptSaveBlocked('x', { saving: false })).toBe(PROMPT_TOO_SHORT_HINT)
  })

  it('阈值与提示文案一致（改一处必须改另一处）', () => {
    expect(PROMPT_TOO_SHORT_HINT).toContain(String(MIN_MANUAL_PROMPT))
  })
})

describe('reviseBlockedReason —— 迭代前置校验', () => {
  it('★ 没有打开的版本 → 拦下并给可操作指引', () => {
    expect(reviseBlockedReason({ current: null, feedback: '改一下' })).toBe(NO_VERSION_HINT)
    expect(reviseBlockedReason({ current: {}, feedback: '改一下' })).toBe(NO_VERSION_HINT)
    expect(NO_VERSION_HINT).toContain('我的库')
  })

  it('有版本 + 有反馈 → 放行', () => {
    expect(reviseBlockedReason({ current: { id: 'f1' }, feedback: '改一下' })).toBeNull()
  })

  it('反馈为空时放行（按钮本来就是灰的，不重复提示）', () => {
    expect(reviseBlockedReason({ current: { id: 'f1' }, feedback: '' })).toBeNull()
    expect(reviseBlockedReason({ current: { id: 'f1' }, feedback: '   ' })).toBeNull()
  })

  it('★ 负向：参数全缺失也不抛', () => {
    expect(() => reviseBlockedReason({})).not.toThrow()
    expect(reviseBlockedReason({})).toBe(NO_VERSION_HINT)
  })

  it('★ 版本判定看 id：id 为空串等同没有', () => {
    expect(reviseBlockedReason({ current: { id: '' }, feedback: 'x' })).toBe(NO_VERSION_HINT)
  })
})

describe('installFlash —— 安装提示（QC 警告必须说出来）', () => {
  it('无警告：给出简洁的成功文案', () => {
    expect(installFlash({ family_id: 'riso' })).toBe('已安装「riso」，可以在左侧家族列表里用了')
    expect(installFlash({ family_id: 'riso', qc_warnings: [], warnings: [] }))
      .toBe('已安装「riso」，可以在左侧家族列表里用了')
  })

  it('只有 qc_warnings：也要报（★ 此前算完就丢，是真实缺陷）', () => {
    const msg = installFlash({ family_id: 'riso', qc_warnings: ['缺少禁止项'] })
    expect(msg).toContain('需要注意')
    expect(msg).toContain('缺少禁止项')
  })

  it('两类警告合并计数并一起列出', () => {
    const msg = installFlash({
      family_id: 'riso',
      qc_warnings: ['A 问题'],
      warnings: ['B 问题'],
    })
    expect(msg).toContain('2 处')
    expect(msg).toContain('A 问题；B 问题')
  })

  it('★ 空串 / null 警告被过滤掉，不计入条数', () => {
    const msg = installFlash({
      family_id: 'riso',
      qc_warnings: ['', null, '真问题'],
      warnings: [undefined],
    })
    expect(msg).toContain('1 处')
    expect(msg).toContain('真问题')
  })

  it('★ 只有空警告时退化成成功文案（不能说「有 0 处需要注意」）', () => {
    const msg = installFlash({ family_id: 'riso', qc_warnings: ['', null], warnings: [] })
    expect(msg).toBe('已安装「riso」，可以在左侧家族列表里用了')
  })

  it('★ 负向：字段缺失不抛', () => {
    for (const bad of [null, undefined, {}]) {
      expect(() => installFlash(bad)).not.toThrow()
    }
    expect(installFlash({})).toContain('可以在左侧家族列表里用了')
  })
})

describe('phaseLabel / elapsedLabel —— 阶段与耗时展示', () => {
  it('有phase 就显示它', () => {
    expect(phaseLabel({ phase: '视觉模型识别中' })).toBe('视觉模型识别中')
  })

  it('★ 空/ 缺失回落「进行中」（不能显示空白）', () => {
    expect(phaseLabel({})).toBe('进行中')
    expect(phaseLabel({ phase: '' })).toBe('进行中')
    expect(phaseLabel(null)).toBe('进行中')
    expect(phaseLabel(undefined)).toBe('进行中')
  })

  it('秒数四舍五入', () => {
    expect(elapsedLabel(12.4)).toBe(12)
    expect(elapsedLabel(12.6)).toBe(13)
  })

  it('★ 非法秒数归 0，且不产生 NaN', () => {
    for (const bad of [null, undefined, NaN, 'abc', {}]) {
      expect(elapsedLabel(bad)).toBe(0)
    }
  })

  it('负数归0（不让 UI 出现「已-3s」）', () => {
    expect(elapsedLabel(-5)).toBe(0)
  })
})

describe('draftReadyFlash —— 提炼完成提示', () => {
  it('正常：带名字与版本号', () => {
    expect(draftReadyFlash({ name: '孔版', version: 2 })).toBe('「孔版」第 2 版已就绪')
  })

  it('★ version 为 null → 返回 null（不发「第 null 版」）', () => {
    expect(draftReadyFlash({ name: 'x', version: null })).toBeNull()
    expect(draftReadyFlash({ name: 'x' })).toBeNull()
    expect(draftReadyFlash(null)).toBeNull()
  })

  it('★ version 为 0 也要发（0 是合法版本号，不能被|| 吞掉）', () => {
    expect(draftReadyFlash({ name: 'x', version: 0 })).toBe('「x」第 0 版已就绪')
  })
})

describe('submitFlash —— 提交后的语气随并行数变化', () => {
  it('单任务：告诉用户可以离开', () => {
    expect(submitFlash(1)).toContain('可以继续提交下一组或去别的页面')
  })

  it('多任务：说明会逐个提醒', () => {
    expect(submitFlash(3)).toContain('3 个任务并行')
    expect(submitFlash(3)).toContain('逐个提醒')
  })

  it('边界：0 个也算单任务语气', () => {
    expect(submitFlash(0)).toBe(submitFlash(1))
  })
})
