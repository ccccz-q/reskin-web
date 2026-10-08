"""工坊自修的**处置决策** —— 让模型判断"该修还是该重做"

★★★ 这是本项目里Agent 真正承担不可替代工作的位置 ★★★

    from services.repair_decision import classify_and_route

    decision = classify_and_route(errors, card, doc)
    # → {"action": "code_repair"|"recompile"|"creative_regen"|"give_up",
#     "reason": "...", "confidence": "high"|"medium"|"low"}

★ 为什么这段必须由模型判断，不能写成固定顺序：
  「校验失败」这个信号下面藏着**性质完全不同**的失败：

    「未解析占位符」     → 草稿里有个占位符指向不存在的参数
                        → **代码能修**（摘掉它），不必惊动模型
    「hard_forbid 不足」 → 草稿缺保真底线条款
                        → **重编译**（让它重写），修补没有意义
    「创意不足」          → 结构没问题，但视觉方案平庸
                        → **走 creative 重生成**，修结构是南辕北辙
    「形状跑偏」          → 模型返回了 list 而不是 dict
                        → **归一化层先兜一次**，兜不住再重编译

  如果写成固定顺序的「重试 N 次」，上面四种会被同一种方式对待 ——
  而其中至少两种是**怎么重试都不会变好**的：
  给"创意不足"的草稿做结构修补，纯属浪费一轮调用和时间。

⇒ **这段的价值不在"多一次 LLM 调用"，而在于「避免用错方式解决问题」。**
  它是本项目里"Agent 承担判断、确定性代码承担执行"这个分工的落点。

★ 边界（刻意不做什么）：
  · 不让模型改草稿内容 —— 它只**分类**，不**动手**。
    动手仍然由确定性代码 + 受约束的编译 prompt 完成，
    避免"模型自由改写 → 破坏三段式契约"这类风险。
  · 判断失败时**退化为原有的重试路径**（见 repair_decision 的降级注释）。
    Agent 在这里是加速器，不是新的单点故障。
"""
from __future__ import annotations

import json
import logging
import os
import re

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

logger = logging.getLogger(__name__)

# ── 错误特征的分类规则 ────────────────────────────────────────
# ★ 为什么规则与模型并存：
#   纯规则 = 快、确定、可测，但覆盖不了没见过的错误表述；
#   纯模型 = 覆盖广，但会漂、会在超时后拖垮整条链路。
#   所以：**高置信的用规则直接判（不花钱、零延迟），
#   拿不准的才交给模型**。这也让"决策"这一步大多数时候是免费的。
#
# ★★★ 顺序即优先级，且**词表不许重叠**（补测试时被抓到）：
#   第一版里 creative_regen 的关键词含「不足 / 缺少」，
#   而 recompile 也含「不足 / 缺少」——
#   于是「创意不足，视觉方案单薄」被判成 recompile，
#   **因为 recompile 排在前面先命中**。
#   这类"两个分支抢同一个词"是分类器最典型的 bug：
#   它不会报错，只会让决策静默地永远走向第一个分支。
#   ⇒ 现在的做法：**把判据收紧到互斥**，宁可漏判（漏判会交给模型）
#     也不要抢判（抢判会稳定走错路）。
_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # (action, 特征关键词, 说明)
    # —— 代码层能兜底的放最前：它们最确定、且省一次 LLM 调用
    ("code_repair",
     ("未解析占位符", "悬空引用", "占位符", "残缺语句", "dropped"),
     "占位符指向不存在的参数 —— 代码可摘除，不必重做"),
    ("normalize",
     ("结构异常", "形状异常", "不是字典", "不是列表", "已归一", "类型"),
     "返回形状跑偏 —— 先让归一化层兜一次"),
    # —— 创意类判据必须**不含**「不足/缺少」等通用词，否则与 recompile 抢
    ("creative_regen",
     ("创意", "平庸", "单薄", "视觉方案", "世界观", "表现力"),
     "结构没问题但视觉方案不够 —— 该走创意层重生成"),
    # —— recompile 放最后：它是"兜底的重做"，让更具体的判据先命中
    ("recompile",
     ("hard_forbid", "不足 4", "缺段", "missing", "回落", "不在允许值内",
      "未声明", "缺少", "缺必填", "未识别"),
     "草稿缺少必填结构条款 —— 修补不如重新编译"),
)


def _rule_route(errors: list[str]) -> tuple[str, str] | None:
    """规则优先：命中关键词就返回 (action, reason)。"""
    blob = " ".join(errors or [])
    for action, kws, why in _RULES:
        if any(k in blob for k in kws):
            return action, f"规则命中（{why}）"
    return None


# ── 给模型看的分类提示 ────────────────────────────────────────
_DECISION_SYSTEM = (
    "你是图像提示词工程的诊断员。给定一份家族草稿的校验错误清单，"
    "只判断「该怎么处理」，**不要修改草稿本身**。\n"
    "可选动作：\n"
    "  code_repair   —— 错误是占位符/悬空引用之类的机械问题，代码层面能摘除\n"
    "  normalize     —— 返回结构跑偏（类型不对），归一化能兜住\n"
    "  recompile     —— 草稿缺少必填结构条款（缺段、枚举越界、保真条目不足）\n"
    "  creative_regen—— 结构没问题，但视觉方案单薄/创意不足\n"
    "  give_up       —— 反复修不好，或错误说明要求本身不合理\n"
    "只输出 JSON：{\"action\": \"...\", \"reason\": \"不超过30字\"}"
)


def classify_and_route(errors: list[str], *, card: dict | None = None,
                       doc: dict | None = None) -> dict:
    """判断该怎么处置。**永远返回一个可用的 action**，绝不抛异常。

    ★ 降级路径很重要：这个函数在主链路上，
      它自己出错不能把整条提炼链路带崩——
      任何异常都退化成"按原有方式重修一轮"。

    ★ 入参也必须能扛脏数据（实测补测试时被抓到）：
      `errors` 来自校验器，理论上都是字符串，
      但**QC 的错误列表里可能混进 None / 数字 / 嵌套结构**
      （模型返回的字段原样透传）。第一版直接`" ".join(errors)`
      遇到 `[123]` 会抛 `TypeError: sequence item 0: expected str instance`
      —— 而这发生在主链路上，**一个类型错误就能让整次提炼失败**。
      所以先归一化，不信任调用方。
    """
    # ── 入参归一：任何脏数据都不许进到决策逻辑里 ──
    try:
        err_list = [str(e) for e in (errors or []) if e is not None]
    except Exception:                                           # noqa: BLE001
        err_list = []
    err_list = [e for e in err_list if e.strip()]

    if not err_list:
        return {"action": "none", "reason": "没有错误", "confidence": "high",
                "source": "rule"}

    # ① 规则优先（不花钱、零延迟、覆盖常见形态）
    hit = _rule_route(err_list)
    if hit:
        action, why = hit
        return {"action": action, "reason": why, "confidence": "high",
                "source": "rule"}

    # ② 规则拿不准 → 交给模型判断
    try:
        from services.llm import chat
        prompt = json.dumps({
            "errors": err_list[:5],
            "has_visual_card": bool(card),
            "segments_present": sorted((doc or {}).get("segments", {}).keys())
            if isinstance((doc or {}).get("segments"), dict) else [],
        }, ensure_ascii=False)
        txt = chat([
            {"role": "system", "content": _DECISION_SYSTEM},
            {"role": "user", "content": prompt},
        ], temperature=0.0, max_tokens=200, timeout=20,
            response_format={"type": "json_object"})
        m = re.search(r"\{.*\}", str(txt or ""), re.S)
        data = json.loads(m.group(0)) if m else {}
        action = str(data.get("action") or "").strip()
        valid = {"code_repair", "normalize", "recompile",
                 "creative_regen", "give_up"}
        if action in valid:
            return {"action": action,
                    "reason": str(data.get("reason") or "模型判断")[:60],
                    "confidence": "medium", "source": "model"}
        logger.info("决策模型给出未知 action=%r，按默认处置", action)
    except Exception as e:                                       # noqa: BLE001
        logger.warning("处置决策调用失败（降级为原路径）：%s", e)

    # ③ 兜底：模型也没给出可用判断 → 走原来的重修
    return {"action": "recompile", "reason": "无法明确分类，按原路径重修",
            "confidence": "low", "source": "fallback"}


__all__ = ["classify_and_route"]