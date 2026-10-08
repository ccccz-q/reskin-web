"""提示词构建入口 —— 全项目唯一的「模板/家族 → 提示词」通道

════════ 为什么要有这一层（对照审查报告 P0-1）════════

旧版把「怎么生成提示词」的逻辑散在两处，且两处都写死了 `template["prompt_template"]`：

    routers/image.py:33      prompt = template["prompt_template"]
    agents/image_agent.py:66 state["template_prompt"] = template["prompt_template"]

而 travel_sketch.yaml 重写成三段式之后，**根本没有这个字段了** —— 实测：

    >>> template.get("prompt_template")
    KeyError: 'prompt_template'

后果：POST /api/image/generate 与 /api/chat 的出图路径 100% 500。
更糟的是它表面看不出来：服务起得来、健康检查 200、文档页能打开，
只有真正点「生成」时才炸。

现在所有调用方都走这里，`prompt_template` 缺失不再是崩溃，而是**两种合法的模板形态之一**：

    形态 A（旧）  prompt_template: "..."                    → 直接用
    形态 B（新）  family_id: xxx + params                   → 交给 family_renderer 渲染三段式

形态 B 是推荐形态，也是整套「1 骨架 × 6 家族 × N 参数」框架的实际落点。
"""
from __future__ import annotations

import os
import sys

# 导入根是 backend/app（与项目原有风格一致：main.py 写 from routers.image import ...，
# config 也是顶层模块）。单独 python xxx.py 运行时需补上这个根。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.family_renderer import (                           # noqa: E402
    FamilyRenderError,
    render_family,
    render_to_prompt,
)
from services.template_manager import (                          # noqa: E402
    get_family_by_id,
    get_template_by_id,
)


class SourceNotFound(Exception):
    """既不是模板也不是家族"""


class MissingRequiredParams(Exception):
    """家族声明了 required 参数但调用方没给 —— 应由 API 转成 422 让前端补表单"""

    def __init__(self, missing: list[str], family_id: str):
        super().__init__(f"缺少必填参数：{missing}")
        self.missing = missing
        self.family_id = family_id


class RenderFailure(Exception):
    """渲染过程中出错（家族 YAML 有问题 / 占位符无法解析）"""


# 模板 YAML 里可以带一组默认参数，用户不传时用它
def _merge_defaults(base: dict, override: dict | None) -> dict:
    out = dict(base or {})
    for k, v in (override or {}).items():
        if v is not None and not (isinstance(v, str) and v.strip() == ""):
            out[k] = v
    return out


def resolve_source(source_id: str) -> tuple[dict, str]:
    """按 id 找到渲染源，返回 (文档, 类型)

    类型 ∈ {"template", "family"}。找不到抛 SourceNotFound（含可用清单，便于排查）。
    """
    if not source_id:
        raise SourceNotFound("未提供 template_id / family_id")

    tmpl = get_template_by_id(source_id)
    if tmpl:
        return tmpl, "template"

    fam = get_family_by_id(source_id)
    if fam:
        return fam, "family"

    raise SourceNotFound(
        f"找不到 {source_id!r}：既没有同 id 的模板，也没有同 id 的家族"
    )


def build_prompt(
    source_id: str,
    params: dict | None = None,
    card: dict | None = None,
    extra_prompt: str = "",
    extra_mode: str = "append",
    locked: list | None = None,
) -> dict:
    """统一的提示词构建入口

    extra_prompt / extra_mode：用户自定义提示词（见 _apply_extra_prompt）
      - "append"（默认）：追加到 creative 段
      - "replace"：替换 creative 段，但**保留** preserve 与 forbid

    返回 {
        prompt, source_id, source_kind, family_id,
        params, segments, warnings, allow_change, auto_resolved, bridged,
        extra_applied  ← 自定义提示词的落地说明（"" 表示没用到）
    }
    """
    doc, kind = resolve_source(source_id)
    params = dict(params or {})
    card = dict(card or {})

    # ── 形态 A：模板自带 prompt_template ────────────────────
    if kind == "template":
        legacy = (doc.get("prompt_template") or "").strip()
        if legacy:
            return {
                "prompt": legacy,
                "source_id": source_id,
                "source_kind": "template",
                "family_id": doc.get("family_id"),
                "params": params,
                "segments": None,
                "warnings": ["该模板使用 prompt_template 形态，未走家族三段式。"],
                "allow_change": [],
                "auto_resolved": [],
                "bridged": [],
                # 返回体形状对齐：调用方可以无条件读 r["preflight"]，不用分支判断
                "preflight": [],
            }

        # 模板本身没有正文 → 它只是「挂在某个家族下的预设」，把默认参数合进去之后走家族
        family_id = doc.get("family_id")
        if not family_id:
            raise RenderFailure(
                f"模板 {source_id} 既没有 prompt_template，也没有 family_id —— 无法生成提示词"
            )
        # ★ 只走 template_manager（审查 P2-6 的终裁）：
        #   旧代码在查不到时还会兜底 family_renderer.get_family()（每次读盘+全量校验）。
        #   核实后这个 fallback 是**可证明的死代码**：
        #     ① template_manager 递归扫描整个 templates 目录（是 families 目录的超集）；
        #     ② 两边校验同源（template_manager._validate_family 委托 renderer 强校验）；
        #     ③ 工坊 install/删除都会 clear_cache()，不存在"写了盘但缓存没更新"的窗口。
        #   能被 renderer 读到的，template_manager 一定能查到；读不到的（坏 YAML）两边都拒绝。
        #   双份加载器从此职责清晰：renderer 那份只归 CLI/测试。
        family = get_family_by_id(family_id)
        if not family:
            raise SourceNotFound(
                f"模板 {source_id} 指向家族 {family_id}，但该家族未加载"
            )
        params = _merge_defaults(doc.get("default_params") or {}, params)
        return _render_via_family(family_id, family, params, card,
                                  extra_prompt, extra_mode, locked)

    # ── 形态 B：直接按家族渲染 ──────────────────────────────
    return _render_via_family(doc.get("id"), doc, params, card,
                              extra_prompt, extra_mode, locked)


def _apply_extra_prompt(
    segments: dict, extra_prompt: str, mode: str
) -> tuple[dict, str]:
    """把用户自定义提示词合进三段式，返回 (新 segments, 落地说明)

    ★ 为什么只动 creative 段（设计取舍，写清楚免得被当成漏做）
    --------------------------------------------------------
    preserve / forbid 是「原图保真」的载体 —— 它们约束的是**什么不许变**。
    用户写自定义提示词，本意几乎总是「我想要这个效果」，
    也就是 creative 的范畴。如果连 preserve/forbid 一起让用户覆盖，
    这个项目相对「直接调用生图 API」的价值就归零了。

    所以两种模式都保留保真约束，差别只在 creative：
      append  → 家族创作描述 + 用户追加要求
      replace → 只有用户的要求（外加一句「优先于家族预设」的声明）

    真想要「完全裸调」的场景，那不该走这里 —— 那是另一个产品形态。

    用户文本同样要过 sanitize_value：它跟 params 一样是用户可控的，
    不净化就能用 `{hard_forbid_joined}` 把 forbid 段搬运进 creative。
    """
    from services.family_renderer import (
        MAX_EXTRA_PROMPT_CHARS, MAX_EXTRA_PROMPT_HARD_CAP, sanitize_value,
    )

    raw = str(extra_prompt or "").strip()

    # ★ 用户自定义提示词**不截断**（产品原则，2026-10-05 经用户确认）
    #
    # 这个项目「以像原图为主 + 精准提炼」，用户自己写的创作要求属于
    # USER-LOCKED 级别的输入 —— 静默砍掉它，用户的意图就不精准了，
    # 这与造梦师「不许牺牲锁定事实」是同一条原则，也与本项目
    # 「preserve/forbid 永不截断」的立场一致。
    #
    # 成本治理的边界要划清：
    #   - MAX_PARAM_CHARS 限的是**参数**（防某个参数被塞长篇大论放大 token）✔ 保留
    #   - 用户在输入框里认真写的要求 → 不限长 ✔（本函数）
    #
    # 防滥用只留一道**硬顶**（MAX_EXTRA_PROMPT_HARD_CAP）：超过它说明
    # 大概率是误粘贴整篇文档，此时截断但**必须非常醒目地告知**，
    # 绝不静默 —— 静悄悄变短比截断本身更危险。
    if len(raw) > MAX_EXTRA_PROMPT_HARD_CAP:
        text = sanitize_value(raw, MAX_EXTRA_PROMPT_HARD_CAP).strip()
        if not text:
            return segments, ""
        out = dict(segments)
        note = (f"你的提示词 {len(raw)} 字超出硬上限 {MAX_EXTRA_PROMPT_HARD_CAP} 字，"
                f"已截断为 {len(text)} 字 —— 请精简后重试，或分多次生成")
        if str(mode).lower() == "replace":
            out["creative"] = text
            return out, note
        base = (out.get("creative") or "").rstrip()
        out["creative"] = f"{base}\n\n【用户追加要求】{text}" if base else text
        return out, note

    text = sanitize_value(raw, None).strip()          # 只剥占位符，不截断
    if not text:
        return segments, ""

    # 超过「建议长度」只提醒、不砍 —— 让用户自己决定要不要精简
    advisory = (f"提示词较长（{len(raw)} 字），已完整写入；"
                f"过长会稀释家族约束的权重，建议精简"
                if len(raw) > MAX_EXTRA_PROMPT_CHARS else "")

    out = dict(segments)
    if str(mode).lower() == "replace":
        out["creative"] = (
            f"{text}\n\n"
            "（以上是用户直接给出的创作要求，在创作方向上优先于家族预设描述；"
            "但必须严格遵守后文的禁止项与保留项。）"
        )
        return out, "已用你的提示词替换 creative 段（保真约束仍然生效）" + advisory

    base = (out.get("creative") or "").rstrip()
    out["creative"] = f"{base}\n\n【用户追加要求】{text}" if base else text
    return out, "已把你的提示词追加到 creative 段末尾" + advisory


def _truncation_note(truncated: bool, raw_len: int, kept_len: int) -> str:
    """被截断时的说明文案 —— 静悄悄变短比截断本身更危险"""
    if not truncated:
        return ""
    return f"（原文 {raw_len} 字，超出硬上限已截断为 {kept_len} 字，请精简后重试）"


def _render_via_family(
    family_id: str, family: dict, params: dict, card: dict,
    extra_prompt: str = "", extra_mode: str = "append",
    locked: list | None = None,
) -> dict:
    # ── ★ 手动提示词短路（实测需求 2026-10-03）────────────────
    # 用户在工坊里手改过提示词（prompt_override 非空）→ 生成时直接用它，
    # 不再走 spec 渲染。参数调节（dicts/params）对该家族暂时失效——
    # 这正是「我只改这一小段，别动其它」的语义；恢复自动渲染=清空 override。
    # extra_prompt（生成页的自定义追加）仍然生效：拼在 override 之后。
    override = str((family or {}).get("prompt_override") or "").strip()
    if override:
        warnings = ["该家族正在使用手动修改的提示词 —— 参数调节不参与生成；"
                    "到工坊「恢复自动渲染」可回到参数可调状态。"]
        extra_applied = ""
        text = override
        if (extra_prompt or "").strip():
            from services.family_renderer import (
                MAX_EXTRA_PROMPT_HARD_CAP, sanitize_value,
            )
            extra_applied = ("已把手动的自定义提示词追加到手改提示词之后"
                             if extra_mode == "append"
                             else "自定义提示词替换模式对手改提示词家族不生效，已忽略")
            if extra_mode == "append":
                # ★ 与 _apply_extra_prompt 同一原则：用户要求不截断，仅硬顶防滥用
                raw_extra = str(extra_prompt).strip()
                if len(raw_extra) > MAX_EXTRA_PROMPT_HARD_CAP:
                    kept = sanitize_value(raw_extra, MAX_EXTRA_PROMPT_HARD_CAP).strip()
                    extra_applied += _truncation_note(
                        True, len(raw_extra), len(kept))
                else:
                    kept = sanitize_value(raw_extra, None).strip()
                # kept 可能为空（全部被 sanitize 剥光）—— 别给手改提示词留尾部换行
                text = override + "\n" + kept if kept else override
        if extra_applied:
            warnings.append(f"自定义提示词：{extra_applied}")
        return {
            "prompt": text,
            "source_id": family_id,
            "source_kind": "family",
            "family_id": family_id,
            "params": params,
            "segments": None,
            "warnings": warnings,
            "allow_change": [],
            "auto_resolved": [],
            "bridged": [],
            "extra_applied": extra_applied,
            # 手改提示词绕过了渲染，无从做漂移自检 —— 但字段要存在，形状才一致
            "preflight": [],
        }

    try:
        # strict=False：把「缺参数」「占位符悬空」变成结构化字段，由调用方决定怎么呈现，
        # 而不是让一个 KeyError 冲到最外层变成 500。
        result = render_family(family, params, card, strict=False, locked=locked)
    except FamilyRenderError as e:
        raise RenderFailure(f"家族 {family_id} 渲染失败：{e}") from e

    if result["missing_required"]:
        raise MissingRequiredParams(result["missing_required"], family_id)
    if result["unresolved"]:
        raise RenderFailure(
            f"家族 {family_id} 存在未解析占位符：{result['unresolved']}"
        )

    # 自定义提示词在**渲染之后**合入 —— 必须在占位符替换完，
    # 否则用户文本里的花括号会被当成占位符参与渲染。
    segments, extra_applied = _apply_extra_prompt(
        result["segments"], extra_prompt, extra_mode
    )
    warnings = list(result["warnings"])
    if extra_applied:
        warnings.append(f"自定义提示词：{extra_applied}")

    final_prompt = render_to_prompt({**result, "segments": segments})

    # ── ★ 编译后 Preflight 自检（造梦师 Dream Decode preflight 的移植）──
    #
    # 放在 render_to_prompt **之后**：要检查的是最终产物，不是中间态。
    # 现阶段只告警、不阻断 —— 我们无法离线证明阻断不会误伤正常请求，
    # 而「点生成却出不来图」比「出了一张不太像的图」严重得多。
    # 等真实数据证明零误报，再考虑升级为阻断。
    #
    # ★ 为什么放在这里而不是 family_renderer 里：
    #   render_family 是纯渲染函数，被预览/工坊/出图多处调用；
    #   preflight 只该在「真的要出图」这条链上跑一次，避免预览接口被拖慢、
    #   也避免工坊编译时的中间态产生噪音告警。
    #
    # ★ 传**合并前**的 segments（审查存疑项 2 的裁决）：
    #   「过载」检查的语义是"家族自带的载荷太重"，而用户自定义提示词
    #   已按产品原则**不截断** —— 用户认真写 2400 字是他自己的选择，
    #   不该被「权重被稀释」的告警指责（告警在指责一个产品明确允许的行为）。
    #   所以过载只数家族自己的载荷；存在性类检查（USER-LOCKED/残留）仍看最终 prompt。
    #
    # ★ 旁挂检查永不打挂出图：preflight 内部已吞掉所有异常。
    try:
        from services.preflight import preflight as _preflight
        drift = _preflight(
            spec=family,
            params=result["params"],
            card=card,
            segments=result["segments"],
            prompt=final_prompt,
            locked=locked,
        )
    except Exception:                                        # 双保险
        drift = []
    if drift:
        warnings.extend(drift)

    return {
        "prompt": final_prompt,
        "source_id": family_id,
        "source_kind": "family",
        "family_id": family_id,
        "params": result["params"],
        "segments": segments,
        "warnings": warnings,
        "allow_change": result["allow_change"],
        "auto_resolved": result["auto_resolved"],
        "bridged": result["bridged"],
        "extra_applied": extra_applied,
        "preflight": drift,
    }


def build_preview(
    source_id: str, params: dict | None = None, card: dict | None = None
) -> dict:
    """【预留】只渲染不生成图（方案 §5.2 的第①档预览：0 成本、毫秒级）

    当前 routers 直接调 build_prompt（语义完全一致），所以这个别名暂无调用方。
    保留是为了让「预览档」在服务层有一个具名入口，而不是散在路由里。

    原文档：

    前端滑杆一动就调它，把三段式实时显示出来，让用户看见「调这个参数到底改了哪句话」。
    这就是用「提示词实时重渲染」替代「真·实时生图」的关键——后者在闭源付费模型上不成立。
    """
    return build_prompt(source_id, params, card)


def available_sources() -> list[dict]:
    """列出所有可用源（模板 + 家族），给前端选择器用"""
    from services.template_manager import load_families, load_templates

    out: list[dict] = []
    for t in load_templates():
        out.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "icon": t.get("icon", "🎨"),
            "description": t.get("description", ""),
            "kind": "template",
            "family_id": t.get("family_id"),
            "recommended_scenes": t.get("recommended_scenes", []),
            "style_tags": t.get("style_tags", []),
        })
    for f in load_families():
        out.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "icon": f.get("icon", "🎨"),
            "description": f.get("description", ""),
            "kind": "family",
            "family_id": f.get("id"),
            "recommended_scenes": f.get("suitable", []),
            "style_tags": [],
        })
    return out


def safe_build(source_id: str, params: dict | None, card: dict | None,
               extra_prompt: str = "", extra_mode: str = "append",
               locked: list | None = None) -> dict:
    """给 Agent 工具用的容错版本：失败不抛，返回 {"ok": False, "error": "..."}

    Agent 循环里，工具失败应该是「给模型看的一条观察结果」，
    让它有机会换个参数重试，而不是直接炸掉整个对话。
    """
    try:
        result = build_prompt(source_id, params, card, extra_prompt, extra_mode, locked)
    except MissingRequiredParams as e:
        return {"ok": False, "error": str(e), "missing": e.missing}
    except SourceNotFound as e:
        return {"ok": False, "error": str(e)}
    except RenderFailure as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                                   # 兜底：工具不应抛出
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    result["ok"] = True
    return result


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("可用渲染源：")
    for s in available_sources():
        print(f"  [{s['kind']:8}] {s['id']:16} {s['name']}")

    print("\n用 travel_sketch 构建提示词（该模板无 prompt_template，走家族 fallback）：")
    r = build_prompt("travel_sketch")
    print(f"  source_kind = {r['source_kind']}")
    print(f"  family_id   = {r['family_id']}")
    print(f"  提示词长度  = {len(r['prompt'])}")
    print("  前 120 字：", r["prompt"][:120].replace("\n", " "))
