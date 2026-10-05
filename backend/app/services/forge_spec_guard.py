"""服务层 · 工坊 spec 守卫 —— 归一化 / 正向保护 / 残留拦截 / 质检

════════ 第五刀（god module 拆分）══════════════════

这一族函数全是「拿到 spec 之后再加工」：

    _normalize                    模型输出 → 能落盘的家族结构
    _protect_positive_requirements 用户正向要求不许被 forbid 反转
    _norm_segments                三段式缺失补齐
    _resolve_unknown_slots        未知槽位（{xx}）用视觉卡兜底解析
    _enforce_residue              来源残留 → 禁令（含流程备注过滤）
    _check                        校验 + 渲染冒烟
    _pos_conflict                 forbid 条目 vs 用户正向要求的统一判定

共同点：**不调模型、不碰图片**，只依赖 family_renderer 的校验与渲染
（依赖纪律：本模块只准依赖 family_renderer / rules_engine，不得反向
依赖 style_forge —— 否则成环）。搬走后 style_forge 只剩
「解构 → 合成 → 编译 → 编排」的主干。

★ 边界由 AST 静态分析确定（第二刀的事故教训固化为流程）：
  这一族互相引用 _norm_segments/_pos_conflict，对外只被 forge()/revise() 调用。
  测试与工坊的旧入口不变 —— style_forge 仍然 re-export 这些名字。
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.logging import logger                                       # noqa: E402
from services.family_renderer import (                                 # noqa: E402
    FamilyRenderError,
    render_family,
    validate_family,
)




def _pos_conflict(line: str, preq: dict | None) -> bool:
    """统一判定：禁止条目是否与用户正向要求冲突（字面短语 + 语义关键词）。"""
    if not preq:
        return False
    low = str(line).lower()
    if any(q and q.lower() in low for q in (preq.get("quoted") or [])):
        return True
    return any(k and k in low for k in (preq.get("keywords") or []))


def _normalize(doc: Any) -> dict:
    """把模型输出规整成能落盘的家族结构（保留旧行为以兼容既有测试）"""
    if not isinstance(doc, dict):
        raise ValueError("模型输出不是 JSON 对象")

    out = dict(doc)
    out.setdefault("kind", "family")
    # ★ 画幅兜底（实测 2026-10-03：提炼产物漏 default_aspect → 生成端拿不到
    #   画幅 → 正方形原图被 gpt-image 默认竖版强改成 9:16）。缺省 = 跟随原图，
    #   对「原图覆写」类家族（本工坊的主流场景）永远是最安全的选择。
    da = str(out.get("default_aspect") or "").strip()
    out["default_aspect"] = da if da else "origin"

    fid = str(out.get("id") or "").strip()
    if not fid:
        fid = "forged_" + uuid.uuid4().hex[:8]
    fid = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in fid).lower()
    out["id"] = fid

    for key in ("params", "segments", "hard_forbid", "suitable", "allow_change"):
        if key in out and not isinstance(out[key], (dict, list)):
            out.pop(key)

    params = out.get("params") or {}
    # ★ segments 双花括号归一（实测 2026-10-01）：模型会在 YAML 语境"多写一层括号"
    #   （{{subject.name}}），渲染器宽容后仍统一转成单括号，保持入库产物干净。
    segs_out = out.get("segments")
    if isinstance(segs_out, dict):
        for sk, sv in list(segs_out.items()):
            if isinstance(sv, str) and "{{" in sv:
                segs_out[sk] = re.sub(r"\{\{([\w.]+)(?::[^{}]*)?\}\}", r"{\1}", sv)
    fixed: dict = {}
    for name, spec in params.items():
        if not isinstance(spec, dict):
            continue
        opts = spec.get("options")
        if opts and not isinstance(opts, list):
            spec.pop("options")
        # ★ 参数名必须是英文小写下划线（它是程序里的标识符）。
        #   模型常给「套色方案」这种中文键名，原名保留在 label 里。
        key = str(name)
        slug = "".join(
            ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in key
        ).strip("_").lower()
        if not slug or slug.strip("_") == "":
            slug = f"param_{len(fixed) + 1}"
        spec.setdefault("label", key)
        base_slug = slug
        i = 2
        while slug in fixed:
            slug = f"{base_slug}_{i}"
            i += 1
        # ★ option_labels 形状归一：模型常把它写成与 options 等长的数组
        #   （实测 voxel 风格），渲染器要求 dict —— 按位置配对转 dict。
        ol = spec.get("option_labels")
        opts = spec.get("options")
        if isinstance(ol, list) and isinstance(opts, list):
            spec["option_labels"] = dict(zip([str(o) for o in opts],
                                             [str(x) for x in ol]))
        elif ol is not None and not isinstance(ol, dict):
            spec.pop("option_labels", None)
        fixed[slug] = spec
    out["params"] = fixed
    return out


def _protect_positive_requirements(spec: dict, preq: dict | None) -> tuple[dict, list[str]]:
    """★ 正向要求保护（实测 2026-10-02 ground_pixel_overlay 教训；
    10-03 复发加固；10-03 晚升级语义级豁免）：

    用户提示词里**明确要求出现**的文字/界面（如 "YOU DIED!"、准星、复古像素按钮）
    被提炼器反转成了禁止项 —— 写进 hard_forbid 和 forbid 段，方向完全反了。

    复发与漏网史：
      - 直接反转（条目含引号短语字样）→ 引号匹配可清；
      - 残留兜底在保护之后追加 → 统一 _finalize 管线后修复；
      - **间接反转**（「十字准星样式」「灰色按钮样式」不含引号短语字样，
        但语义上就是用户要求出现的元素）→ 引号匹配抓不到，
        本版改由 _parse_positive_requirements 提供**语义关键词**，
        hard_forbid / forbid / 残留追加三处统一用 _pos_conflict 判定。

    这里做三件事：
      1. 从 hard_forbid / segments.forbid 里移除与正向要求冲突的条目
      2. 在 forbid 段补一条豁免：「除用户要求的元素外，不得出现其它无关文字」
      3. creative 段缺失正向元素时补写
    """
    quoted = [q for q in (preq or {}).get("quoted") or [] if q]
    keywords = [k for k in (preq or {}).get("keywords") or [] if k]
    if not quoted and not keywords:
        return spec, []
    notes: list[str] = []

    def _conflict(line: str) -> bool:
        return _pos_conflict(line, preq)

    # 1) hard_forbid：移除与正向要求冲突的条目（保底数量由调用方校验兜住）
    hf = [str(x) for x in (spec.get("hard_forbid") or [])]
    kept = []
    for line in hf:
        if _conflict(line):
            notes.append(f"hard_forbid 与用户正向要求冲突，已移除：{line[:40]}")
        else:
            kept.append(line)
    # 移除后不足 4 条会过不了校验 —— 用不含冲突文字的通用保真条款补足
    generic = [
        "重绘或改变原照片核心主体的身份、结构与真实质感",
        "改变原照片的构图、透视关系与地平线高度",
        "额外添加与画面无关的水印、乱码或模糊低质量纹理",
        "商业广告感、廉价 AI 感与过度修饰",
    ]
    i = 0
    while len(kept) < 4 and i < len(generic):
        if not any(generic[i][:10] in x for x in kept):
            kept.append(generic[i])
        i += 1
    spec["hard_forbid"] = kept

    # 2) forbid 段：逐行移除冲突条目 + 补豁免说明
    segs = _norm_segments(spec)
    fb = segs.get("forbid")
    if isinstance(fb, str) and any(_conflict(l) for l in fb.splitlines()):
        lines = [l for l in fb.splitlines() if not _conflict(l)]
        lines.append("除用户明确要求出现的标题、界面元素与文字外，不得出现其它无关文字、乱码或水印。")
        segs["forbid"] = "\n".join(lines).strip()
        notes.append("forbid 段已移除与正向要求冲突的条目，并补豁免说明")
        spec["segments"] = segs

    # 3) creative 段：确保用户的正向要求在描述里（缺失则追加）
    cr = segs.get("creative")
    if isinstance(cr, str) and quoted and not any(q in cr for q in quoted):
        segs["creative"] = cr.rstrip() + "\n按用户要求呈现这些元素：" + \
            "、".join(f"“{q}”" for q in quoted[:6]) + "。"
        spec["segments"] = segs
        notes.append("creative 段已补上用户要求出现的元素")
    return spec, notes


def _norm_segments(doc_or_spec: dict) -> dict:
    """segments 形状归一（审查 P2-4 的通用化）：模型可能给 str/list/缺键，
    统一返回标准 dict（缺失三段补空串）。调用方拿到的永远是可安全读写的 dict。
    """
    raw = (doc_or_spec or {}).get("segments")
    segs = dict(raw) if isinstance(raw, dict) else {}
    for k in ("preserve", "creative", "forbid"):
        segs.setdefault(k, "")
    return segs


def _resolve_unknown_slots(doc: dict, card: dict) -> tuple[dict, int]:
    """把模型自造的中文占位符**就地消解**，不再为它多花一轮模型调用。

    实测教训：编译出来的 spec 里出现过 `{画面核心主体}` 这种模型自己发明的槽位 ——
    渲染时判为悬空 → 触发自修轮 → 中转站 502 → 整次提炼以 ok=False 收场。
    为一个占位符再烧一轮 LLM，既不划算又脆弱。这里代码兜底：
      - 含"主体/主角"→ 填 Scene Card 的核心主体
      - 含"锚点"    → 填视觉锚点
      - 其余        → 整句丢弃（残句比错句安全）
    """
    segs = _norm_segments(doc)
    params = set((doc.get("params") or {}).keys())
    known = set(DERIVED_KEYS) | params

    subject_name = ""
    subj = card.get("subject")
    if isinstance(subj, dict):
        subject_name = str(subj.get("name") or "")
    elif isinstance(subj, str):
        subject_name = subj
    if not subject_name:
        subject_name = str((card.get("shared_grammar") or {}).get("core_subjects") or "")
    anchors = "、".join(
        str(a.get("desc")) for a in (card.get("anchors") or [])
        if isinstance(a, dict) and a.get("desc"))

    fixed = 0
    seg_names = {"preserve", "creative", "forbid", "text", "figures", "dicts", "params"}
    known_set = known

    def _sub(m: "re.Match") -> str:
        nonlocal fixed
        ph = m.group(1).strip()
        if not ph:
            fixed += 1
            return ""
        core = ph.split(":", 1)[0].strip()
        if core in known_set or ph in known_set:
            return m.group(0)                          # 合法槽位（派生量/参数），原样保留
        if any(k in ph for k in ("主体", "主角", "subject")):
            fixed += 1
            return (subject_name or "核心主体").replace("\n", " ")
        if "锚点" in ph:
            fixed += 1
            return (anchors or "视觉锚点").replace("\n", " ")
        # ★「段名:内容」式自造占位符（实测 2026-10-01：模型模仿了用户提示词包里
        #   「标签:内容」的格式，写出 {creative:以中央林荫道路…} 这种东西）：
        #   有实质描述就剥壳保留内容（那本来就是想放的话），纯「段名:数字」无实义则摘掉。
        if core in seg_names:
            rest = ph.split(":", 1)[1].replace("\n", " ").strip() if ":" in ph else ""
            if len(re.sub(r"[\s，。、；,.;:]+", "", rest)) >= 4:
                fixed += 1
                return rest
            fixed += 1
            return ""
        # 其余自造槽位：剥壳保留括号内内容 —— 模型写在占位符里的本来就是
        # 想放这句话的内容，剥壳比摘掉更保信息（旧版整句丢弃太粗暴）。
        cleaned = ph.replace("\n", " ").strip()
        if len(re.sub(r"[\s，。、；,.;:]+", "", cleaned)) >= 4:
            fixed += 1
            return cleaned
        fixed += 1
        return ""

    for key in ("preserve", "creative", "forbid"):
        text = segs.get(key)
        if not isinstance(text, str) or "{" not in text:
            continue
        # ★ 全文级处理（DOTALL）：旧版逐行匹配，跨行占位符（{ 在一行 } 在另一行）
        #   永远漏网 —— 实测这正是 22:32 失败的根因之一。
        new_text = re.sub(r"\{([^{}]{1,600}?)\}", _sub, text, flags=re.DOTALL)
        segs[key] = re.sub(r"[ \t]{2,}", " ", new_text).strip()
    return {**doc, "segments": segs}, fixed


# 来源残留里的「流程备注」特征：这些是解构模型的观察记录/犹豫说明，不是禁令 ——
# 机械转成「不得出现：X」后会与 creative 的正向保留直接打架（实测 2026-10-03：
# 「YOU DIED!、分数、按钮…均为画面中可见元素；是否保留应由后续任务要求决定」
# 被写成不得出现，而 creative ④ 要求保留完整死亡界面 → 生图方向随机）。
# 命中即整条丢弃 —— 备注本来就不是禁令，丢弃零风险。
_RESIDUE_NOTE_MARKS = (
    "未观察到", "不足以确认", "是否保留应由后续", "是否保留由后续",
    "均为画面中可见元素", "均为画面可见元素", "无法确认身份", "由后续任务",
    "由后续流程", "由用户后续",
)


# ★ 轴推导已下沉到 family_renderer（单一事实源，避免两份词表漂移）
#   —— style_forge 依赖 family_renderer，反向 import 会成环，所以推导逻辑
#   必须住在渲染侧；这里只保留一个薄包装供工坊编译与测试调用。
def _derive_rule_meta(card: dict) -> list[dict]:
    """从视觉卡推导规则的 axis / weight（不给模型加输出负担）"""
    from services.rules_engine import derive_rule_meta
    rules = [str(r) for r in (card.get("core_rules") or []) if str(r).strip()]
    if not rules:
        return []
    strong_text = " ".join(
        str(x) for x in ((card.get("transfer_scope") or {}).get("strong") or [])
    )
    return derive_rule_meta(rules, strong_text)


def _enforce_residue(spec: dict, card: dict, preq: dict | None = None) -> dict:
    """★ 来源残留拦截（编译后兜底）

    视觉卡的 source_residue 每一项都必须在 forbid/hard_forbid 里有对应表述
    （造梦师的铁律：Do Not Transfer 的内容泄漏 = Actual Drift）。
    模型偶尔会漏写 —— 这里做代码层兜底：缺的自动追加进 forbid 段。

    ★ preq（正向要求豁免）：参考图里如实记录的「残留」若与用户要求出现的
    元素语义重叠（引号短语或 keywords 命中，如准星、按钮），就不算待拦截残留
    —— 否则残留兜底会把用户的正向要求反转成禁止项（10-03 间接反转教训）。
    两参调用（preq 缺省 None）= 无豁免，行为与旧版一致。
    """
    residue = [x for x in (card.get("source_residue") or [])
               if isinstance(x, str) and x.strip()]
    if not residue:
        return spec
    # ★ 流程备注过滤（实测 2026-10-03）：解构模型偶尔把观察记录写进残留清单，
    #   「不得出现：XXX 未观察到」既不是禁令也不是事实指令，只会污染提示词；
    #   「是否保留应由后续决定」类条目更是会与 creative 的正向保留直接冲突。
    #   这里在代码层整条丢弃 —— prompt 约束会失效，代码约束不会。
    dropped_notes = [r for r in residue if any(m in r for m in _RESIDUE_NOTE_MARKS)]
    residue = [r for r in residue if not any(m in r for m in _RESIDUE_NOTE_MARKS)]
    if dropped_notes:
        logger.info("残留拦截：丢弃 %d 条流程备注型残留（非禁令）：%s",
                    len(dropped_notes), [d[:30] for d in dropped_notes[:3]])
    if not residue:
        return spec
    # ★ 正向要求豁免：与用户要求出现的元素语义重叠的残留 → 不拦截
    if preq:
        residue = [r for r in residue if not _pos_conflict(r, preq)]
    # ★ 形状归一（审查 P1-4 / P2-4）：segments 可能是 str/list（模型乱给），
    #   hard_forbid 元素可能非 str —— 归一后再拼，不打破「永不抛异常」契约。
    segs = _norm_segments(spec)
    forbid_seg = segs.get("forbid")
    forbid_text = forbid_seg if isinstance(forbid_seg, str) else "\n".join(
        str(x) for x in (forbid_seg or []))
    hf = spec.get("hard_forbid") or []
    forbid_text += "\n" + "\n".join(str(x) for x in hf)
    forbid_text = forbid_text.replace("{hard_forbid_joined}", "")
    forbid_lines = [ln.strip(" -•\t") for ln in forbid_text.splitlines() if ln.strip(" -•\t")]
    missing = []
    for r in residue:
        # 消毒（审查 P2-2）：残留文本若含 {xxx} 会变成未解析占位符，
        # 含换行会伪造 bullet 行 —— 先替换掉再入库。
        clean = str(r).replace("{", "（").replace("}", "）").replace("\n", " ").strip()
        key = clean[:14]                   # 前 14 字做包含判断，容忍表述差异
        if not any(key in ln or ln in clean for ln in forbid_lines):
            missing.append(clean)
    if not missing:
        return spec
    extra = "\n".join(f"- 不得出现：{m}" for m in missing)
    base_forbid = forbid_seg if isinstance(forbid_seg, str) else "\n".join(
        str(x) for x in (forbid_seg or []))
    segs["forbid"] = (base_forbid + "\n" + extra).strip()
    logger.info("残留拦截：为 forbid 补了 %d 条来源残留约束", len(missing))
    return {**spec, "segments": segs}


def _check(doc: dict, card: dict | None = None) -> tuple[list[str], dict]:
    """校验 + 渲染冒烟。返回 (错误列表, 渲染报告)

    card 必须传真实视觉卡：冒烟渲染用它填 {subject.name} 等槽位。
    之前这里传空卡 —— 于是每次都报「缺少创作卡」，而实际上主体和锚点明明有，
    这条警告是纯噪音，还让用户以为提炼失败了。
    （视觉卡的残留拦截在调用方 _enforce_residue；形容词检查在 _synthesize。）"""
    errors: list[str] = []
    try:
        validate_family(doc, source=doc.get("id", "?"))
    except FamilyRenderError as e:
        errors.append(str(e))

    # 来源残留的拦截在调用方做（_enforce_residue）——
    # 在这里改 doc 局部变量、变更会丢失（实测教训：别在检查函数里偷偷改输入）。

    report: dict = {}
    if not errors:
        try:
            r = render_family(doc, {}, card or {}, strict=False)
            report = {
                "dropped_lines": r.get("dropped_lines", 0),
                "unresolved": r.get("unresolved", []),
                "warnings": r.get("warnings", []),
                "prompt_chars": len("\n\n".join(
                    p for p in [r["segments"].get("preserve", ""),
                                r["segments"]["creative"],
                                r["segments"]["forbid"]] if p)),
            }
            if r.get("unresolved"):
                errors.append(f"存在未解析占位符：{r['unresolved']}")
        except Exception as e:
            errors.append(f"渲染失败：{type(e).__name__}: {e}")
    return errors, report

__all__ = [
    "_check",
    "_derive_rule_meta",
    "_enforce_residue",
    "_norm_segments",
    "_normalize",
    "_pos_conflict",
    "_protect_positive_requirements",
    "_resolve_unknown_slots",
]
