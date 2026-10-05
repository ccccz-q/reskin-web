"""工具层 · 实现与调度 —— contracts/tools.py 里每个 Schema 的唯一落地

════════ 设计约束 ════════

1. **工具不做权限判断**。这台农夫制的分工：
   - Schema 在 `contracts/tools.py`
   - 权限在 `governance/guard.py`
   - 实现在这里
   三处单源，避免「新增工具忘了加护栏」。

2. **工具失败返回结构化错误，不抛异常**。
   Agent 循环里，一次工具失败应该是**给模型看的一条观察结果**，
   让它有机会换个参数重试，而不是把整个 HTTP 请求炸成 500。

3. **返回值必须是 JSON 可序列化的纯数据**。
   里面有 Path / datetime / 异常对象的话，一旦要塞进 SSE 就栽了。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.tools import (                                        # noqa: E402
    CONDITIONAL_SPEND_TOOLS,
    costs_money,
    validate_call,
)
from governance.guard import (                                        # noqa: E402
    GovernanceError,
    release_generation,
    reserve_generation,
    settle_generation,
)
from infra.logging import audit, logger, step                          # noqa: E402
from services.image_generator import generate_image_with_reference     # noqa: E402
from services.card_extractor import build_card                       # noqa: E402
from services.prompt_builder import available_sources, safe_build        # noqa: E402
from services.family_renderer import load_families                      # noqa: E402
from services.template_manager import get_family_by_id, load_families as _list_families  # noqa: E402

# 单个字段回传给模型的最大字符数 —— 提示词全文不必都给模型看，给多了只是烧 token
PREVIEW_CLIP = 1200


@dataclass
class ToolContext:
    """一次工具调用所需的全部运行时信息

    刻意做成一个显式的对象，而不是散装的 keyword arguments ——
    加一个新工具要传新参数时，类型系统 remnant 会提醒我漏了哪一处。
    """
    thread_id: str = "default"
    image_path: str = ""
    card: dict = field(default_factory=dict)
    image_info: dict = field(default_factory=dict)
    allow_spend: bool = True
    # 用户自定义提示词（请求级字段，不是工具参数）。
    # ★ 为什么放 ctx 而不是让模型通过工具参数传：
    #   用户写的提示词必须**原样**进入最终 prompt。如果让模型转述，
    #   它可能改写、截断、或干脆忘掉 —— 那「自定义提示词」就成了随缘功能。
    #   放请求级后链路是 前端 → ChatRequest → ctx → build_prompt，
    #   中间没有任何一步由模型经手。
    extra_prompt: str = ""
    extra_mode: str = "append"      # append | replace
    # 工具执行过程中产生的「副作用结果」，由引擎读取
    artifacts: dict = field(default_factory=dict)
    # 因预览模式被强制降级的工具名。
    # ★ 为什么要单独记：dispatch 是把参数改掉之后再调实现的，
    #   实现内部再看 use_vlm 已经是 False，无从知道「本意是 True 但被省钱了」。
    #   工具需要据此如实告诉模型 —— 否则模型会以为拿到的是完整卡。
    downgraded: set = field(default_factory=set)


@dataclass
class ToolResult:
    """统一的工具返回包"""
    name: str
    ok: bool
    observation: dict[str, Any]
    terminal: bool = False          # True 表示主任务已完成，循环可以收尾


def _clip(text: Any, limit: int = PREVIEW_CLIP) -> str:
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit] + f"…（截断，共 {len(s)} 字符）"


def _family_doc(family_id: str) -> dict:
    """优先走 template_manager（带 kind 判别 + 家族校验），兜底走 family_renderer"""
    fam = get_family_by_id(family_id)
    if fam:
        return fam
    return (load_families() or {}).get(family_id) or {}


def _all_family_ids() -> list[str]:
    return [f.get("id") for f in _list_families()]


# ══════════════════════ 工具实现 ══════════════════════

def tool_list_families(_args: dict, ctx: ToolContext) -> dict:
    """列出所有家族 —— 零成本，模型的第一站"""
    from services.family_renderer import family_meta

    out: list[dict] = []
    for fam in _list_families():
        meta = family_meta(fam)
        out.append({
            "id": meta["id"],
            "name": meta["name"],
            "icon": meta["icon"],
            "description": meta["description"],
            "suitable": meta["suitable"],
            "default_aspect": meta["default_aspect"],
            "required_params": [p["name"] for p in meta["params"] if p["required"]],
            "param_count": len(meta["params"]),
            "kind": "family",
        })

    for src in available_sources():
        if src.get("kind") == "template":
            out.append({
                "id": src["id"],
                "name": src["name"],
                "icon": src.get("icon", "🎨"),
                "description": src.get("description", ""),
                "family_id": src.get("family_id"),
                "suitable": src.get("recommended_scenes", []),
                "required_params": [],
                "param_count": 0,
                "kind": "template",
            })

    orient = (ctx.image_info or {}).get("orientation")
    hint = {
        "portrait": "当前原图是竖构图，优先推荐竖画幅/通用画幅家族。",
        "landscape": "当前原图是横构图，优先推荐横画幅/通用画幅家族。",
        "square": "当前原图是方构图，优先 '1:1' 画幅家族。",
    }.get(orient or "", "未上传图片，无法判断朝向。")

    audit("tool_list_families", thread_id=ctx.thread_id, count=len(out))
    return {
        "families": out,
        "orientation_hint": hint,
        "tip": "下一步调用 describe_family(id) 查看某个家族的完整参数表。",
    }


def tool_describe_family(args: dict, ctx: ToolContext) -> dict:
    """查看某个家族的参数表 —— 模型取合法取值的唯一途径"""
    from services.family_renderer import family_meta, params_schema

    family_id = str(args.get("family_id") or "").strip()
    fam = _family_doc(family_id)
    if not fam:
        # 不要静默返回空 —— 把可用清单回给模型，它下一次就能调对
        return {
            "error": f"没有名为 {family_id!r} 的家族",
            "available": _all_family_ids(),
            "hint": "请使用 list_families 返回的 id，不要自己编。",
        }

    meta = family_meta(fam)
    params = params_schema(fam)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "description": meta["description"],
        "default_aspect": meta["default_aspect"],
        "forbid_scope": meta["forbid_scope"],
        "allow_change": meta["allow_change"],
        "variants": meta["variants"],
        "params": params,
        "required": [p["name"] for p in params if p["required"]],
        "hint": "params 里的 options/range 才是合法取值，缺必填项时渲染会失败。",
    }


def tool_extract_card(args: dict, ctx: ToolContext) -> dict:
    """从原图提炼创作卡 —— 「反推 forbid」的唯一数据来源

    ★ 这个工具补的是全链路最后一个缺口（审查发现 P0-1）。
      在它出现之前，card 没有任何生产者，于是渲染器恒常拿到 `{}`，
      静默产出「提炼原图中的 0 个轮廓与路径」这类指令，
      forbid 段里的 `{dynamic_forbid}` 整行被删掉。

    两档：use_vlm=false 只做本地取色（0 成本）；
         use_vlm=true（默认）额外调视觉模型识别主体与锚点。
    """
    if not ctx.image_path:
        return {
            "error": "当前会话还没有上传原图，无法提炼。",
            "hint": "请让用户先上传一张图片。",
        }

    use_vlm = args.get("use_vlm")
    if use_vlm is None:
        use_vlm = True
    use_vlm = bool(use_vlm)

    # ★ 预览模式下 dispatch 已经把 use_vlm 强制降级了（见 contracts/tools.py 的
    #   CONDITIONAL_SPEND_TOOLS）。这里要如实告诉模型，否则它会以为拿到了完整卡。
    #   注意不能靠 `use_vlm 且 not allow_spend` 判断 —— 参数在进来之前已经被改掉了，
    #   要靠 dispatch 留下的降级记录。
    downgraded = "extract_card" in ctx.downgraded

    card = build_card(ctx.image_path, use_vlm=use_vlm)
    if not card:
        return {
            "error": "提炼失败：原图无法读取或已损坏。",
            "hint": "让用户重新上传一张图片。",
        }

    ctx.card = card          # ★ 关键：写回上下文，后续 render_prompt / generate_image 都会用到
    ctx.artifacts["card"] = card
    ctx.artifacts["card_origin"] = card.get("_origin")

    has_subject = bool(
        (card.get("subject") or {}).get("name")
        if isinstance(card.get("subject"), dict) else card.get("subject")
    )
    anchors = [a.get("desc") for a in (card.get("anchors") or []) if a.get("desc")]

    if not has_subject and not anchors:
        # 本地档拿不到主体是正常的（它只测色彩），必须说清楚而不是假装成功
        why = ("当前是预览模式（allow_spend=false），视觉模型调用已被强制关闭"
               if downgraded else
               "未配置视觉模型（VISION_MODEL）或视觉识别失败")
        return {
            "ok": True,
            "origin": card.get("_origin"),
            "downgraded": downgraded,
            "palette": card.get("palette"),
            "orientation": card.get("orientation"),
            "subject": None,
            "anchors": [],
            "warning": f"只拿到色板信息：{why}，主体与锚点无法提炼。"
                       "保真仍可用，但「反推 forbid」的效力会明显弱于设计预期。",
            "hint": "可以直接继续（色板足以驱动 fidelity=heavy 类的保留约束）。",
        }

    return {
        "ok": True,
        "origin": card.get("_origin"),
        "downgraded": downgraded,
        "subject": card.get("subject"),
        "anchors": anchors,
        "palette": card.get("palette"),
        "orientation": card.get("orientation"),
        "light_hint": card.get("light_hint"),
        "risk_notes": card.get("risk_notes"),
        "hint": "卡片已就绪，后续 render_prompt / generate_image 会自动用它。",
    }


def tool_read_image_info(_args: dict, ctx: ToolContext) -> dict:
    """读原图信息 —— 决定画幅的唯一依据"""
    if not ctx.image_path:
        return {
            "error": "当前会话还没有上传原图，无法读取尺寸。",
            "hint": "请让用户先上传一张图片再决定画幅。",
        }
    info = dict(ctx.image_info or {})
    advice = {
        "portrait": "竖构图：优先 3:4 / 9:16 类竖幅家族，避开横幅。",
        "landscape": "横构图：优先 4:3 / 16:9 类横幅家族。",
        "square": "方构图：1:1 家族最合适。",
    }.get(info.get("orientation"), "无法判断朝向。")
    return {
        "filename": os.path.basename(ctx.image_path),
        **info,
        "advice": advice,
    }


def tool_render_prompt(args: dict, ctx: ToolContext) -> dict:
    """渲染提示词预览 —— 不花钱，毫秒级"""
    family_id = str(args.get("family_id") or "").strip()
    params = args.get("params") or {}

    built = safe_build(family_id, params, ctx.card,
                        extra_prompt=ctx.extra_prompt,
                        extra_mode=ctx.extra_mode)
    if not built.get("ok"):
        return {
            "error": built.get("error"),
            "missing": built.get("missing", []),
            "hint": "缺参数时调用 describe_family 看必填项，补齐后重试。",
        }

    segs = built.get("segments") or {}
    obs = {
        "family_id": family_id,
        "resolved_params": built.get("params"),
        "preserve": _clip(segs.get("preserve", "")),
        "creative": _clip(segs.get("creative", "")),
        "forbid": _clip(segs.get("forbid", "")),
        "allow_change": built.get("allow_change", []),
        "warnings": built.get("warnings", []),
        "auto_resolved": built.get("auto_resolved", []),
        "prompt_chars": len(built.get("prompt") or ""),
        "hint": "确认方向没问题再调用 generate_image（这一步会真实计费）。",
    }
    ctx.artifacts["last_prompt"] = built.get("prompt")
    ctx.artifacts["last_family"] = family_id
    ctx.artifacts["last_params"] = built.get("params")
    audit("tool_render_prompt", thread_id=ctx.thread_id, family=family_id,
          chars=obs["prompt_chars"])
    return obs


def tool_generate_image(args: dict, ctx: ToolContext) -> dict:
    """真正出图 —— 本项目唯一会花钱的动作"""
    family_id = str(args.get("family_id") or "").strip()
    params = args.get("params") or {}

    if not ctx.allow_spend:
        return {
            "error": "当前会话处于预览模式（allow_spend=false），不允许生成图片。",
            "hint": "告诉用户：预览功能都可用，正式出图需要在界面上确认。",
        }

    if not ctx.image_path:
        return {"error": "还没有上传原图，无法生成。",
                "hint": "请让用户先上传一张图片。"}

    # ★ 权限判断 + 额度占位统一在治理层 —— 工具本身不判断
    #   且必须是「预扣」而非「只看不写」：否则并发两个请求会同时通过检查。
    try:
        token = reserve_generation(ctx.thread_id, reference_image=ctx.image_path)
    except GovernanceError as e:
        audit("generate_denied", thread_id=ctx.thread_id, code=e.code, **e.extra)
        return {
            "error": str(e),
            "code": e.code,
            "hint": "这是治理策略拦截，不是程序错误，不要尝试重试。",
        }

    built = safe_build(family_id, params, ctx.card,
                        extra_prompt=ctx.extra_prompt,
                        extra_mode=ctx.extra_mode)
    if not built.get("ok"):
        # 提示词都没渲染出来，钱还没花出去 → 把预扣的额度退回去
        release_generation(ctx.thread_id, token, reason="渲染失败")
        return {
            "error": built.get("error"),
            "missing": built.get("missing", []),
            "hint": "先用 render_prompt 确认参数能通过再生成，避免白花钱。",
        }

    prompt = built["prompt"]
    family = _family_doc(built.get("family_id") or family_id)
    aspect = (family or {}).get("default_aspect")

    with step("Agent 出图", family=built.get("family_id"), thread=ctx.thread_id):
        result = generate_image_with_reference(
            reference_image_path=ctx.image_path,
            prompt=prompt,
            size=None,                 # ★ 尺寸由 resolve_size 决定，遵守「跟随原图」
            aspect=aspect,
        )

    if not result.get("success"):
        # 失败要凭预扣票据退还 —— 失败了还扣用户的额度是不讲理的
        quota = release_generation(
            ctx.thread_id, token, reason=str(result.get("error"))[:200]
        )
        return {
            "error": result.get("error"),
            "quota": quota,
            "hint": "生成失败，额度已退还。可以调整参数后再试，或换个家族。",
        }

    quota = settle_generation(
        ctx.thread_id, token, size=result.get("size", ""), reference=ctx.image_path
    )
    ctx.artifacts["image_url"] = result.get("url")
    ctx.artifacts["image_path"] = result.get("image_path")
    ctx.artifacts["family_id"] = built.get("family_id")
    ctx.artifacts["params"] = built.get("params")
    ctx.artifacts["prompt"] = prompt
    ctx.artifacts["size"] = result.get("size")
    if result.get("aspect_warning"):
        ctx.artifacts["aspect_warning"] = result["aspect_warning"]

    # ★ 出图前的漂移自检必须让模型看见（preflight 链路的最后一环）。
    #   此前 generate_image 直接无视 warnings/preflight —— 模型在花钱前
    #   根本不知道「媒介漂移 / 残留泄漏 / 锁定未落地」，用户也就永远听不到。
    #   仍然不阻断（阶段一原则：先收集数据），但要求模型如实转述，别只报喜。
    drift = built.get("preflight") or []
    drift_note = ""
    if drift:
        ctx.artifacts["preflight"] = drift
        drift_note = ("★ 出图前自检发现以下漂移提示，请如实告诉用户并建议重试方式，"
                      "不要隐瞒：" + "；".join(x[:60] for x in drift[:3]))

    audit("agent_generated", thread_id=ctx.thread_id,
          family=built.get("family_id"), size=result.get("size"))
    return {
        "success": True,
        "family_id": built.get("family_id"),
        "image_url": result.get("url"),
        "size": result.get("size"),
        "aspect_warning": result.get("aspect_warning") or "",
        "quota": quota,
        "preflight": drift,
        "note": "图片已生成。请用中文告诉用户选了哪个家族、叠加了什么、哪些原图特征被刻意保留了。"
                "★ 提一句：如果用户对结果某一处不满意，可以让他指出具体位置，"
                "你用 repair_image 只修那一处（画布上也有「局部修复」按钮）。"
                + (f"注意：{result['aspect_warning']}" if result.get("aspect_warning") else "")
                + drift_note,
    }


def _build_repair_prompt(change: str, constraints: list[str] | None = None) -> str:
    """构造修复提示词 —— 造梦师 Decode Repair 的「整图重生成」形态

    ★ 为什么这么短：修复走的参考图是**上一版成品**，构图/配色/风格已经由
      参考图本身锁死；把原家族提示词整段塞回来反而会让模型重新想象，
      把没让改的地方也改了（造梦师：只有局部漂移时不重新解码重写 prompt）。
      所以修复提示词只做三件事：说清要改的变量 + 明令其余全部不动 +
      重申用户既定约束（不许被修复顺手破坏）。

    ★ change 支持多行（前端一次勾选多个维度时每行一条）：
      逐条编号下发，最多 3 条 —— 超过 3 条改动就不可控了，
      用户没法判断哪条指令生效哪条没生效。单行超长截断。

    ★ constraints（2026-10-05 用户实测反馈）：修复不能破坏用户定下的规矩 ——
      自定义提示词与家族硬禁令在这里重申一遍。刻意**不塞家族三段式**
      （那是重新解码），只摘硬禁令与用户原话，保住约束又不把 prompt 撑爆。
    """
    c = str(change or "").strip()
    raw_items = [s.strip(" \t；;。.") for s in c.replace("；", "\n").split("\n")]
    items = [s for s in raw_items if s][:3]
    if not items:
        items = [c[:120]]
    for i, it in enumerate(items):
        if len(it) > 120:
            items[i] = it[:120] + "…"
    n = len(items)
    listed = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))

    out = [
        "【修复指令 · 最小外科修正】",
        "参考图就是上一版成品。只修正下面这 "
        f"{('1 处' if n == 1 else f'{n} 处')}，其余一切与参考图保持完全一致：",
        listed,
        "",
        "【保持不变】",
        "- 构图、视角、主体、动作、配色、光线、材质与风格一律不动",
        "- 不新增参考图中没有的元素，不重画未提及的区域",
        "- 修正处必须延续参考图的整体风格，不能变成另一张图",
    ]

    cleaned = [str(x).strip(" \n；;") for x in (constraints or [])]
    cleaned = [x for x in cleaned if x][:6]
    if cleaned:
        out += ["", "【必须继续遵守的既定约束（修复不许破坏）】"]
        out += [f"- {x[:120]}" for x in cleaned]
    return "\n".join(out)


def build_repair_constraints(extra_prompt: str = "", family_id: str = "") -> list[str]:
    """修复时要重申的既定约束 —— 用户自定义提示词 + 家族硬禁令

    ★ 为什么存在（2026-10-05 用户实测反馈）：修复以「上一版成品」为参考、
      不带家族三段式，这保证了"只改一处"，但也意味着模型看不到用户当初
      定下的规矩 —— 修复一次就可能把用户约束踩掉。这里把约束**重申**进
      修复指令（不是重新解码，只摘最硬的几条，不把 prompt 撑爆）。
    """
    cons: list[str] = []
    ep = str(extra_prompt or "").strip()
    if ep:
        cons.append(f"用户自定义要求（原样遵守）：{ep[:200]}")
    fam = _family_doc(family_id) if family_id else {}
    hf = fam.get("hard_forbid") if isinstance(fam, dict) else None
    if isinstance(hf, list) and hf:
        joined = "；".join(str(x).strip()[:60] for x in hf[:6] if str(x).strip())
        if joined:
            cons.append(f"家族硬禁令（修复同样不许违反）：{joined}")
    return cons


def tool_repair_image(args: dict, ctx: ToolContext) -> dict:
    """局部修复刚生成的图 —— 造梦师 Decode Repair 的最小实现

    与 generate_image 的本质区别：参考图是**上一版成品**而不是用户原图，
    提示词是 CHANGE ONLY 外科指令而不是家族三段式。
    这样"改一处"不会连带重画整张图。
    """
    change = str(args.get("change") or "").strip()
    if not change:
        return {
            "error": "缺少修复内容：必须说清要修正的具体问题。",
            "hint": "问用户具体哪里不对（如『树冠变成圆球了』），凝练成一句话再调用。",
        }

    last_image = ctx.artifacts.get("image_path")
    if not last_image or not os.path.exists(last_image):
        return {
            "error": "还没有可修复的生成结果。",
            "hint": "修复的对象是最近一次生成的图 —— 请先 generate_image 出图，"
                    "用户看到结果并提出具体问题后再修复。",
        }

    if not ctx.allow_spend:
        return {
            "error": "当前会话处于预览模式（allow_spend=false），不允许修复生成。",
            "hint": "告诉用户：修复会真实计费，需要在界面上确认。",
        }

    # 权限与额度：与 generate_image 同一套预扣 → 结算/退还
    try:
        token = reserve_generation(ctx.thread_id, reference_image=last_image)
    except GovernanceError as e:
        audit("repair_denied", thread_id=ctx.thread_id, code=e.code, **e.extra)
        return {
            "error": str(e),
            "code": e.code,
            "hint": "这是治理策略拦截，不是程序错误，不要尝试重试。",
        }

    prompt = _build_repair_prompt(
        change, build_repair_constraints(ctx.extra_prompt,
                                         str(ctx.artifacts.get("family_id") or "")))
    with step("Agent 修复出图", thread=ctx.thread_id, change=change[:40]):
        result = generate_image_with_reference(
            reference_image_path=last_image,       # ★ 参考图 = 上一版成品
            prompt=prompt,
            size=None,
        )

    if not result.get("success"):
        quota = release_generation(
            ctx.thread_id, token, reason=str(result.get("error"))[:200]
        )
        return {
            "error": result.get("error"),
            "quota": quota,
            "hint": "修复失败，额度已退还。可以让用户换个说法描述问题后再试。",
        }

    quota = settle_generation(
        ctx.thread_id, token, size=result.get("size", ""), reference=last_image
    )
    # ★ 新图顶替旧图成为「最新生成结果」—— 连续修复（修完一处再修另一处）天然成立
    ctx.artifacts["image_url"] = result.get("url")
    ctx.artifacts["image_path"] = result.get("image_path")
    ctx.artifacts["prompt"] = prompt
    ctx.artifacts["size"] = result.get("size")
    ctx.artifacts["last_repair"] = {"change": change, "from": last_image}

    audit("agent_repaired", thread_id=ctx.thread_id, change=change[:60])
    return {
        "success": True,
        "image_url": result.get("url"),
        "size": result.get("size"),
        "quota": quota,
        "note": "修复完成。请告诉用户：只改了「" + change[:60] + "」这一处，"
                "其余画面保持原样；如果还不满意，可以继续指出下一处问题。",
    }


# ══════════════════════ 注册表 ══════════════════════

IMPLEMENTATIONS = {
    "extract_card": tool_extract_card,
    "list_families": tool_list_families,
    "describe_family": tool_describe_family,
    "read_image_info": tool_read_image_info,
    "render_prompt": tool_render_prompt,
    "generate_image": tool_generate_image,
    "repair_image": tool_repair_image,
}

# 哪些工具成功之后就可以收尾了（避免模型出完图还继续空转）
# repair_image 也是终态：修完应把结果交给用户，由用户决定要不要再修下一处
TERMINAL_TOOLS = frozenset({"generate_image", "repair_image"})


def missing_implementations() -> list[str]:
    """契约里声明了但没有实现 / 实现了契约里没有 —— 启动自检用

    这两边对不上是极隐蔽的 bug：模型看得到 Schema 会去调，
    但 dispatch 里没有对应函数，运行时才炸。
    """
    from contracts.tools import TOOL_NAMES
    return sorted(set(IMPLEMENTATIONS) ^ set(TOOL_NAMES))


def dispatch(name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
    """执行一次工具调用 —— 永远不抛异常

    返回 ToolResult，observation 一定可直接 json.dumps。
    """
    arguments = dict(arguments or {})

    # ① Schema 校验：模型的 typo 变成可见错误而不是崩溃
    errs = validate_call(name, arguments)
    if errs:
        return ToolResult(name, False, {
            "error": "工具参数不合法",
            "details": errs,
            "hint": "按提示修正参数名后重试，不要重复同样的调用。",
        })

    fn = IMPLEMENTATIONS.get(name)
    if fn is None:
        return ToolResult(name, False, {
            "error": f"工具 {name!r} 没有实现",
            "hint": "这是代码缺陷，请反馈给开发者。",
        })

    # ② 花钱类工具在 Schema 之外再兜一道 allow_spend
    if costs_money(name, arguments):
        if not ctx.allow_spend:
            # ★ 条件计费工具（extract_card）在预览模式下**降级而不是拒绝**：
            #   拒绝会让「预览模式」下连本地取色都用不上，而本地档是 0 成本的。
            #   直接改成强制 use_vlm=False 更符合用户预期。
            if name in CONDITIONAL_SPEND_TOOLS:
                param = CONDITIONAL_SPEND_TOOLS[name][0]
                logger.info("预览模式：%s 的 %s 强制降级为不花钱档", name, param)
                arguments = {**arguments, param: False}
                ctx.downgraded.add(name)
            else:
                return ToolResult(name, False, {
                    "error": "预览模式下不允许调用计费工具。",
                    "hint": "正式出图需要在界面上确认。",
                })

    try:
        obs = fn(arguments, ctx)
    except Exception as e:                       # 工具实现内部出错也必须收敛成观察结果
        logger.exception("工具 %s 执行异常", name)
        return ToolResult(name, False, {
            "error": f"{type(e).__name__}: {e}",
            "hint": "这是服务端错误，换成别的参数不见得有用，请如实告诉用户。",
        })

    ok = not bool(obs.get("error")) if isinstance(obs, dict) else True
    return ToolResult(name, ok, obs, terminal=name in TERMINAL_TOOLS and ok)


__all__ = [
    "IMPLEMENTATIONS",
    "ToolContext",
    "ToolResult",
    "TERMINAL_TOOLS",
    "dispatch",
    "missing_implementations",
]
