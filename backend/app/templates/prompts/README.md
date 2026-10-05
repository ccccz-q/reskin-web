# 工坊提示词资产（templates/prompts/）

工坊（style_forge）的 system prompt 全部外置在这里 —— **改提示词文案不需要动 Python 代码**，
改完 `.md` 保存即可（进程内缓存，重启服务后生效）。

| 文件 | 用途 | 状态 |
|---|---|---|
| `forge_decode.md` | ① 解构：逐张图提取三层视觉语法（Scene Facts / Visual Grammar / Hybrid Decisions） | ✅ 已接线（`style_forge._DECODE_SYSTEM`） |
| `forge_compile.md` | ③ 编译：视觉卡 + 用户意图 → 家族 YAML | ✅ 已接线（`style_forge._COMPILE_SYSTEM`） |
| `forge_world.md` | 世界观意图识别：用户文字里是否明确指定了参照物 | ✅ 已接线（`style_forge._WORLD_SYSTEM`） |
| `forge_positive.md` | 用户意图解析：正向要求（引号短语 / 保留关键词） | ✅ 已接线（`style_forge._POSITIVE_SYSTEM`） |
| `forge_synth.md` | ② 合成：多张图合成一张视觉卡 | ⚠️ **未接线**（多图合成流未启用） |
| `forge_role.md` | 视觉参考分析师（早期单图角色设定） | ⚠️ **未接线**（被 decode 取代） |

## 改动规范

1. **只改文案，不改 JSON 输出契约**：这些 prompt 都约定模型「只返回 JSON」，
   字段名是代码解析的依据 —— 改字段名等于改接口，必须同步改 `style_forge` 的解析代码。
2. **保持「可执行性」判据**：禁止「电影感/氛围感/高级感」这类无法执行的形容词
   （这是造梦师移植的核心判据，见 `造梦师v2.5.0借鉴计划书.md`）。
3. **来源残留必须写成可执行的禁令宾语**，不许夹带「未观察到」「是否保留由后续决定」
   这类流程备注 —— 代码层（`_enforce_residue`）会把这类条目整条丢弃。

## 部署

`templates/prompts/` 必须随代码一起部署（与 `templates/families/` 同级同待遇）。
缺文件时工坊导入即报错（`RuntimeError: 工坊提示词资产缺失`），不会静默降级。
