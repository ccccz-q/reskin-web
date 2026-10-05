"""漂移诊断（repair v1）—— 「AI 找问题」的纯逻辑核心

════════ 为什么单独一个文件 ════════

这东西会**真花钱**（一次付费视觉调用），所以它的每一条降级路径都必须能被
测试反复走到；而 FastAPI 端点要起 TestClient、要 FormData、要会话头 ——
每样都让「把失败分支测一遍」的成本翻一倍。
所以拆成：

    routers/image.py   只管 HTTP：取参、鉴权、限速、状态码
    services/drift.py  只管逻辑：拼消息 → 调视觉模型 → 解析 → 归一化

纯函数、无 FastAPI 依赖，测试可以直接喂假模型的返回值。

════════ 判据来自造梦师 Decode Repair ════════

造梦师区分「该改的」与「不该改的」，这里翻成两分类：

    adaptation  为服从模板风格而发生的合理改变（整体变插画质感、色调统一）
                → 不是问题，不值得花一张额度去重修
    drift       模板没要求、也不该发生的意外改变（人物多出眼镜、建筑转向、
                画面里冒出文字水印）
                → 值得修，也正是 repair_image 要接管的东西

★ 失败一律 `ok=False + 一句能照做的话`，绝不往上抛：
  诊断是**增强**不是前提 —— 它挂了用户照样能手填修复内容。
"""
from __future__ import annotations

import json
import time
from typing import Any

from infra.logging import logger

MAX_DRIFTS = 3
MAX_CHANGE_CHARS = 120
MAX_WHY_CHARS = 80

# 视觉模型单次诊断的调用预算（秒）—— 诊断不能让人等太久，倒了就给出可手填的降级
DIAGNOSE_TIMEOUT_SEC = 90

_DIAGNOSE_TEXT = (
    "第一张图是用户的原图，第二张图是按风格模板重绘的成品。"
    "对比两者，找出成品相对原图的显著改变，并逐条分类：\n"
    "- adaptation：为服从模板风格而发生的合理改变"
    "（如整体变成插画质感、色调统一）—— 不算问题\n"
    "- drift：模板没要求、也不该发生的意外改变"
    "（如人物多了眼镜、建筑转向、多了文字水印）\n"
    '输出 JSON，不要任何其他文字：\n'
    '{"drifts": [{"change": "一句话描述这处改变", '
    '"kind": "adaptation|drift", "why": "一句话理由"}]}\n'
    "最多 3 条，只列最显眼的；没有值得说的就返回 {\"drifts\": []}。"
    "change 要写成可照着修的指令（如「人物恢复原图的侧脸角度」），不要写成形容词。"
)

# ★ 模板上下文（2026-10-05 用户实测反馈）：不知道成品出自哪个模板时，
#   VLM 会把「模板刻意添加的元素」判成 drift —— 实测把涂鸦小人、手写文案
#   标成了"建议修"，用户一修就把风格修没了。带上模板名与描述后，
#   "按模板该有的"会被归入 adaptation，只有真正的意外才值得修。
_FAMILY_CONTEXT_TMPL = (
    "\n\n已知信息：该成品是用风格模板「{name}」生成的 —— {desc}。"
    "判断时先读这条：凡是**这个模板按其风格本该添加或改变的东西"
    "（描述里点名的元素、该风格的标志性笔法）都属于 adaptation，绝不列入 drift**；"
    "drift 只留给模板风格解释不了的意外（主体被改、多出无关物件、文字乱码等）。"
)

# ★★ 内置模板专用口径（2026-10-05 用户实测反馈，第二次）
#   实测：材料印章（内置）生成的图左上角带模板自带的英文字样，
#   诊断却给出「材料印章模板不要求额外添加文字」这种**模板内部理由**，
#   把成品按"建议修"——用户点下去就会把风格自带的字样修掉。
#   判据：内置模板的风格就是产品的一部分，它的版式/文字/印章/边框/拼贴
#   都是"本来就该那样"，拿"模板没要求"去指责它等于用我们的标准打我们自己。
#   所以内置模板：**禁止**用任何"模板是否要求/没要求"作为理由；
#   只有当元素与原照片本身矛盾时才提（主体被改、无关物件、文字乱码等）。
_BUILTIN_CONTEXT_TMPL = (
    "\n\n重要规则（内置风格模板专用）：该成品用的是本应用**内置**风格模板 —— 「{name}」{desc}。"
    "内置模板的画法就是产品的一部分，**禁止**用「模板没要求 / 模板不要求 / 模板未提及」"
    "这类**模板内部理由**提出修改：模板自带的版式、文字、印章、边框、拼贴、笔触等一律视为"
    "风格本身（adaptation），不得列为 drift。"
    "只有当某个元素**与原照片本身矛盾**时才算问题，例如：主体被改形或换掉、"
    "凭空多出与场景无关的物件、文字乱码或明显错字、水印/二维码。"
)

_FALLBACK_MSG = "AI 找问题没成功，请直接手写要修的地方"
_UNPARSED_MSG = "AI 的诊断结果没解析出来，请直接手写要修的地方"


def parse_drifts(raw: str) -> dict:
    """视觉模型的返回文本 → 归一化候选列表

    ★ 这条解析线的要求与 card_extractor 一致：**不要因为模型多说了一句话
      就把整段付过费的结果丢掉**。``response_format=json_object`` 能挡住大部分，
      但挡不住模型先说句客套话再吐 JSON（实测过），所以要自己抠首末花括号。
    """
    if not raw or not isinstance(raw, str):
        return {"ok": False, "error": _UNPARSED_MSG}
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        return {"ok": False, "error": _UNPARSED_MSG}
    try:
        data = json.loads(raw[start:end])
    except (ValueError, TypeError) as e:
        logger.warning("漂移诊断 JSON 解析失败：%s", e)
        return {"ok": False, "error": _UNPARSED_MSG}
    if not isinstance(data, dict):
        return {"ok": False, "error": _UNPARSED_MSG}

    out: list[dict[str, Any]] = []
    for item in (data.get("drifts") or [])[:MAX_DRIFTS]:
        if not isinstance(item, dict):
            continue
        change = str(item.get("change") or "").strip()
        if not change:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        # kind 缺失/写错按 drift 处理：宁可让用户看到并删掉，
        # 也不要替他把「本该修的」静默判成 adaptation 然后藏起来。
        if kind not in ("adaptation", "drift"):
            kind = "drift"
        out.append({
            "change": change[:MAX_CHANGE_CHARS],
            "kind": kind,
            "why": str(item.get("why") or "")[:MAX_WHY_CHARS],
        })
    return {"ok": True, "drifts": out}


def diagnose_drifts(original_path: str, generated_path: str,
                    family_hint: str = "", builtin: bool = False) -> dict:
    """对比原图与成品 —— 返回 {"ok", "drifts"|"error", "elapsed_sec"}

    family_hint：形如「趣味涂鸦叙述者｜原照片主体保真融入整幅画面…」的
    模板上下文；给了它，"模板本该有的元素"才不会被误判成漂移。
    builtin：该模板是否**内置**。内置模板额外套一层口径
    （见 _BUILTIN_CONTEXT_TMPL）：禁止用"模板没要求"当理由 ——
    实测材料印章（内置）的成品左上角带模板自带字样，VLM 却以
    "材料印章模板不要求额外添加文字"为由把它标成"建议修"，
    用户一点就把风格自带的字样修掉了。

    刻意**不抛异常**：拿到手的钱已经花出去了，把异常冒上去只会让用户
    看到一个 500，而他要的只是「能不能给出候选」。
    """
    started = time.monotonic()
    text = _DIAGNOSE_TEXT
    if family_hint:
        name, _, desc = family_hint.partition("｜")
        if builtin:
            text += _BUILTIN_CONTEXT_TMPL.format(
                name=name or "未命名模板",
                desc=(f"（{desc}）" if desc else ""))
        else:
            text += _FAMILY_CONTEXT_TMPL.format(
                name=name or "未命名模板", desc=desc or "（无描述）")
    try:
        from services.card_extractor import _prepare_for_vlm
        from services.llm import LLMError, vision

        parts: list[dict] = [{"type": "text", "text": text}]
        for p in (original_path, generated_path):
            b64, mime = _prepare_for_vlm(p)
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
    except Exception as e:            # 图片坏了 / 读不出来 —— 让人手填，别炸请求
        logger.warning("漂移诊断的图片预处理失败：%s", e)
        return {"ok": False, "error": _FALLBACK_MSG,
                "elapsed_sec": round(time.monotonic() - started, 2)}

    try:
        raw = vision(
            [{"role": "user", "content": parts}],
            max_tokens=500,
            response_format={"type": "json_object"},
            timeout=DIAGNOSE_TIMEOUT_SEC,
        )
    except LLMError as e:
        logger.warning("漂移诊断失败（不影响主流程）：%s", e)
        return {"ok": False, "error": _FALLBACK_MSG,
                "elapsed_sec": round(time.monotonic() - started, 2)}
    except Exception as e:            # 视觉层承诺抛 LLMError，但兜底不能赌承诺
        logger.exception("漂移诊断出现未预期异常（已降级）")
        return {"ok": False, "error": _FALLBACK_MSG,
                "elapsed_sec": round(time.monotonic() - started, 2)}

    result = parse_drifts(raw)
    result["elapsed_sec"] = round(time.monotonic() - started, 2)
    return result


__all__ = ["MAX_DRIFTS", "diagnose_drifts", "parse_drifts"]
