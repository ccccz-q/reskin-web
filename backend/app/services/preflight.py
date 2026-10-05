"""编译后 Preflight 自检 —— 出图前的最后一道「漂移体检」

════════ 为什么要有这一层 ════════

造梦师 v2.5.0 的 `references/prompt-compiler.md` 里有一节「Dream Decode preflight」，
列出 9 项编译后必须自查的漂移。我们翻译成本项目能**确定性判定**的 6 项。

它的定位是：**编译完了、还没花钱出图之前**，用代码把「这次编译大概率会跑偏」的
信号挑出来，而不是等用户看到图再说。

★ 与 family_qc 的分工
----------------------
`family_qc.qc_family` 是**离线体检**：家族 YAML 写得好不好，人工跑一次看报告。
`preflight` 是**运行时体检**：每次出图都跑，输入是「这一次的家族 + 参数 + 提炼卡 + 渲染结果」。

★ 为什么不阻断出图
------------------
我们无法离线证明「阻断」不会误伤正常请求。造梦师的 preflight 是给 LLM 读的自然语言
清单，判错了顶多多写一句；我们是代码判定，判错了就是**用户点生成却出不来图**。

所以第一阶段只做三件事：① 写进 warnings；② 记日志；③ 不影响任何既有输出。
等真实数据证明零误报，再考虑升级为阻断。

★ 判定原则：宁可漏报，不可误报
------------------------------
所有检查都要求「有明确证据才报」—— 家族没声明媒介就跳过媒介检查，
没有残留清单就跳过残留检查。**不确定时一律不报。**
误报会让用户关掉整个告警通道（狼来了），漏报只是少一条提示。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ★ 阈值单一事实源：从 family_renderer 引入，不在两处写字面量。
#   此前过载阈值/条数上限在两个文件各写一份、注释写着「同口径」但无代码约束，
#   将来改一处漏一处就是「注释与实现不符」的典型温床（审查 P2-5）。
from services.family_renderer import (                       # noqa: E402
    _MAX_FORBID_CLAUSES, _MAX_TOTAL_CHARS,
)


# ─────────────────── 词库 ───────────────────

# 摄影/实拍特征词：非摄影媒介的创意段里出现这些，说明媒介被悄悄改写成摄影了
_PHOTO_MARKS = (
    "浅景深", "景深虚化", "焦外", "虚化背景", "35mm", "50mm", "85mm",
    "焦距", "光圈", "实拍", "电影镜头", "电影级", "bokeh", "hdr", "HDR",
    "高动态范围", "光影质感写实", "照片级真实", "写实渲染", "pbr", "PBR",
)

# 非摄影媒介特征词：只有媒介文本里明确出现这些，才判定「本家族是非摄影媒介」
_NON_PHOTO_MARKS = (
    "纸", "插画", "印刷", "版画", "网点", "丝网", "riso", "Riso", "RISO",
    "像素", "拼贴", "collage", "水彩", "水墨", "手绘", "平面", "graphic",
    "paper", "print", "pixel", "ink", "sticker", "贴纸", "漫画", "涂鸦",
    "木刻", "蚀刻", "平版", "凸版", "手工", "剪纸", "zine",
)

# （过载/条数阈值从 family_renderer 引入，见文件头 —— 不在此重复定义）


# ─────────────────── 工具 ───────────────────

def _text(value: Any) -> str:
    """把任意形态的取值压成纯文本（家族 YAML 形状不总是规整）"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # medium 通常写成 {primary,constraints,avoid}
        for k in ("primary", "name", "text"):
            v = value.get(k)
            if isinstance(v, str):
                return v
        return " ".join(str(x) for x in value.values() if isinstance(x, str))
    if isinstance(value, (list, tuple)):
        return " ".join(str(x) for x in value)
    return str(value)


def _medium_of(spec: dict, card: dict) -> str:
    """取「主媒介」文本 —— 家族优先，其次提炼卡

    家族里的 `card_medium` 是工坊编译时从视觉卡存档下来的（见 style_forge）。
    老家族没有这个字段 → 返回空串 → 媒介检查自动跳过（零误报）。
    """
    m = spec.get("card_medium")
    if m:
        return _text(m)
    m = (card or {}).get("medium")
    if m:
        return _text(m)
    return ""


def _is_non_photo(medium_text: str) -> bool:
    """是否**明确**是非摄影媒介

    ★ 必须显式命中非摄影词才判 True。
      媒介文本缺失或含糊时返回 False —— 不确定就不报，避免误伤。
    """
    if not medium_text:
        return False
    return any(w in medium_text for w in _NON_PHOTO_MARKS)


def _residue_of(spec: dict, card: dict) -> list[str]:
    """取来源残留清单

    家族里的 `card_source_residue` 是工坊编译时存档的（视觉卡的 source_residue）。
    老家族没有 → 空列表 → 残留检查自动跳过。
    """
    out: list[str] = []
    for src in (spec.get("card_source_residue"), (card or {}).get("source_residue")):
        if isinstance(src, (list, tuple)):
            out.extend(str(x) for x in src if str(x).strip())
    # 去重保序
    return list(dict.fromkeys(out))


def _locked_descs(spec: dict, params: dict, locked: list[str] | None) -> list[tuple[str, str]]:
    """USER-LOCKED 参数 → 它对应的中文文案（用于验证「锁定是否真的落地」）

    返回 [(参数名, 期望出现在提示词里的文案), ...]
    取不到文案的参数跳过（比如 string 型自由输入，或家族没配 dicts）。
    """
    dicts = spec.get("dicts") or {}
    if not isinstance(dicts, dict):
        return []
    out: list[tuple[str, str]] = []
    for name in (locked or []):
        name = str(name)
        mapping = dicts.get(name)
        if not isinstance(mapping, dict):
            continue
        picked = params.get(name)
        desc = mapping.get(picked)
        if desc is None and isinstance(picked, str):
            # bool / 字符串形态的容错（HTTP 传来的是字符串）
            for alt in (picked.lower(), picked):
                if alt in mapping:
                    desc = mapping[alt]
                    break
        if isinstance(desc, str) and desc.strip():
            # ★ 必须与渲染侧同口径：渲染时 dicts 文案会先过 _strip_lead_verb()
            #   剥掉「采用/使用/让…」等引导动词（prompt_cleanup，渲染 {x_desc} 桥接处）。
            #   不剥的话探针找「使用明亮蓝天天光…」，渲染产物是「明亮蓝天天光…」，
            #   必然误报「USER-LOCKED 未落地」（审查 P1-1 实锤：
            #   voxel_path_reflection.yaml 的 light_mode / water_position 全中）。
            #   —— 这与「宁可漏报不可误报」的立项原则直接冲突，误报会逼用户关掉告警。
            from services.prompt_cleanup import _strip_lead_verb
            out.append((name, _strip_lead_verb(desc.strip())))
    return out


# ─────────────────── 主入口 ───────────────────

def preflight(
    spec: dict | None,
    params: dict | None = None,
    card: dict | None = None,
    segments: dict | None = None,
    prompt: str = "",
    locked: list | None = None,
) -> list[str]:
    """编译后自检，返回人类可读的告警列表（空列表 = 没发现问题）

    参数
      spec     家族文档（渲染源）
      params   本次实际参数（已含默认值与 auto 兜底后的结果）
      card     本次提炼卡
      segments 渲染出的三段式
      prompt   最终提示词（三段拼好之后）
      locked   USER-LOCKED 参数名列表

    本函数**永不抛异常**：它是出图链路上的旁挂检查，不该有能力把出图打挂。
    """
    spec = spec or {}
    params = dict(params or {})
    card = dict(card or {})
    segments = dict(segments or {})
    creative = str(segments.get("creative") or "")
    forbid = str(segments.get("forbid") or "")
    prompt = str(prompt or "")

    warnings: list[str] = []

    try:
        # ── 1. 媒介漂移（造梦师 preflight #1 / #7）────────────────
        # 主媒介在造梦师的 Priority Gate 里排第 3，仅次于锁定事实与参考职责。
        # 纸本插画被写成「浅景深 / 电影镜头」= 媒介身份丢失 = 必然不像。
        medium_text = _medium_of(spec, card)
        if _is_non_photo(medium_text):
            hits = [w for w in _PHOTO_MARKS if w in creative]
            if hits:
                warnings.append(
                    f"媒介漂移：本家族主媒介是「{medium_text[:24]}」（非摄影），"
                    f"但 creative 里出现摄影化表述 {hits[:4]} —— "
                    f"出图可能被改写成实拍质感"
                )

        # ── 2. 来源残留泄漏（造梦师 preflight #3）────────────────
        # 工坊已用 _enforce_residue 把残留烘焙进 forbid 段；
        # 这里验证它们**在渲染之后还活着**（可能被残句清理或占位符问题删掉）。
        for r in _residue_of(spec, card):
            key = r[:14]
            # prompt 是三段拼接（包含 forbid），查 forbid 一处即可覆盖
            if key and key not in forbid:
                warnings.append(
                    f"来源残留未被拦截：{r[:30]} 未出现在 forbid 段 —— "
                    f"参考图的身份/品牌/地点可能泄漏进新图"
                )

        # ── 3. USER-LOCKED 未落地（造梦师 preflight #6 场景意图损伤）──
        # 用户显式选过的参数，其取值文案必须真的出现在提示词里。
        # 没出现 = 锁定被静默吞掉（残句清理 / dicts 缺项 / 占位符悬空都可能造成）。
        for name, desc in _locked_descs(spec, params, locked):
            probe = desc[:12]
            if probe and probe not in prompt:
                warnings.append(
                    f"USER-LOCKED 未落地：参数 {name} 的取值「{desc[:24]}」"
                    f"未出现在提示词中 —— 用户的显式选择可能被丢弃"
                )

        # ── 4. forbid ↔ creative 自相矛盾 ────────────────────────
        clauses = [x for x in forbid.replace("；", "\n").splitlines() if x.strip()]
        if len(clauses) > _MAX_FORBID_CLAUSES:
            warnings.append(
                f"forbid 条数 {len(clauses)} 超过 {_MAX_FORBID_CLAUSES} 条，会稀释 creative 权重"
            )

        # ── 5. Prompt 过载（造梦师 preflight #9）─────────────────
        total = sum(len(str(v)) for v in segments.values())
        if total > _MAX_TOTAL_CHARS:
            warnings.append(
                f"提示词总长 {total} 字，超过 {_MAX_TOTAL_CHARS} 字阈值 —— "
                f"权重被稀释，关键约束可能失效"
            )

        # ── 6. 漂移维度提示（用视觉卡自己声明的易跑偏维度）─────────
        # 造梦师把 Drift Warnings 留在卡里供修复与自检使用。
        # 我们目前没有自动修复链路，先作为「这次要重点看什么」提示出来。
        dw = spec.get("card_drift_warnings")
        if not dw:
            dw = (card or {}).get("drift_warnings")
        if isinstance(dw, (list, tuple)) and dw:
            items = [str(x).strip() for x in dw if str(x).strip()][:3]
            if items:
                warnings.append(
                    "本家族易跑偏维度：" + "；".join(x[:28] for x in items)
                )

    except Exception as e:                                  # 兜底：旁挂检查不许打挂出图
        logger.warning("preflight 自检异常（已忽略）：%s: %s", type(e).__name__, e)
        return []

    if warnings:
        logger.info("preflight 报出 %d 条漂移提示：%s", len(warnings), warnings[:3])
    return warnings
