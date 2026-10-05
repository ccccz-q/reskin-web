import { useEffect, useMemo, useRef, useState } from 'react'

/**
 * 参数表单 —— 「params 即 UI schema」的前端落点
 *
 * 控件的类型、取值范围、默认值、**以及中文名**全部来自后端
 * /api/families 返回的 schema，前端**不认识任何一个具体的家族**。
 * 这样新增一个家族 = 加一个 YAML，这里零改动。
 *
 * ★ 下面的 LABELS / VALUES 只是**向后兼容兜底**（后端某个字段没给时用）。
 *   真正的真源是后端 templates/_labels.yaml，已经全量覆盖，
 *   所以正常情况下这两张表一行都不会被命中 —— 保留它们是为了
 *   万一标签文件丢了也不至于显示成空白控件。
 */

const LABELS = {
  aspect: '画幅',
  fidelity: '保真强度',
  abstraction: '抽象度',
  palette_source: '色板来源',
  render: '风格化手法',
  style: '风格',
  school: '学校',
  major: '专业',
  direction: '方向',
  silhouette_side: '剪影朝向',
  object_form: '物化形态',
  interaction: '互动方式',
  inner_world: '内在世界',
  giant_element: '巨物元素',
  flat_shapes: '平涂形状',
  small_elements: '小元素',
  substrate: '纸张基底',
  photo_ratio: '照片占比',
  edge: '边缘处理',
  materialize_anchor: '物化锚点',
  crossing_color: '跨越色',
  text_role: '文字用法',
  text_lang: '文字语种',
  text_align: '文字对齐',
  figures: '人物数量',
  resolution_hint: '输出清晰度',
  split_ratio: '分区比例',
  tear_logic: '撕纸逻辑',
  subject: '主体',
}

const VALUES = {
  fidelity: { light: '轻度', medium: '中度', heavy: '重度' },
  abstraction: { low: '低', mid: '中', high: '高' },
  palette_source: { fixed: '固定色板', from_photo: '取自原图' },
  edge: { hard: '硬边', bleed: '晕染外溢' },
}

/** 参数中文名：后端 label 优先，其次兜底表，最后才是参数名 */
function labelOf(param) {
  return param?.label || LABELS[param?.name] || param?.name || ''
}

/** 选项中文名：后端 option_labels 优先，其次兜底表 */
function valueLabel(param, v) {
  return (
    param?.option_labels?.[v] ??
    VALUES[param?.name]?.[v] ??
    v
  )
}

/**
 * 单个参数 = 一个「标签 + 控件」的组合。
 * 标签的关联方式按控件类型分：
 *   - 枚举是一组按钮 → 用 role=group + aria-labelledby（htmlFor 指不到一组按钮）
 *   - 开关自己就是一个 label → 把参数名写进 label 文本里
 *   - 滑杆 / 文本框是单个输入 → 用 htmlFor + id
 * 以前统一写 <label> 却不给 htmlFor，屏幕阅读器读到的是一串没有名字的控件。
 */
function Control({ param, value, onChange, disabled }) {
  const cid = `pf-${param.name}`
  const lid = `${cid}-l`
  const name = labelOf(param)
  const reqMark = param.required
    ? <><span className="pf-req" aria-hidden="true">*</span><span className="sr-only">（必填）</span></>
    : null

  if (param.type === 'enum' && param.options?.length) {
    return (
      <div className="pf-row">
        <span className="pf-label" id={lid}>{name}{reqMark}</span>
        <div className="pf-chips" role="group" aria-labelledby={lid}>
          {param.options.map((opt) => (
            <button
              key={opt}
              type="button"
              className={`pf-chip ${value === opt ? 'on' : ''}`}
              aria-pressed={value === opt}
              disabled={disabled}
              onClick={() => onChange(opt)}
            >
              {valueLabel(param, opt)}
            </button>
          ))}
        </div>
      </div>
    )
  }

  if (param.type === 'bool' || typeof param.default === 'boolean') {
    return (
      <div className="pf-row">
        <label className={`pf-switch ${value ? 'on' : ''}`}>
          <input
            type="checkbox"
            checked={!!value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.checked)}
          />
          <span className="pf-switch-track"><span className="pf-switch-knob" /></span>
          <span className="pf-switch-text">{name}：{value ? '开' : '关'}</span>
        </label>
      </div>
    )
  }

  if (param.range?.length === 2) {
    const [min, max] = param.range
    const num = Number(value ?? min)
    return (
      <div className="pf-row">
        <label className="pf-label" htmlFor={cid}>{name}{reqMark}</label>
        <div className="pf-range">
          <input
            id={cid}
            type="range"
            min={min}
            max={max}
            step={(max - min) / 20 || 1}
            value={num}
            disabled={disabled}
            aria-valuetext={String(num)}
            onChange={(e) => onChange(Number(e.target.value))}
          />
          <span className="pf-range-val" aria-hidden="true">{num}</span>
        </div>
      </div>
    )
  }

  return (
    <div className="pf-row">
      <label className="pf-label" htmlFor={cid}>{name}{reqMark}</label>
      <input
        id={cid}
        className="pf-text"
        type="text"
        value={value ?? ''}
        disabled={disabled}
        placeholder={param.source === 'from_photo' ? '留空则自动从原图提炼' : '输入…'}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}

export default function ParamForm({ family, params, onChange, disabled }) {
  const schema = useMemo(() => family?.params || [], [family])

  // 家族切换时补齐默认值：不留 undefined 给后端
  const timer = useRef(null)
  const [rendering, setRendering] = useState(false)
  useEffect(() => {
    if (!family) return
    const patch = {}
    for (const p of schema) {
      const cur = params[p.name]
      if (cur === undefined || cur === '') {
        if (p.default !== undefined && p.default !== null && p.default !== 'auto') {
          patch[p.name] = p.default
        }
      }
    }
    if (Object.keys(patch).length) onChange({ ...params, ...patch })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [family?.id])

  // 参数一改，服务端会在 220ms 后重渲染提示词 —— 这段时间告诉读屏器「正在变」
  useEffect(() => {
    setRendering(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setRendering(false), 260)
    return () => clearTimeout(timer.current)
  }, [params])

  if (!family) {
    return <p className="pf-empty">先在左边选一个家族</p>
  }

  const required = schema.filter((p) => p.required)
  const optional = schema.filter((p) => !p.required)

  const row = (p) => (
    <Control key={p.name} param={p} value={params[p.name]} disabled={disabled}
             onChange={(v) => onChange({ ...params, [p.name]: v }, p.name)} />
  )

  return (
    <div className="pf" aria-busy={rendering}>
      {required.length > 0 && (
        <div className="pf-group" role="group" aria-labelledby="pf-g-req">
          <div className="pf-group-title" id="pf-g-req">必填 · 缺了就渲染不出来</div>
          {required.map(row)}
        </div>
      )}

      <div className="pf-group" role="group" aria-labelledby="pf-g-opt">
        <div className="pf-group-title" id="pf-g-opt">可调参数</div>
        {optional.length === 0 && <p className="pf-empty">这个家族没有可调参数</p>}
        {optional.map(row)}
      </div>

      {family.variants?.length > 0 && (
        <div className="pf-group" role="group" aria-labelledby="pf-g-var">
          <div className="pf-group-title" id="pf-g-var">预设变体</div>
          <div className="pf-chips">
            {family.variants.map((v) => (
              <button
                key={v.id}
                type="button"
                className="pf-chip"
                disabled={disabled}
                onClick={() => onChange({ ...params, ...(v.params || {}) })}
              >
                {v.name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
