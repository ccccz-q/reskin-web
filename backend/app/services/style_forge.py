"""服务层 · 模板工坊（Style Forge）

════ 它要解决的问题 ════

用户手上有一批**同类型**的图片（比如一整组 Risograph 印刷品、一组撕纸拼贴海报），
可能还会附一段风格理论。期望是：

    传入多张图（±理论）→ 自动分析出它们的视觉特征 → 写出提示词 → 作为家族模板使用

关键在"自动分析出特征"——不是把图压成几个风格词（`复古`、`电影感`、`颗粒`），
而是**区分这张图"里有什么"和"它凭什么是这个样子"**：
人物、地点、事件可以换；媒介、色彩关系、空间组织、材质逻辑、笔触与反套路口径
应该被提炼出来并迁移到新场景。

════ 为什么拆成三段 ════

一次让模型"看图 → 直接写家族 YAML"会失败在两个地方：
  1. 分析和写作混在一起，模型会跳过分析直接编模板，写出来的三段式是空话；
  2. 家族 YAML 结构复杂（params/dicts/segments/校验约束），
     同时还要做视觉分析，两件事互相干扰，校验经常过不了。

所以拆成：

    ① 解构 DECODE   逐张图提取「视觉语法」，严格分三层
    ② 合成 SYNTH    多张图合成一张「视觉卡」（核心规则 5–8 条 + 迁移范围 + 来源残留）
    ③ 编译 COMPILE  视觉卡 + 用户意图 → 家族 YAML（只吃 3–5 条当前相关的规则）

前一段的产物是后一段的唯一输入，中间产物（视觉卡）本身就是可复用资产：
解构一次，之后换场景、换风格名都能再编译，不用重新看图。

════ 理论文本的正确角色 ════

理论（比如 Risograph 的起源与工艺）是**意图锚点**，不是风格来源。
它告诉模型"用户想要什么方向"，但**不能取代对图片的观察**——
图片里没有的特征，理论再详细也不许写进提示词。

════ 降级策略 ════

没配 VISION_MODEL 时，① 退化为"基于本地客观测量（色板/明度/朝向）+ 理论文本"
的解构。质量会下降，但链路仍然完整可用、仍产出结构合法的视觉卡与家族。
配了之后自动切换到逐张图看图的解构路径。
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from functools import lru_cache
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 工坊 system prompt 已外置为 templates/prompts/ 资产（god module 拆分第三刀）：
#   改提示词文案不再需要动 Python 代码 —— 这是个以提示词为核心资产的项目，
#   文案迭代频率远高于代码逻辑，值得有自己的「文案层」。
#   ⚠️ templates/prompts/ 必须随代码一起部署（与 templates/families/ 同级同待遇）。
_PROMPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "templates", "prompts")
)


@lru_cache(maxsize=1)
def _load_prompt(filename: str) -> str:
    """读取外置的工坊 system prompt（模块加载期即调用 → 缺文件会大声失败）"""
    path = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise RuntimeError(
            f"工坊提示词资产缺失：{path}（{e}）—— "
            f"templates/prompts/ 目录必须与代码一起部署，否则工坊无法编译。"
        ) from e


import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from config import (  # noqa: E402
    ANALYZE_TIMEOUT_SEC,
    FORGE_COMPILE_TIMEOUT_SEC,
    FORGE_REPAIR_TIMEOUT_SEC,
    FORGE_TOTAL_BUDGET_SEC,
    FORGE_VLM_TIMEOUT_SEC,
    FORGE_DECODE_WORKERS,
    FORGE_MAX_IMAGES,
)
from infra.logging import logger, step  # noqa: E402
from services.card_extractor import (  # noqa: E402
    build_card,
    check_observable_rules,
    summarize_card,
)
from services.llm import LLMError, chat, extract_json  # noqa: E402

MAX_IMAGES = FORGE_MAX_IMAGES
MAX_REPAIR = 1


# ══════════════════ ① 角色分配（多图时）══════════════════



# ══════════════════ ② 单图解构 ═══════════════════

_DECODE_SYSTEM = _load_prompt("forge_decode.md")   # 外置资产：templates/prompts/forge_decode.md


# ══════════════════ ③ 多图合成视觉卡 ═══════════════════



# ══════════════════ ④ 编译为家族 ═══════════════════

_COMPILE_SYSTEM = _load_prompt("forge_compile.md")   # 外置资产：templates/prompts/forge_compile.md


_REPAIR_TMPL = """这是一版已基本完成的家族模板，只有以下错误：

{errors}

外科修补契约（严格遵守）：
- 最小改动：只修改上面错误直接涉及的字段与行，每个错误用尽可能小的改动修复；优先修因，不止改表症。
- 逐字保留：除错误直接涉及的部分外，其余所有内容——name、description、params、dicts、variants、segments 的其它行——必须与上一版逐字一致，一个字都不改。
- 禁止重新设计或重写未出错的段落；禁止发明任何 {{花括号占位符}}；禁止把 segments 写成「段名:内容」式槽位引用；禁止出现平台名称。
- 结构性要求不变：enum 必须有 options 和合法的 default，dicts 必须覆盖每个 enum 参数的全部 options。

按契约输出修正后的完整 JSON（结构不变，只落实上述最小修改）。
"""


def _strip_fence(text: str) -> str:
    """模型偶尔会裹 ```json 代码块 —— 容错剥掉"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _vlm_json(image_path: str, system: str, extra: str = "",
               timeout: float | None = None) -> dict:
    """用视觉模型对单张图做结构化分析。任何失败都返回 {} —— 调用方会退回降级路径。"""
    from config import VISION_MODEL
    if not VISION_MODEL:
        return {}
    try:
        from services.card_extractor import _prepare_for_vlm
        from config import VISION_MODEL as _vm
        # ★ 看图统一走 services/llm.vision()：那里有空响应识别 / 退避重试 /
        #   从异常报文抢救内容。此处原先自己 new OpenAI 且零重试 ——
        #   上游一次抖动就把这张图的 VLM 解构打没了（见 llm.py 顶部注释）。
        from services.llm import vision

        b64, mime = _prepare_for_vlm(image_path)
        content = [{"type": "text", "text": system + (("\n\n" + extra) if extra else "")},
                   {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
        with step("看图解构", model=_vm, file=os.path.basename(image_path)):
            text = vision(
                [{"role": "user", "content": content}],
                max_tokens=2000,
                temperature=0.3,
                response_format={"type": "json_object"},
                # ★ 阶段超时（2026-10-07）：此前主路径**完全不限时**，
                #   上游卡住就是无限等。超时抛 LLMError → 上面 except 收{} →
                #   这张图降级为纯证据卡，其余图照常，**整条链路不会死**。
                timeout=timeout if timeout is not None else FORGE_VLM_TIMEOUT_SEC,
            )
        return extract_json(_strip_fence(text)) or {}
    except Exception as e:
        logger.warning("看图解构失败（%s）：%s。退回文本解构。", os.path.basename(image_path), e)
        return {}


def _evidence_one(path: str, use_vlm: bool) -> tuple[dict | None, str | None]:
    """单张参考图 → (证据, 警告)。供 _reference_evidence 并行调用。"""
    card = build_card(path, use_vlm=use_vlm)
    if not card:
        return None, f"参考图 {os.path.basename(path)} 无法解析，已跳过"
    ev = {
        "file": os.path.basename(path),
        "orientation": card.get("orientation"),
        "light": card.get("light_hint"),
        "palette": [{"name": c.get("name"), "hex": c.get("hex"),
                     "ratio": c.get("ratio")} for c in (card.get("palette") or [])],
        "subject": card.get("subject"),
        "anchors": [a.get("desc") for a in (card.get("anchors") or [])],
        "risk_notes": card.get("risk_notes"),
        "summary": summarize_card(card),
        "origin": card.get("_origin"),
    }
    warn = (f"参考图 {os.path.basename(path)} 只做了本地取色，缺少主体与结构信息"
            if card.get("_origin") == "local" else None)
    return ev, warn


def _reference_evidence(image_paths: list[str], use_vlm: bool) -> tuple[list[dict], list[str]]:
    """把参考图转成文本证据（本地客观测量 + 可选 VLM）。保留旧签名以兼容既有测试。

    ★ 性能修复：每张图一次 VLM 调用约 15s，串行 6 张 ≈ 90s（用户实测 6 分钟的主要来源）。
      与 decode 轮同样用线程池并行；pool.map 保序，输出顺序与旧串行版一致。
      VLM 客户端每次调用独立创建，线程安全无共享状态。
    """
    targets = list(image_paths[:MAX_IMAGES])
    evidence: list[dict | None] = [None] * len(targets)
    warns: list[str | None] = [None] * len(targets)
    if targets:
        workers = max(1, min(FORGE_DECODE_WORKERS, len(targets)))
        with step("参考图证据", images=len(targets), workers=workers):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda pth: _evidence_one(pth, use_vlm), targets))
        for i, (ev, warn) in enumerate(results):
            evidence[i], warns[i] = ev, warn
    warnings = [w for w in warns if w]
    if len(image_paths) > MAX_IMAGES:
        warnings.append(f"参考图超过 {MAX_IMAGES} 张，只分析了前 {MAX_IMAGES} 张")
    return [e for e in evidence if e], warnings


def _decode_single(path: str, role: dict | None, evidence: dict,
                   use_vlm: bool = True, style_prompt: str = "",
                   cancelled: "callable | None" = None) -> dict:
    """① 单图解构：优先看图，失败则基于本地证据做文本解构

    ★ 每张图**只走一次模型**。
      之前 `forge()` 会先用 VLM 跑一遍 `build_card(use_vlm=True)` 取证据，
      这里又跑一遍看图解构 —— 配了视觉模型后等于**每张图看两次**（6 张 = 12 次看图调用）。
      现在证据一律走本地客观测量（便宜且真实），看图只发生在这一处。
    cancelled：中止探测 —— 解构是多图并行的长阶段（单次 VLM 20–60s），
      取消检查点原来只在整段解构之后，多图时「中止」要等几分钟才生效（实测）；
      每张图开始前先查一次，还没开始的图直接跳过。
    """
    if cancelled and cancelled():
        logger.info("解构已中止，跳过：%s", os.path.basename(path))
        return {}
    extra = ""
    if role:
        extra += f"这张图在本组中的职责：{role.get('role', '未指定')}（{role.get('focus', '')}）\n"
    if evidence.get("summary"):
        extra += f"本地客观测量结果（真实数据，可直接引用，不要与之矛盾）：{evidence['summary']}"
    if (style_prompt or "").strip():
        extra += (
            "\n\n【用户提供的风格提示词（权威的风格词汇来源，优先级高于你对图片的直觉）】\n"
            f"{style_prompt.strip()[:1500]}\n"
            "（其中的正向词应体现在 core_rules 与材质/色彩描述里；"
            "负面提示词/反向词应理解为该风格的禁止项。）"
            "\n\n【输出硬约束】core_rules 与各描述必须是完整直白的中文句子，"
            "严禁发明任何 {花括号占位符}、严禁输出「段名:内容」式的槽位引用、"
            "严禁出现英文 Prompt 原文里的平台名称（Midjourney/Stable Diffusion 等）。"
        )
    got = _vlm_json(path, _DECODE_SYSTEM, extra) if use_vlm else {}
    if got.get("core_rules"):
        got["_source"] = "vlm"
        return got
    # 降级：用本地证据做一次纯文本解构（质量下降但结构完整）
    basis = json.dumps({
        "file": evidence.get("file"),
        "orientation": evidence.get("orientation"),
        "light": evidence.get("light"),
        "palette": evidence.get("palette"),
        "subject": evidence.get("subject"),
        "anchors": evidence.get("anchors"),
        "risk_notes": evidence.get("risk_notes"),
    }, ensure_ascii=False)
    prompt = (f"无法直接看到图片，只能依据下面的客观测量数据推断其视觉语法。\n"
              f"数据：{basis}\n"
              f"职责：{role.get('role') if role else '未指定'}\n"
              f"不确定就写'未观察到'，不要编造。")
    try:
        raw = chat([{"role": "system", "content": _DECODE_SYSTEM},
                    {"role": "user", "content": prompt}],
                   temperature=0.4, max_tokens=1200,
                   response_format={"type": "json_object"},
                   timeout=ANALYZE_TIMEOUT_SEC)
        got = extract_json(_strip_fence(raw)) or {}
    except LLMError as e:
        logger.warning("文本解构失败：%s", e)
        got = {}
    got["_source"] = "text"
    return got


def _assign_roles(paths: list[str]) -> list[dict]:
    """多图职责分配 —— 确定性规则，**不再为它单独发一次模型调用**。

    造梦师的流程里没有这一步：每张图独立建 Scene Card，合成时靠代码取交集。
    原先这里为了知道"哪张是主样本"专门发一轮 LLM，实测要 20–30s，
    而按文件名顺序取第一张当主样本、其余当佐证，效果完全等价。
    """
    if len(paths) < 2:
        return [{"i": 0, "role": "全部", "focus": "单张参考图"}]
    return [
        {"i": 0, "role": "主样本", "focus": "最能代表这组风格的基准图"},
        *[{"i": i, "role": "佐证", "focus": "用于交叉验证与补充共性"} for i in range(1, len(paths))],
    ]


def _norm_rule(r: str) -> str:
    """规则归一化：去空白与标点，用于跨图判重"""
    return "".join(ch for ch in str(r or "") if not ch.isspace() and ch not in "。，、；：,. ")


def _most_common(items: list[str], limit: int = 3) -> str:
    """取出现最多的一条文本（同频次时取最长的那条——信息量更大）"""
    cands = [x for x in items if isinstance(x, str) and x.strip()]
    if not cands:
        return ""
    counted: dict[str, int] = {}
    for x in cands:
        k = _norm_rule(x)
        counted[k] = counted.get(k, 0) + 1
    best = max(counted.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    for x in cands:
        if _norm_rule(x) == best:
            return x.strip()
    return ""


def _synthesize(decodes: list[dict], theory: str,
                evidence: list[dict] | None = None) -> dict:
    """② 合成视觉卡 —— **纯代码合并，不调模型**（造梦师的做法）

    造梦师的流程里没有"合成"这一步：每张图独立建 Scene Card，
    需要共性时由代码取交集。原来这里专门发一轮 LLM 去"归纳 N 份解构"，
    实测 30–60s，而它做的事本质是可确定的：
      - core_rules：按出现频次排序，取跨图共识（单图时全取）
      - shared_grammar：每个维度取出现最多的那条
      - source_residue：取并集（宁可多禁，不可漏禁）
    改成纯代码后既省一轮调用，也更稳（模型不会在这步自己发挥）。
    """
    decodes = [d for d in (decodes or []) if isinstance(d, dict)]
    n = len(decodes)
    card: dict = {}

    # ── core_rules：跨图共识优先 ──
    freq: dict[str, int] = {}
    sample: dict[str, str] = {}
    rank: dict[str, int] = {}
    for i, d in enumerate(decodes):
        for r in (d.get("core_rules") or []):
            if not isinstance(r, str) or not r.strip():
                continue
            k = _norm_rule(r)
            if not k:
                continue
            freq[k] = freq.get(k, 0) + 1
            sample.setdefault(k, r.strip())
            rank.setdefault(k, i)                 # 主样本(0)的规则优先
    ordered = sorted(freq.items(), key=lambda kv: (-kv[1], rank[kv[0]]))
    if n >= 2:
        need = max(1, (n + 1) // 2)               # 至少被半数图支持
        consensus = [sample[k] for k, c in ordered if c >= need]
    else:
        consensus = [sample[k] for k, _c in ordered]
    # 不足 5 条时按频次补齐（单图的规则、再是其余）
    for k, _c in ordered:
        if len(consensus) >= 5:
            break
        if sample[k] not in consensus:
            consensus.append(sample[k])
    consensus = consensus[:8]

    # 形容词兜底：命中的剔除并记 warning（与旧版一致）
    clean, style_warns = [], []
    for r in consensus:
        hits = check_observable_rules(r)
        if hits:
            style_warns.append(f"规则含不可观察形容词（{','.join(hits)}），已剔除：{r[:40]}")
            logger.warning("视觉卡规则含不可观察形容词（%s），已剔除：%s", ",".join(hits), r)
            continue
        clean.append(r)
    card["core_rules"] = clean
    card["style_warnings"] = style_warns

    # ── shared_grammar：每个维度取出现最多的那条 ──
    grammar_keys = [
        ("core_subjects", "核心主体"),
        ("spatial_invariants", "空间不变量"),
        ("dominant_gesture", "主导动势"),
        ("visual_weight_map", "视觉权重"),
        ("color_atmosphere", "色彩氛围"),
        ("source_shape_candidates", "源形状候选"),
        ("natural_quiet_areas", "天然安静区"),
        ("semantic_minimum", "语义最小集"),
        ("supporting_elements", "支撑元素"),
    ]
    grammar: dict = {}
    for key, _label in grammar_keys:
        vals: list[str] = []
        for d in decodes:
            v = d.get(key)
            if isinstance(v, list):
                vals.extend([str(x) for x in v if x])
            elif isinstance(v, str) and v.strip():
                vals.append(v.strip())
        picked = _most_common(vals)
        if picked:
            grammar[key] = picked
    card["shared_grammar"] = grammar

    # ── medium：主媒介取共识，约束与禁忌取并集 ──
    primaries: list[str] = []
    constraints: list[str] = []
    avoids: list[str] = []
    for d in decodes:
        m = d.get("medium")
        if isinstance(m, dict):
            if isinstance(m.get("primary"), str) and m["primary"].strip():
                primaries.append(m["primary"].strip())
            constraints.extend([str(x) for x in (m.get("constraints") or []) if x])
            avoids.extend([str(x) for x in (m.get("avoid") or []) if x])
        elif isinstance(m, str) and m.strip():
            primaries.append(m.strip())
    card["medium"] = {
        "primary": _most_common(primaries, 1),
        "constraints": list(dict.fromkeys(constraints))[:6],
        "avoid": list(dict.fromkeys(avoids))[:6],
    }

    # ── source_residue / anti_clichés：取并集（宁可多禁）──
    residue: list[str] = []
    anticli: list[str] = []
    for d in decodes:
        residue.extend([str(x) for x in (d.get("source_residue") or []) if x])
        anticli.extend([str(x) for x in (d.get("anti_clichés") or d.get("anti_cliches") or []) if x])
    card["source_residue"] = list(dict.fromkeys(residue))[:8]
    card["anti_clichés"] = list(dict.fromkeys(anticli))[:6]

    # ── 造梦师建卡字段聚合（2026-10-03）：语义核/情感残留取共识（一条最有
    #    代表性的），丢弃清单取并集（编译端「必须消失」的素材），
    #    转换机会取并集前几条（creative 的转换手法素材）──
    nuc: list[str] = []
    emo: list[str] = []
    discards: list[str] = []
    transforms: list[str] = []
    for d in decodes:
        if isinstance(d.get("semantic_nucleus"), str) and d["semantic_nucleus"].strip():
            nuc.append(d["semantic_nucleus"].strip())
        if isinstance(d.get("emotional_residue"), str) and d["emotional_residue"].strip():
            emo.append(d["emotional_residue"].strip())
        discards.extend([str(x) for x in (d.get("discard_list") or []) if x])
        transforms.extend([str(x) for x in (d.get("transformation_opportunities") or []) if x])
    if nuc:
        card["semantic_nucleus"] = _most_common(nuc, 1)
    if emo:
        card["emotional_residue"] = _most_common(emo, 1)
    if discards:
        card["discard_list"] = list(dict.fromkeys(discards))[:6]
    if transforms:
        card["transformation_opportunities"] = list(dict.fromkeys(transforms))[:4]

    # ── 主体与锚点：直接取自 Scene Card（不再依赖本地-only 的提取卡）──
    #    证据轮走的是本地测量，拿不到主体名 —— 以前 card 里没有 subject，
    #    渲染时 {subject.name} 只能降级成通用表述，保真度因此打折。
    subj_text = str(grammar.get("core_subjects") or "").strip()
    if subj_text:
        card["subject"] = {"name": subj_text[:40]}

    anchor_pool: list[str] = []
    for d in decodes:
        for key in ("source_shape_candidates", "spatial_invariants"):
            v = d.get(key)
            if isinstance(v, list):
                anchor_pool.extend([str(x).strip() for x in v if str(x).strip()])
            elif isinstance(v, str) and v.strip():
                anchor_pool.append(v.strip())
    seen: set[str] = set()
    anchors: list[dict] = []
    for a in anchor_pool:
        k = _norm_rule(a)
        if not k or k in seen:
            continue
        seen.add(k)
        anchors.append({"desc": a[:60]})
        if len(anchors) >= 4:
            break
    if anchors:
        card["anchors"] = anchors

    if evidence:
        first = evidence[0] if isinstance(evidence[0], dict) else {}
        if first.get("orientation"):
            card["orientation"] = first["orientation"]
        if first.get("light"):
            card["light_hint"] = first["light"]

    # ── 迁移范围：确定性派生（原图重绘场景：主体强继承）──
    card["transfer_scope"] = {
        "strong": ["主体身份与姿态", "色彩结构与占比", "明度与曝光行为",
                   "材质与表面反应", "媒介特征"],
        "conditional": [{"item": "构图与空间组织", "action": "适应新场景主体"},
                        {"item": "视角与主体尺度", "action": "适应新场景叙事"}],
        "do_not": [],
    }
    card["drift_warnings"] = []

    # ── 色板：客观测量合并（不靠模型猜）──
    if evidence:
        merged: dict[str, list] = {}
        for e in evidence:
            for c in (e.get("palette") or []):
                if not isinstance(c, dict):
                    continue
                name = c.get("name") or c.get("hex") or "?"
                cell = merged.setdefault(name, [0.0, 0])
                try:
                    cell[0] += float(c.get("ratio") or 0)
                except (TypeError, ValueError):
                    pass
                cell[1] += 1
        ranked = sorted(merged.items(), key=lambda kv: -(kv[1][0] / max(1, kv[1][1])))
        card["palette"] = [
            {"name": k, "ratio": round(v[0] / max(1, v[1]), 3), "votes": v[1]}
            for k, v in ranked[:5]
        ]

    if (theory or "").strip():
        card["theory_note"] = theory.strip()[:400]
    card["_source"] = "merge"                     # 诊断用：这张卡是代码合并出来的
    return card


def _compile(card: dict, intent: str, base: dict | None,
             style_prompt: str = "", world: dict | None = None,
             positive: dict | None = None) -> dict:
    """③ 视觉卡 → 家族 JSON

    world：世界观意图识别的命中结果（{"world_name", "world_kit"}，未命中为 None）。
    positive：正向要求解析结果（{"quoted", "keywords", "removals"}）——
    removals 是用户**明确要求去掉**的参考图部分，除此之外参考图观察为主。
    """
    payload = {"visual_card": card,
               "user_intent": intent or "（未额外说明，依据视觉卡自动推断用途）"}
    if (style_prompt or "").strip():
        # ★ 用户贴的生图 Prompt 整包（正/负向词都在）：正向词进词汇与质感，
        #   反向词（SD negative）转成 hard_forbid 候选。
        payload["user_style_prompt"] = {
            "text": style_prompt.strip()[:3000],
            "usage": ("这是用户收集的风格提示词。正向描述应体现到 segments 与材质/色彩；"
                      "负面提示词（negative prompt / 反向词）应转成 hard_forbid 条目。"
                      "segments 必须是完整直白的中文句子——严禁发明 {花括号占位符}、"
                      "严禁「段名:内容」式槽位引用、严禁出现平台名称。"),
        }
    if world:
        payload["world_kit"] = {
            "world_name": world["world_name"],
            "knowledge": world["world_kit"],
            "usage": ("用户明确要求按这个世界观生成。creative 段的形态语言、物件设计、"
                      "材质与渲染表现必须以 knowledge 的世界观知识为准；参考图观察结果"
                      "与世界观知识冲突时（如世界观是体素方块而图里是光滑曲面），"
                      "以世界观知识为主，参考图只提供场景构图与氛围。"
                      "不要引入世界观之外的任何参照 IP。"),
        }
    if positive and (positive.get("keywords") or positive.get("removals")
                     or positive.get("preserves")):
        payload["user_requests"] = {
            "required_elements": [str(k) for k in (positive.get("keywords") or [])][:12],
            "preserve_regions": [
                {"name": p["name"], "position": p["region"],
                 "must_keep": p["content"]}
                for p in (positive.get("preserves") or [])][:4],
            "remove_from_reference": [str(r) for r in (positive.get("removals") or [])][:6],
            "usage": ("★ 保留与创作以参考图观察为准 —— 除非 remove_from_reference "
                      "明确列出了要忽略的部分，才允许忽略参考图对应部分并写进禁止项；"
                      "没有列出的部分一律按参考图保留。required_elements 是用户"
                      "要求出现的元素，绝不许写成禁止项。"
                      "★ preserve_regions 是最高优先级：用户指定了「哪个位置保留什么」，"
                      "creative/preserve 必须逐条写明该区域的**位置与内容严格复现**"
                      "（如「画面顶部居中：YOU DIED! 界面布局原样保留」），"
                      "不得挪动、缩放或改写 —— 这对应造梦师的 spatial cue 保留，"
                      "冲突时压过其它一切美学判断。"),
        }
    if base:
        payload["reference_structure"] = {
            "layout": base.get("layout"),
            "forbid_scope": base.get("forbid_scope"),
            "params": list((base.get("params") or {}).keys()),
        }
    raw = chat([{"role": "system", "content": _COMPILE_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
               temperature=0.55, max_tokens=2600, premium=True,
               response_format={"type": "json_object"},
               # ★ 阶段超时（2026-10-07）：此前编译无时限，上游卡住就是无限等。
               #   实测编译 45s，给到 180s（4 倍余量）只为防上游异常，
               #   **不是为了赶时间** —— 超时的后果是"这次提炼失败"，
               #   不会降级出低质量草稿（QC 仍然是唯一的质量门）。
               timeout=FORGE_COMPILE_TIMEOUT_SEC)
    return extract_json(_strip_fence(raw)) or {}


# ══════════════════ ③' 世界观意图识别 ═══════════════════

_WORLD_SYSTEM = _load_prompt("forge_world.md")   # 外置资产：templates/prompts/forge_world.md


def _light_json(system: str, text: str, max_tokens: int = 900,
                timeout: float | None = None) -> dict:
    """轻量结构化意图调用（低温度 + JSON 模式）。任何异常返回 {} —— 意图层
    的小调用绝不允许阻塞提炼主链。_detect_world / _parse_positive 共用。

    ★ 提速专项（10-04）premium=True→False：意图解析是关键词识别级任务，
      常规模型完全够用（世界观识别还有代码层幻觉校验兜底），没必要占用
      premium 通道 —— 常规模型快一倍以上，两连 15–20s 压到 5–10s。
    """
    try:
        raw = chat([{"role": "system", "content": system},
                    {"role": "user", "content": text[:4000]}],
                   temperature=0.1, max_tokens=max_tokens,
                   response_format={"type": "json_object"},
                   timeout=timeout)
        return extract_json(_strip_fence(raw)) or {}
    except LLMError as e:
        logger.warning("轻量意图调用失败（按未命中处理）：%s", e)
        return {}
    except Exception as e:                       # 任何意外都不阻塞提炼
        logger.warning("轻量意图调用异常（按未命中处理）：%s", e)
        return {}


def _detect_world(intent_text: str) -> dict:
    """识别用户是否明确指定参照世界观/IP（如「按照我的世界」）。

    ★ 防编造契约（三层）：
      1. 提示词铁律：用户没有明确指定参照物 → has_world=false；
      2. 幻觉校验：world_name 的任何写法都不在用户原文里 → 丢弃；
      3. 容错：任何异常 → 返回 {}，绝不阻塞提炼。
    命中时返回 {"world_name": ..., "world_kit": {...}}；未命中/失败一律 {}。
    """
    text = (intent_text or "").strip()
    if len(text) < 2:
        return {}
    got = _light_json(_WORLD_SYSTEM, text, max_tokens=1100)
    if got.get("has_world") is not True or not str(got.get("world_name") or "").strip():
        return {}
    name = str(got["world_name"]).strip()
    kit = got.get("world_kit")
    if not isinstance(kit, dict) or not kit:
        return {}
    # ★ 幻觉校验：world_name 的候选写法（按括号/斜杠/空格拆，如
    #   「Minecraft(我的世界)」→ minecraft / 我的世界）必须有一个真的出现在
    #   用户原文里，否则视为模型编造。
    lowered = text.lower()
    candidates = [c.strip().lower() for c in re.split(r"[（）()/\s]+", name) if c.strip()]
    if not any(c and c in lowered for c in candidates):
        logger.warning("世界观识别疑似幻觉（%s 不在用户原文），丢弃", name[:20])
        return {}
    kit = {k: v for k, v in kit.items() if isinstance(v, list) and v}
    if not kit:
        return {}
    logger.info("世界观意图命中：%s（%d 组知识）", name, len(kit))
    return {"world_name": name, "world_kit": kit}


# ══════════════════ ③'' 正向要求解析（语义级）══════════════════

_POSITIVE_SYSTEM = _load_prompt("forge_positive.md")   # 外置资产：templates/prompts/forge_positive.md

# 泛词黑名单：这类词做豁免匹配会误杀通用禁止项（如「不得出现无关文字」）
_POSITIVE_STOPWORDS = {"文字", "元素", "内容", "东西", "图案", "样式", "部分", "画面"}


def _parse_positive_requirements(style_prompt: str, user_notes: str = "") -> dict:
    """解析用户的正向要求（要求出现的元素 / 要求保留的区域 / 要求去掉的部分）。

    返回 {"quoted", "keywords", "removals", "preserves"}。
    preserves 是区域级保留指令（[{name, region, content, keywords}]）——
    用户说「按照理想图保留顶部的界面布局」时，位置与内容必须严格复现。
    LLM 提取失败时退回引号正则兜底 —— 精确短语保护永不失效。
    """
    text = " ".join(x for x in [(style_prompt or "").strip(), (user_notes or "").strip()] if x)
    quoted = [q for q in re.findall(r"[“\"“”']([^“\"“”']{2,40})[”\"“”']", text) if q.strip()]
    preq: dict = {"quoted": quoted, "keywords": [], "removals": [], "preserves": []}
    if len(text) < 2:
        return preq

    got = _light_json(_POSITIVE_SYSTEM, text, max_tokens=900)
    # required：元素 keywords 收集（泛词过滤 + 去重）
    kws: list[str] = []
    for item in (got.get("required") or []):
        if not isinstance(item, dict):
            continue
        for k in (item.get("keywords") or []):
            k = str(k).strip()
            if len(k) >= 2 and k not in _POSITIVE_STOPWORDS and k not in kws:
                kws.append(k)
    # removals：用户明确要求去掉的参考图部分
    removals = [str(r).strip()[:60] for r in (got.get("removals") or [])
                if str(r).strip()]
    # preserves：区域级保留（位置 + 内容 + 关键词），keywords 并入豁免集
    preserves: list[dict] = []
    for item in (got.get("preserves") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:30]
        region = str(item.get("region") or "").strip()[:40]
        content = str(item.get("content") or "").strip()[:200]
        if not (name or content):
            continue
        pkws = [str(k).strip() for k in (item.get("keywords") or [])
                if len(str(k).strip()) >= 2 and str(k).strip() not in _POSITIVE_STOPWORDS]
        preserves.append({"name": name or content[:12], "region": region,
                          "content": content, "keywords": pkws[:4]})
        for k in pkws:
            if k not in kws:
                kws.append(k)
    preq["keywords"] = kws[:12]
    preq["removals"] = removals[:6]
    preq["preserves"] = preserves[:4]
    if kws or removals or preserves:
        logger.info("正向要求解析：语义元素 %d 组，区域保留 %d 项，去除要求 %d 条",
                    len(kws), len(preserves), len(removals))
    return preq
# ════════════ ⑥ spec 守卫（第五刀外置 → services/forge_spec_guard.py）════════════
# ★ god module 拆分第五刀：归一化 / 正向保护 / 残留拦截 / 质检这一族搬走了
#   （共同点：不调模型、不碰图片，只依赖 family_renderer —— 依赖纪律见新模块头注）。
#   下面 re-export 保持旧入口：tests/test_forge.py 等仍从 style_forge 导入这些名字。
from services.forge_spec_guard import (                             # noqa: E402,F401
    _check,
    _derive_rule_meta,
    _enforce_residue,
    _norm_segments,
    _normalize,
    _pos_conflict,
    _protect_positive_requirements,
    _resolve_unknown_slots,
)


_CANCELLED = {"ok": False, "cancelled": True, "error": "已按你的要求中止"}


def forge(
    theory: str,
    image_paths: list[str] | None = None,
    user_notes: str = "",
    base_family: dict | None = None,
    use_vlm: bool = True,
    progress: "callable | None" = None,
    style_prompt: str = "",
    cancelled: "callable | None" = None,
) -> dict:
    """主入口：多图（+可选理论 +可选风格提示词）→ 视觉卡 → 家族模板

    progress：可选回调 progress(阶段文案)，供后台任务向 UI 报告进度；
    回调自身的异常一律吞掉（进度上报绝不能弄死提炼）。
    style_prompt：用户收集的生图提示词整包（正/负向词）—— 第三输入源，
    在解构与编译两个阶段都会注入。
    cancelled：可选的中止探测函数（如 threading.Event().is_set）—— 在每个
    耗时阶段之间检查，命中即返回 {"ok": False, "cancelled": True}；正在
    进行中的那一次模型调用无法被硬中断，但绝不会开始下一次。永不抛业务异常。
    """

    def _report(msg: str):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    def _stop() -> bool:
        try:
            if cancelled and cancelled():
                return True
        except Exception:
            pass
        # ★ 整条链路硬闸（2026-10-07）：到点就当"中止"处理。
        #   为什么接在 _stop() 而不是各阶段各判一次：_stop() 是全流程**唯一**
        #   的中止探针（解构前后、合成前、编译前、自修轮都会问），
        #   接在这里 = 一处改动覆盖所有阶段，不会漏。
        if _deadline is not None and time.time() > _deadline:
            _budget_hit = True
            logger.warning("提炼超过总预算 %.0fs，提前收尾（已产出的草稿会保留）",
                           FORGE_TOTAL_BUDGET_SEC)
            return True
        return False

    # 预算闸在started 赋值后再计算，所以先声明
    _deadline: float | None = None
    _budget_hit = False

    paths = [p for p in (image_paths or []) if p and os.path.exists(p)]
    if not paths and not (theory or "").strip() and not (style_prompt or "").strip():
        return {"ok": False, "error": "至少给一张参考图，或写一段风格理论 / 风格提示词。"}
    if _stop():
        return dict(_CANCELLED)

    started = time.time()
    if FORGE_TOTAL_BUDGET_SEC > 0:
        _deadline = started + FORGE_TOTAL_BUDGET_SEC

    # ★ 证据一律走本地客观测量（色板/明度/朝向，便宜且真实）。
    #   看图只发生在下面的解构阶段 —— 之前两张阶段各看一次，配了视觉模型后等于每张图看两遍。
    _report(f"分析 {len(paths)} 张参考图")
    evidence, warnings = _reference_evidence(paths, False)

    # ① 解构（并行 —— 等模型返回是 IO 密集，串行会让 6 张图变成好几分钟）
    roles = _assign_roles(paths) if paths else []
    role_by_i = {int(r.get("i", -1)): r for r in roles if isinstance(r, dict)}
    targets = list(enumerate(paths[:MAX_IMAGES]))
    # 证据按文件名对齐（审查 P2-3）：_reference_evidence 会跳过解析失败的图，
    # 按位置索引会把 A 图的测量塞给 B 图的解构。
    ev_by_file = {e["file"]: e for e in evidence if e.get("file")}
    decodes: list[dict] = []
    if targets:
        workers = max(1, min(FORGE_DECODE_WORKERS, len(targets)))
        _report(f"逐图解构（{len(targets)} 张，{workers} 并发）")
        with step("逐图解构", images=len(targets), workers=workers):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(
                    lambda it: _decode_single(
                        it[1], role_by_i.get(it[0]),
                        ev_by_file.get(os.path.basename(it[1]), {}), use_vlm,
                        style_prompt, cancelled=cancelled),
                    targets))
        decodes = results
    decode_sec = round(time.time() - started, 2)
    if _stop():
        return dict(_CANCELLED)

    # ② 合成视觉卡（纯代码合并 + 跨图共识投票，不调模型）
    #   ★ 编译合并（2026-10-04）：原「≥3 图时再加一次 LLM 跨图共识」已砍——
    #   _synthesize 的 shared_grammar 频次投票本来就是默认共识路径，LLM 版
    #   只是锦上添花却是全链路唯一可省的调用（编译合并后 6 图省 1 次调用
    #   + 15–60s 墙钟）。共识表述的连贯性由编译端五段式重组弥补。
    _report("合成视觉卡")
    card = _synthesize(decodes, theory, evidence) if (decodes or (theory or "").strip()) else {}
    if _stop():
        return dict(_CANCELLED)

    # ③ 编译家族（intent 三源合一：风格理论 + 用户提示词 + 补充说明）
    intent = " ".join(x for x in [(theory or "").strip(), (style_prompt or "").strip(),
                                  (user_notes or "").strip()] if x)
    if _stop():
        return dict(_CANCELLED)

    # ③' 轻量意图解析（并行两连）：世界观参照 + 正向要求（要求出现/要求去掉）。
    #   都是低温度小调用，失败各自静默降级为空 —— 不阻塞主链。
    _report("识别用户意图")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fw = pool.submit(_detect_world, intent)
        fp = pool.submit(_parse_positive_requirements, style_prompt, user_notes)
        world = fw.result()
        preq = fp.result()
    if world:
        _report(f"按「{world['world_name']}」世界观编译")
    intent_calls = (1 if world else 0) + (1 if (preq.get("keywords") or preq.get("removals")
                                                or preq.get("preserves")) else 0)

    doc: dict = {}
    try:
        _report("编译家族模板")
        with step("编译家族模板", images=len(paths), has_card=bool(card)):
            doc = _compile(card, intent, base_family, style_prompt,
                           world=world or None, positive=preq or None)
    except LLMError as e:
        return {"ok": False, "error": f"模型调用失败：{e}",
                "evidence": evidence, "card": card}

    if not isinstance(doc, dict) or not doc:
        return {"ok": False, "error": "模型没有返回可解析的家族 JSON",
                "evidence": evidence, "card": card}
    # ★ 归一化不得让整条流水线归零（2026-10-07 端到端实测）：
    #   _normalize 里任何一处对模型输出形状的假设不成立，都会以
    #   AttributeError/TypeError 的形式冒到顶，**把已经花掉的 170 秒
    #   与几十次调用全部作废**，而用户看到的只是"提炼失败"。
    #   归一化的目的是"把脏形状擦干净"，它自己必须是**永不抛**的那一层。
    try:
        doc = _normalize(doc)
    except Exception as e:                                    # noqa: BLE001
        logger.error("家族模板归一化失败（模型输出形状异常）：%s: %s",
                     type(e).__name__, e)
        return {"ok": False,
                "error": "模型返回的家族模板结构异常，没能整理成可用参数。"
                          "可以点「重试」再试一次，或把风格描述写得更具体些。",
                "evidence": evidence, "card": card}

    # ★ 出口收敛（10-03 复发修复；10-03 晚升级语义豁免 + 出厂检验）：所有分支
    #   的校验前必须走同一条「正向要求保护 → 残留拦截 → 校验 → QC」流水线。
    #   preq 同时携带引号短语（字面精确）与语义关键词（准星/按钮等间接指代），
    #   保护与残留兜底共用 _pos_conflict 判定，间接反转无从漏网。
    #   QC（family_qc）是最后一道语义闸：画幅/正向短语存活/forbid↔creative 矛盾。
    def _finalize(d: dict) -> tuple[dict, list[str], dict]:
        # ★ card_all_rules 代码合成（提速专项 10-04）：它只是视觉卡规则的
        #   逐字存档（revise 时给模型参考用，不进提示词）——让模型抄一遍纯属
        #   浪费 300–500 输出 token（编译大头 ≈ 数十秒）。由代码从视觉卡直接搬。
        if card and not d.get("card_all_rules"):
            d["card_all_rules"] = list(card.get("core_rules") or [])
        # ★ 媒介 / 残留 / 漂移维度同样由代码存档（10-05 补）
        #
        #   这三样在视觉卡里都有，但编译产物家族的 JSON 输出字段清单里没有它们，
        #   于是「编译完就丢了」—— 后果是 preflight 的三项漂移检查（媒介漂移、
        #   残留泄漏、易跑偏维度）**根本没数据可查**，永远不报。
        #
        #   造梦师把「主媒介」放在 Priority Gate 第 3 位，媒介身份是"像不像"的
        #   一票否决项；来源残留是它 preflight 的第 3 项检查。所以这里必须存档。
        #   与 card_all_rules 同理：让模型抄一遍纯属浪费输出 token，代码直接搬。
        if card:
            if not d.get("card_medium") and card.get("medium"):
                d["card_medium"] = card["medium"]
            if not d.get("card_source_residue") and card.get("source_residue"):
                d["card_source_residue"] = list(card["source_residue"])
            if not d.get("card_drift_warnings") and card.get("drift_warnings"):
                d["card_drift_warnings"] = list(card["drift_warnings"])
            # ★ 规则元数据：让「按相关性选 Active Core Rules」真正生效。
            #   没有它，family_renderer 的门控会回落成位置截取（第 6-8 条规则永远进不了提示词）。
            if not d.get("card_rule_meta"):
                derived = _derive_rule_meta(card)
                if derived:
                    d["card_rule_meta"] = derived
        d, _notes = _protect_positive_requirements(d, preq)
        for n in _notes:
            logger.info("正向要求保护：%s", n)
        d = _enforce_residue(d, card or {}, preq)
        errors, report = _check(d, card or {})
        if not errors:
            try:
                from services.family_qc import qc_family
                d, qc_blockers, qc_warnings, qc_fixes = qc_family(d, preq, card)
                if qc_fixes:
                    errors, report = _check(d, card or {})   # QC 修过再验一遍
                errors = list(errors) + list(qc_blockers)
                if qc_warnings:
                    report.setdefault("warnings", []).extend(qc_warnings)
                if qc_fixes:
                    report["qc_fixed"] = qc_fixes
            except Exception as e:                # QC 自身故障不能弄死提炼
                logger.warning("出厂检验异常（跳过）：%s", e)
        return d, errors, report

    doc, errors, report = _finalize(doc)

    # ★ 悬空占位符先由代码消解（省一轮模型调用，也避开中转站 502）
    if errors and any("未解析占位符" in e for e in errors):
        doc, fixed = _resolve_unknown_slots(doc, card or {})
        if fixed:
            logger.info("代码消解了 %d 个模型自造的占位符，重跑校验", fixed)
            doc, errors, report = _finalize(doc)

    # 校验不过 → 外科自修（造梦师 Surgical Repair 契约的机制重写，不搬其文本）：
    #   最多 3 个主要失败 → 最小改动修复 → 其余逐字保留 → 绝不重跑整个创作流程。
    #   ★ 旧版第一条消息带着含 style_prompt 的完整 intent —— 用户提示词包里
    #     「标签:内容」的格式在自修轮继续诱导模型犯同样的错（实测 22:32 自修无效的根因）。
    #     外科模式下模型只面对「上一版产物 + 错误清单」，诱导源被移出上下文。
    rounds = 0
    while errors and rounds < MAX_REPAIR and not _stop():
        rounds += 1
        _report(f"校验自修第 {rounds} 轮")
        logger.info("家族校验未通过，第 %d 次外科自修：%s", rounds, errors[:2])
        try:
            repair = chat([
                {"role": "system", "content": _COMPILE_SYSTEM},
                {"role": "user", "content": json.dumps(
                    {"visual_card": card}, ensure_ascii=False)},
                {"role": "assistant", "content": json.dumps(doc, ensure_ascii=False)},
                {"role": "user", "content": _REPAIR_TMPL.format(
                    errors="\n".join(f"- {e}" for e in errors[:3]))},
            ], temperature=0.2, max_tokens=2600, premium=True,
            timeout=FORGE_REPAIR_TIMEOUT_SEC,
               response_format={"type": "json_object"})
            fixed = extract_json(_strip_fence(repair))
            if isinstance(fixed, dict) and fixed:
                doc = _normalize(fixed)
                doc, errors, report = _finalize(doc)   # 自修产物同样过保护+残留+校验+QC
        except LLMError as e:
            logger.warning("自修轮 %d 调用失败：%s", rounds, e)
            break

    # ★ 预算到点 vs 用户主动中止，要区别对待（2026-10-07）
    #   用户主动中止 → 草稿他不要了，返回取消是对的。
    #   **预算到点** → 草稿已经编译好了（可能还过了 QC），
    #     这时候把它扔掉、让用户重跑 3 分钟，是最糟的处理。
    #     所以：预算到点且手上已有可用 doc，就**继续往下走**，
    #     只把"跳过了自修轮"记进 warnings，如实告诉用户。
    if _stop() and not _budget_hit:
        return {**dict(_CANCELLED), "card": card}
    if _budget_hit:
        msg = (f"提炼用了 {round(time.time() - started)} 秒，已超过本次的时间预算，"
               "就到这里收尾（可以点「继续迭代」接着改）")
        logger.info(msg)
        warnings.append(msg)

    elapsed = round(time.time() - started, 2)
    # 分段耗时：用户问"为什么提炼好几分钟"时，能直接看出慢在解构还是编译。
    report["elapsed_sec"] = elapsed
    report["decode_sec"] = decode_sec
    report["compile_sec"] = round(elapsed - decode_sec, 2)
    report["llm_calls"] = 1 + len(decodes) + intent_calls + 1 + rounds
    # 角色 + 逐图解构 + 意图层（世界观/正向要求，命中才计） + 编译 + 自修
    # （编译合并 2026-10-04：跨图共识 LLM 调用已砍，共识由 _synthesize 代码投票承担）
    logger.info("模板提炼完成：%.1fs（解构 %.1fs / 编译 %.1fs，%d 次调用）",
                elapsed, decode_sec, elapsed - decode_sec, report["llm_calls"])

    return {
        "ok": not errors,
        "spec": doc,
        "card": card,                 # 视觉卡：可复用资产，解构一次反复编译
        "decodes": [{k: v for k, v in d.items() if not k.startswith("_")} for d in decodes],
        "evidence": evidence,
        "errors": errors,
        "report": report,
        "warnings": warnings,
        "repair_rounds": rounds,
    }


def revise(spec: dict, feedback: str, theory: str = "",
           image_paths: list[str] | None = None,
           card: dict | None = None,
           use_vlm: bool = False) -> dict:
    """在既有模板上按反馈改一版

    ★ 相对旧版的关键改进：可以带着「视觉卡」一起改 ——
      如果反馈指向风格层面（"颜色再淡一点""不要那么商业"），
      应该先改视觉卡再重新编译，而不是在已经写坏的三段式上打补丁。
    """
    if not feedback or not feedback.strip():
        return {"ok": False, "error": "没有给出修改意见"}

    paths = [p for p in (image_paths or []) if p and os.path.exists(p)]
    payload: dict = {"current_family": spec, "feedback": feedback.strip()}
    if card:
        payload["visual_card"] = card
    if paths:
        ev, _ = _reference_evidence(paths, use_vlm)
        payload["reference_evidence"] = ev
    if (theory or "").strip():
        payload["style_theory"] = theory.strip()

    # 意图层同样作用于迭代：反馈里明确指定参照 IP/游戏、要求出现/去掉某些
    # 参考图部分时命中（两次轻量调用并行，失败静默降级）。
    with ThreadPoolExecutor(max_workers=2) as pool:
        fw = pool.submit(_detect_world, " ".join([feedback, theory or ""]))
        fp = pool.submit(_parse_positive_requirements, feedback, "")
        world = fw.result()
        preq = fp.result()
    if world:
        payload["world_kit"] = {
            "world_name": world["world_name"],
            "knowledge": world["world_kit"],
            "usage": ("用户在反馈中明确要求按这个世界观生成。segments 的形态语言、"
                      "物件设计、材质与渲染表现必须以 knowledge 为准；与参考图冲突时"
                      "以世界观知识为主。不要引入世界观之外的任何参照 IP。"),
        }
    if preq and (preq.get("keywords") or preq.get("removals")
                 or preq.get("preserves")):
        payload["user_requests"] = {
            "required_elements": [str(k) for k in (preq.get("keywords") or [])][:12],
            "preserve_regions": [
                {"name": p["name"], "position": p["region"],
                 "must_keep": p["content"]}
                for p in (preq.get("preserves") or [])][:4],
            "remove_from_reference": [str(r) for r in (preq.get("removals") or [])][:6],
            "usage": ("★ 保留与创作以参考图观察为准 —— 除非 remove_from_reference "
                      "明确列出，才允许忽略参考图对应部分并写进禁止项；"
                      "required_elements 是用户要求出现的元素，绝不许写成禁止项。"
                      "★ preserve_regions 是最高优先级：该区域的位置与内容必须"
                      "严格复现，不得挪动、缩放或改写。"),
        }

    system = (
        "你是提示词模板工程师。用户对自己已有的家族模板提出了修改意见。\n"
        "如果意见指向**视觉风格层面**（色彩、光影、材质、媒介、构图倾向），"
        "请先更新 visual_card 的对应字段与 core_rules，再据此重写 segments；"
        "如果是结构层面（参数、画幅、禁止项），直接改家族字段即可。\n"
        "保持 id 不变。输出同样的完整 JSON 结构（含 card_all_rules）。"
    )
    try:
        raw = chat([{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                   temperature=0.45, max_tokens=2600, premium=True,
                   response_format={"type": "json_object"})
        doc = extract_json(_strip_fence(raw))
    except LLMError as e:
        return {"ok": False, "error": f"模型调用失败：{e}"}

    if not isinstance(doc, dict) or not doc:
        return {"ok": False, "error": "模型没有返回可解析的 JSON"}
    doc = _normalize(doc)
    if spec.get("id"):                 # 迭代不该换个家族
        doc["id"] = spec["id"]
    doc = _enforce_residue(doc, card or {}, preq)
    errors, report = _check(doc, card or {})
    if not errors:
        try:
            from services.family_qc import qc_family
            doc, qc_blockers, qc_warnings, qc_fixes = qc_family(doc, preq, card)
            if qc_fixes:
                errors, report = _check(doc, card or {})
            errors = list(errors) + list(qc_blockers)
            if qc_warnings:
                report.setdefault("warnings", []).extend(qc_warnings)
            if qc_fixes:
                report["qc_fixed"] = qc_fixes
        except Exception as e:
            logger.warning("出厂检验异常（跳过）：%s", e)
    return {"ok": not errors, "spec": doc, "errors": errors, "report": report}
