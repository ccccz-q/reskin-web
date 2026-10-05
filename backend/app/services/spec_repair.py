"""服务层 · 提炼产物的「安装前体检与修复」

★ 为什么需要这一层（2026-10-01 连续两次实测教训）
------------------------------------------------
工坊提炼出的家族是**模型写的**，缺陷形态稳定复现：

  ① 占位符写双花括号 `{{subject.name}}` → 严格渲染报「未解析占位符」
  ② `option_labels` 写成 list（应为 dict）→ family_meta 崩 → /api/families 整个 500
  ③ dicts 漏覆盖某个 enum → `{x_desc}` 悬空 → 渲染失败
  ④ hard_forbid 不足 4 条 / 缺段 / layout 取值越界 / allow_change 含未知维度

这些缺陷的共同点是：**装得进去、用起来才炸**。用户视角就是「装了却在风格列表里
看不到 / 选了却渲染不出提示词」。

所以在安装写盘之前强制走一遍：
    代码修复（能修的修掉）→ 严格渲染冒烟（与运行时同源）→ 通过才写盘
修得好的装上就能用；修不好的直接拦住并告诉用户具体原因，绝不产出带病家族。

★ 只做**结构与语法**层面的修复，不碰创意内容：
  模型的风格设计（保留什么、画什么、禁止什么）一个字不改。
"""
from __future__ import annotations

import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.logging import logger  # noqa: E402
from services.family_renderer import (  # noqa: E402
    ALL_DIMENSIONS,
    DERIVED_KEYS,
    FAMILY_REQUIRED,
    LAYOUTS,
    SCOPES,
    SEGMENT_NAMES,
)

# 双花括号 → 单花括号（模型在 YAML 语境常多写一层）
_DOUBLE_BRACE_RE = re.compile(r"\{\{([\w.]+)(?::[^{}]*)?\}\}")
# 任意占位符（含单/双括号写法），用于后续的白名单判定
_ANY_PH_RE = re.compile(r"\{\{?([^{}]{1,200}?)\}\}?")

# 保真底线：hard_forbid 不足 4 条时用它补齐（风格无关的通用条目）
_FIDELITY_FALLBACK = [
    "重绘或改变原照片核心主体的身份、结构与真实质感",
    "改变原照片的构图、透视关系与地平线高度",
    "额外添加与画面无关的文字、logo、水印或界面元素",
    "商业广告感、廉价 AI 感与过度修饰",
]

_SEG_FALLBACK = {
    "preserve": "严格保留原照片核心主体的身份、结构、姿态与真实质感，不得重绘或插画化。",
    "creative": "按本家族的风格要求重构画面，整体必须与原照片有明确对应关系，一眼能认出来源于它。",
    "forbid": "避免：{hard_forbid_joined}。",
}


def _norm_double_braces(text: str) -> str:
    return _DOUBLE_BRACE_RE.sub(r"{\1}", text or "")


def _repair_placeholders(text: str, card: dict, notes: list[str]) -> str:
    """把模型自造的占位符就地消解

    与 style_forge._resolve_unknown_slots 同思路（机制复用，独立实现以免互相牵连）：
      · 合法槽位（派生量 / 参数 / 参数_desc）→ 原样保留
      · 含「主体/主角/subject」→ 填视觉卡主体
      · 含「锚点」→ 填视觉锚点
      · 「段名:内容」式 → 剥壳保留描述；纯「段名:数字」→ 摘掉
      · 其余 → 剥壳保留内容（模型写在占位符里的本来就是想放的话）
    """
    if not isinstance(text, str) or "{" not in text:
        return text

    subj = card.get("subject")
    subject_name = ""
    if isinstance(subj, dict):
        subject_name = str(subj.get("name") or "")
    elif isinstance(subj, str):
        subject_name = subj
    anchors = "、".join(
        str(a.get("desc")) for a in (card.get("anchors") or [])
        if isinstance(a, dict) and a.get("desc"))
    seg_names = set(SEGMENT_NAMES) | {"text", "figures", "dicts", "params"}

    def _sub(m: "re.Match") -> str:
        ph = m.group(1).strip()
        if not ph:
            return ""
        core = ph.split(":", 1)[0].strip()
        if core in DERIVED_KEYS or ph in DERIVED_KEYS or core.endswith("_desc"):
            return m.group(0)          # 合法槽位：原样
        if any(k in ph for k in ("主体", "主角", "subject")):
            notes.append(f"占位符 {{{ph}}} 已填为主题")
            return (subject_name or "核心主体").replace("\n", " ")
        if "锚点" in ph:
            notes.append(f"占位符 {{{ph}}} 已填为视觉锚点")
            return (anchors or "视觉锚点").replace("\n", " ")
        if core in seg_names:
            rest = ph.split(":", 1)[1].replace("\n", " ").strip() if ":" in ph else ""
            if len(re.sub(r"[\s，。、；,.;:]+", "", rest)) >= 4:
                notes.append(f"占位符 {{{ph[:20]}}} 已剥壳保留为描述")
                return rest
            notes.append(f"占位符 {{{ph[:20]}}} 无实义已摘除")
            return ""
        cleaned = ph.replace("\n", " ").strip()
        if len(re.sub(r"[\s，。、；,.;:]+", "", cleaned)) >= 4:
            notes.append(f"自造占位符 {{{ph[:20]}}} 已剥壳")
            return cleaned
        return ""

    # 先把双括号归一，再逐项消解
    return _ANY_PH_RE.sub(_sub, _norm_double_braces(text))


def repair_spec(spec: dict, card: dict | None = None) -> tuple[dict, list[str]]:
    """安装前体检 + 修复。返回 (修复后的 spec, 修复说明列表)

    只做结构/语法修复，不动创意内容。任何一步异常都吞掉并继续 ——
    宁可修得不完美，也不能让体检本身把安装弄挂。
    """
    out = dict(spec or {})
    card = card or {}
    notes: list[str] = []

    # ── 1. 必填字段与枚举取值 ─────────────────────────
    if not str(out.get("id") or "").strip():
        out["id"] = "forged_" + os.urandom(4).hex()
        notes.append(f"缺少 id，已生成 {out['id']}")
    if not str(out.get("name") or "").strip():
        out["name"] = str(out.get("id"))
        notes.append("缺少名称，已用 id 兜底")
    if not str(out.get("description") or "").strip():
        out["description"] = f"{out.get('name')}（工坊提炼）"
    out["kind"] = out.get("kind") or "family"

    layout = str(out.get("layout") or "")
    if layout not in LAYOUTS:
        out["layout"] = "full"
        notes.append(f"layout={layout or '空'} 不在允许值内，已回退为 full")

    scope = str(out.get("forbid_scope") or "")
    if scope not in SCOPES:
        out["forbid_scope"] = "whole"
        notes.append(f"forbid_scope={scope or '空'} 不在允许值内，已回退为 whole")

    # ── 1.5 default_aspect：缺失/非法 → origin（跟随原图）──
    # 实测 2026-10-03：提炼产物漏这个字段 → 正方形原图被默认竖版强改成长条。
    da = str(out.get("default_aspect") or "").strip()
    if not da:
        out["default_aspect"] = "origin"
        notes.append("default_aspect 缺失，已补为 origin（跟随原图宽高比）")

    # ── 2. allow_change：剔掉未知维度 ──────────────────
    allow = out.get("allow_change")
    if allow is not None:
        if not isinstance(allow, list):
            allow = [allow] if isinstance(allow, str) else []
            notes.append("allow_change 形状异常，已归一为列表")
        bad = [d for d in allow if str(d) not in ALL_DIMENSIONS]
        if bad:
            out["allow_change"] = [d for d in allow if str(d) in ALL_DIMENSIONS]
            notes.append(f"allow_change 剔除了未知维度 {bad}")
    else:
        out["allow_change"] = ["color", "detail_density", "background"]
        notes.append("缺少 allow_change，已补基础艺术维度")

    # ── 3. hard_forbid：保真底线 4 条 ──────────────────
    hf = out.get("hard_forbid")
    hf = [str(x).strip() for x in hf if str(x).strip()] if isinstance(hf, list) else []
    if not isinstance(hf, list):
        hf = []
    if len(hf) < 4:
        need = _FIDELITY_FALLBACK
        for item in need:
            if len(hf) >= 4:
                break
            if not any(item[:10] in x for x in hf):
                hf.append(item)
        notes.append(f"hard_forbid 不足 4 条，已补齐至 {len(hf)} 条")
    out["hard_forbid"] = hf

    for key in ("suitable",):
        val = out.get(key)
        if val is not None and not isinstance(val, list):
            out[key] = [val] if isinstance(val, str) else []
            notes.append(f"{key} 形状异常，已归一为列表")

    # ── 4. params：形状归一 + enum 完整性 ──────────────
    params = out.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    for pname, pspec in list(params.items()):
        if not isinstance(pspec, dict):
            params.pop(pname)
            notes.append(f"参数 {pname} 结构异常，已移除")
            continue
        # option_labels：list → 与 options 按位置配对成 dict
        ol = pspec.get("option_labels")
        opts = pspec.get("options")
        if isinstance(ol, list) and isinstance(opts, list):
            pspec["option_labels"] = dict(zip([str(o) for o in opts], [str(x) for x in ol]))
            notes.append(f"参数 {pname} 的 option_labels 是数组，已转为字典")
        elif ol is not None and not isinstance(ol, dict):
            pspec.pop("option_labels", None)
            notes.append(f"参数 {pname} 的 option_labels 形状异常，已移除")
        # enum 必须有 options；没有就降级为 string
        if pspec.get("type") == "enum":
            if not isinstance(opts, list) or not opts:
                pspec.pop("type", None)
                pspec["type"] = "string"
                notes.append(f"参数 {pname} 是 enum 却没有 options，已降级为 string")
            elif "default" not in pspec or pspec.get("default") not in opts:
                pspec["default"] = opts[0]
                notes.append(f"参数 {pname} 的 default 缺失/越界，已设为 {opts[0]}")
    out["params"] = params

    # ── 5. dicts：补齐被 segments 引用的 enum 映射 ─────
    dicts = out.get("dicts")
    dicts = dict(dicts) if isinstance(dicts, dict) else {}
    segs_raw = out.get("segments")
    segs_raw = dict(segs_raw) if isinstance(segs_raw, dict) else {}
    joined = "\n".join(str(v) for v in segs_raw.values() if isinstance(v, str))
    for pname, pspec in (out.get("params") or {}).items():
        if not isinstance(pspec, dict):
            continue
        if f"{{{pname}_desc}}" in joined and pname not in dicts:
            opts = [str(o) for o in (pspec.get("options") or [])]
            # 兜底文案优先取选项的中文名（option_labels 上面已归一为 dict），
            # 没有才退回选项值本身 —— 免得中文句子里冒出英文枚举名。
            labels = pspec.get("option_labels")
            labels = labels if isinstance(labels, dict) else {}
            dicts[pname] = {o: str(labels.get(o) or o) for o in opts}
            notes.append(f"dicts 缺 {pname}（被 {{{pname}_desc}} 引用），已按选项补齐")
    out["dicts"] = dicts

    # ── 6. segments：缺段补齐 + 占位符消解 ─────────────
    segs = {}
    for name in SEGMENT_NAMES:
        val = segs_raw.get(name)
        if not isinstance(val, str) or not val.strip():
            segs[name] = _SEG_FALLBACK[name]
            notes.append(f"segments 缺 {name} 段，已补通用兜底")
        else:
            segs[name] = val
    for name in list(segs.keys()):
        segs[name] = _repair_placeholders(segs[name], card, notes)
    out["segments"] = segs

    # ── 7. 形态收尾 ───────────────────────────────────
    if str(out.get("allow_change_mode", "static")) == "dynamic" \
            and not isinstance(out.get("style_allow_change"), dict):
        out["allow_change_mode"] = "static"
        notes.append("声明了 dynamic 却没有 style_allow_change，已回退为 static")

    for k in ("variants",):
        v = out.get(k)
        if v is not None and not isinstance(v, list):
            out[k] = []
            notes.append(f"{k} 形状异常，已置空")

    # 缺的必填字段兜底（理论上前面都已覆盖，这里是最后一道）
    for k in FAMILY_REQUIRED:
        if k not in out:
            out[k] = {"params": {}, "segments": segs}.get(k, "")
            notes.append(f"缺必填字段 {k}，已补默认值")

    if notes:
        logger.info("安装前体检修复了 %d 处：%s", len(notes), notes[:5])
    return out, notes


__all__ = ["repair_spec"]
