"""工坊流水线 · 阶段函数（2026-10-07 从 `style_forge.forge()` 拆出）

════ 为什么拆 ════
`forge()` 曾是全项目最长的函数（306 行），七个流程阶段挤在一个函数体里：
证据 → 逐图解构 → 视觉卡合成 → 意图识别 → 编译 → QC/自修 → 收尾。
它同时承载**三道预算闸**（阶段超时 / 墙钟预算 / 阶段配额）——
这三道闸是 2026-10-07 端到端实测才补上的，动错一行就是"用户等三分钟什么都没拿到"。

所以这里按**流程阶段**切，不按"逻辑类别"切：
每个阶段函数只做一件事，参数全部显式传，跨阶段共享的状态挂在 `ForgeCtx` 上。

════ 三道闸为什么必须挂在同一个对象上 ════
1. **墙钟预算只有一个 deadline**：在 `forge()` 里 arm 一次（`ctx.arm(...)`），
   之后每个阶段问的都是同一个 `ctx.budget_left()`。
   ★ 本次拆分最大的风险点就在这里：如果哪个阶段自己`time.time() + 预算` 起算，
   预算就被重置了，硬闸形同虚设。
2. **阶段配额**：`ctx.decode_budget()` 按**剩余**预算 × `DECODE_SHARE` 给解构
   划额度，保证编译（唯一**不可降级**的阶段，没有它就没有草稿）一定有饭。
3. **中止语义**：`ctx.stop()` 同时管「用户中止」和「预算到点」，但两者
   **处理方式不同** —— 用户中止返回 `cancelled`；预算到点要**保留已编译的草稿**
   并如实写进 `warnings`。所以 `budget_hit` 是 ctx 上的字段而不是返回值：
   只有 `forge()` 有资格决定这一轮按哪种方式收尾。

════ 依赖纪律（与 `forge_spec_guard` 同一份，但方向相反）════
`style_forge` 在**模块加载期** import 本模块（拿 `ForgeCtx` 与各阶段函数），
所以本模块**绝不能**在模块加载期 import `style_forge` —— 那样直接成环。
各阶段函数在**调用时**才`from services import style_forge`：
既避开循环导入，也保住"测试改 style_forge 的模块属性、阶段函数立刻看得见"
这个既有行为（延迟取属性，而不是 import 时把名字绑死）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (                                          # noqa: E402
    FORGE_REPAIR_TIMEOUT_SEC,
    FORGE_TOTAL_BUDGET_SEC,
    FORGE_VLM_TIMEOUT_SEC,
)
from infra.logging import logger, step                        # noqa: E402
from services.llm import LLMError, chat, extract_json         # noqa: E402
# ★ 自修处置决策：让模型判断「该修还是该重做」——
#   这是本项目里 Agent 承担不可替代判断的位置，详见该模块的docstring。
from services.repair_decision import classify_and_route     # noqa: E402


# ★ 阶段配额：解构最多只能吃掉总预算的一部分，**剩下的必须留给编译**。
#   为什么（2026-10-07 端到端实测）：上游慢的时候，解构会把 360s 里的
#   大头花光，等编译开始时 `budget_left()` 已经很薄，
#   编译被硬闸掐掉 → 用户等了 3 分半，**什么都没拿到**。
#   而编译是**唯一不可降级**的阶段（没有它就没有草稿），
#   所以它必须**预留**额度，而不是和其他阶段先到先得。
DECODE_SHARE = 0.55        # 解构最多用掉 55%，至少留 45% 给编译


@dataclass
class ForgeCtx:
    """一次 `forge()` 调用的**唯一**共享上下文

    ★ 为什么要显式传对象，而不是模块级全局变量：
      全局变量在并发两个提炼请求时会互相串台（这是 `run()` 里 on_event
      刻意不做猴子补丁的同一条纪律），也让阶段函数无法离线测试。
    ★ 为什么 `deadline` 只能arm 一次：
      见模块头注「墙钟预算只有一个 deadline」。
    """

    cancelled: Callable[[], bool] | None = None
    progress: Callable[[str], None] | None = None
    started: float = 0.0
    deadline: float | None = None
    budget_hit: bool = False
    decode_share: float = DECODE_SHARE

    # ── 预算闸（三道闸的公共部分都在这里）────────────────────

    def arm(self, started: float, deadline: float | None) -> None:
        """记录起点与硬闸deadline。**全流程只该被调用一次**。"""
        self.started = started
        self.deadline = deadline

    def budget_left(self) -> float:
        """整条链路还剩多少秒（inf = 不设硬闸）"""
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.time())

    def decode_budget(self) -> float:
        """解构这一轮允许花多少（含重试）—— 阶段配额闸"""
        left = self.budget_left()
        if left == float("inf"):
            return float(FORGE_VLM_TIMEOUT_SEC)
        return max(30.0, min(FORGE_VLM_TIMEOUT_SEC, left * self.decode_share))

    def stop(self) -> bool:
        """全流程**唯一**的中止探针：用户中止 or 预算到点

        ★ 为什么整条链路硬闸接在这里而不是各阶段各判一次：
          `stop()` 是全流程唯一的中止探针（解构前后、合成前、编译前、
          自修轮都会问），接在一处 = 改一处覆盖所有阶段，不会漏。
        ★ 注意它只回答"要不要停"，**不决定怎么收尾**：
          用户中止 → cancelled；预算到点 → 保留草稿 + 写 warnings。
          那个区别由`forge()` 依据 `budget_hit` 做出。
        """
        try:
            if self.cancelled and self.cancelled():
                return True
        except Exception:
            pass
        # ★ 整条链路硬闸（2026-10-07）：到点就当"中止"处理。
        if self.deadline is not None and time.time() > self.deadline:
            self.budget_hit = True
            logger.warning("提炼超过总预算 %.0fs，提前收尾（已产出的草稿会保留）",
                           FORGE_TOTAL_BUDGET_SEC)
            return True
        return False

    # ── 进度上报 ────────────────────────────────────────────

    def report(self, msg: str) -> None:
        """进度回调 —— 回调自身的异常一律吞掉（进度上报绝不能弄死提炼）"""
        if self.progress:
            try:
                self.progress(msg)
            except Exception:
                pass


# ══════════════════ 阶段 ① 证据 ═══════════════════


def stage_evidence(ctx: ForgeCtx, paths: list[str]) -> tuple[list[dict], list[str]]:
    """参考图 → 文本证据

    ★ 证据一律走本地客观测量（色板/明度/朝向，便宜且真实）：
      看图只发生在解构阶段 —— 之前两个阶段各看一次，配了视觉模型后
      等于每张图看两遍。
    """
    from services.style_forge import _reference_evidence

    ctx.report(f"分析 {len(paths)} 张参考图")
    return _reference_evidence(paths, False)


# ══════════════════ 阶段 ③ 视觉卡合成 ═══════════════════


def stage_card(ctx: ForgeCtx, decodes: list[dict], theory: str,
               evidence: list[dict]) -> dict:
    """多份解构 → 一张视觉卡（纯代码合并，不调模型）"""
    from services.style_forge import _synthesize

    ctx.report("合成视觉卡")
    if decodes or (theory or "").strip():
        return _synthesize(decodes, theory, evidence)
    return {}


# ══════════════════ 阶段 ④ 意图识别 ═══════════════════


def stage_intent(ctx: ForgeCtx, intent: str, style_prompt: str,
                 user_notes: str) -> tuple[dict, dict, int]:
    """轻量意图解析（并行两连）：世界观参照 + 正向要求

    返回 (world, preq, intent_calls)。两者都是低温度小调用，
    失败各自静默降级为空 —— 绝不阻塞主链。
    """
    from services.style_forge import _detect_world, _parse_positive_requirements

    ctx.report("识别用户意图")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fw = pool.submit(_detect_world, intent)
        fp = pool.submit(_parse_positive_requirements, style_prompt, user_notes)
        world = fw.result()
        preq = fp.result()
    if world:
        ctx.report(f"按「{world['world_name']}」世界观编译")
    intent_calls = (1 if world else 0) + (1 if (preq.get("keywords") or preq.get("removals")
                                                or preq.get("preserves")) else 0)
    return world, preq, intent_calls


# ══════════════════ 阶段 ⑤ 编译 ═══════════════════


def stage_compile(ctx: ForgeCtx, *, card: dict, intent: str, base_family: dict | None,
                  style_prompt: str, world: dict, preq: dict, paths: list[str],
                  evidence: list[dict], warnings: list[str]) -> tuple[dict, dict | None]:
    """视觉卡 + 意图 → 家族 JSON，再归一化 + 前置调和

    返回 `(doc, terminal)`：`terminal` 非None 表示这一阶段已经终结，
    调用方直接原样返回它（不抛业务异常 —— forge 永不抛）。
    """
    from services.style_forge import (
        _compile,
        _normalize,
        _reconcile_dangling_placeholders,
    )

    doc: dict = {}
    try:
        ctx.report("编译家族模板")
        with step("编译家族模板", images=len(paths), has_card=bool(card)):
            doc = _compile(card, intent, base_family, style_prompt,
                           world=world or None, positive=preq or None,
                           budget_left=ctx.budget_left())
    except LLMError as e:
        return {}, {"ok": False, "error": f"模型调用失败：{e}",
                    "evidence": evidence, "card": card}

    if not isinstance(doc, dict) or not doc:
        return {}, {"ok": False, "error": "模型没有返回可解析的家族 JSON",
                    "evidence": evidence, "card": card}
    # ★ 归一化不得让整条流水线归零（2026-10-07 端到端实测）：
    #   _normalize 里任何一处对模型输出形状的假设不成立，都会以
    #   AttributeError/TypeError 的形式冒到顶，**把已经花掉的 170 秒
    #   与几十次调用全部作废**，而用户看到的只是"提炼失败"。
    #   归一化的目的是"把脏形状擦干净"，它自己必须是**永不抛**的那一层。
    try:
        doc = _normalize(doc)
        # ★ 前置调和：摘掉「引用了未声明参数」的占位符（2026-10-07 端到端实测）。
        #   放在归一化之后、QC 之前 —— 这类错误是**机械可修**的，
        #   让它触发"整份草稿被拒 + 再花 70s 让模型重写"是最贵的修法。
        #   摘了什么会写进 warnings，如实告诉用户，不静悄悄。
        dangling = _reconcile_dangling_placeholders(doc)
        if dangling:
            warnings.append(
                "模板里有几处占位符引用了未声明的参数（" + "、".join(dangling)
                + "），已移除以免渲染成空串；如需保留，请在参数面板里补上同名参数。")
    except Exception as e:                                    # noqa: BLE001
        logger.error("家族模板归一化失败（模型输出形状异常）：%s: %s",
                     type(e).__name__, e)
        return {}, {"ok": False,
                    "error": "模型返回的家族模板结构异常，没能整理成可用参数。"
                             "可以点「重试」再试一次，或把风格描述写得更具体些。",
                    "evidence": evidence, "card": card}
    return doc, None


# ══════════════════ 阶段 ⑥ QC / 自修 ═══════════════════


def _finalize_doc(d: dict, card: dict, preq: dict) -> tuple[dict, list[str], dict]:
    """★ 出口收敛（10-03 复发修复；10-03 晚升级语义豁免 + 出厂检验）：
    所有分支的校验前必须走同一条「正向要求保护 → 残留拦截 → 校验 → QC」流水线。
    preq 同时携带引号短语（字面精确）与语义关键词（准星/按钮等间接指代），
    保护与残留兜底共用 _pos_conflict 判定，间接反转无从漏网。
    QC（family_qc）是最后一道语义闸：画幅/正向短语存活/forbid↔creative 矛盾。

    ★ QC 仍是唯一质量门 —— 这里不做任何"为了让代码好看"的放宽。
    """
    from services.style_forge import (
        _check,
        _derive_rule_meta,
        _enforce_residue,
        _protect_positive_requirements,
    )

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


def stage_qc_repair(ctx: ForgeCtx, doc: dict, card: dict,
                    preq: dict) -> tuple[dict, list[str], dict, int]:
    """QC → 悬空占位符代码消解 → 外科自修。返回 (doc, errors, report, rounds)

    校验不过 → 外科自修（造梦师 Surgical Repair 契约的机制重写，不搬其文本）：
      最多 1 个主要失败 → 最小改动修复 → 其余逐字保留 →绝不重跑整个创作流程。
    ★ 旧版第一条消息带着含style_prompt 的完整 intent —— 用户提示词包里
      「标签:内容」的格式在自修轮继续诱导模型犯同样的错（实测 22:32 自修无效的根因）。
      外科模式下模型只面对「上一版产物 + 错误清单」，诱导源被移出上下文。
    """
    from services.style_forge import (
        MAX_REPAIR,
        _COMPILE_SYSTEM,
        _REPAIR_TMPL,
        _normalize,
        _resolve_unknown_slots,
        _strip_fence,
    )

    doc, errors, report = _finalize_doc(doc, card, preq)

    # ★ 悬空占位符先由代码消解（省一轮模型调用，也避开中转站 502）
    if errors and any("未解析占位符" in e for e in errors):
        doc, fixed = _resolve_unknown_slots(doc, card or {})
        if fixed:
            logger.info("代码消解了 %d 个模型自造的占位符，重跑校验", fixed)
            doc, errors, report = _finalize_doc(doc, card, preq)

    rounds = 0
    # ★★ Agent 决策：每轮修复前先判断「该用什么方式修」
    #   为什么这是 Agent 不可替代的位置（详见 services/repair_decision.py）：
    #   「校验失败」下面藏着性质完全不同的失败 ——
    #     占位符悬空   → 代码能修
    #     缺必填条款   → 必须重编译（修补没有意义）
    #     创意不足     → 该走创意层重生成（修结构是南猿北辙）
    #   固定顺序重试会把它们当同一件事处理，
    #   而其中至少两种**怎么重试都不会变好** ——
    #   等于白白烧调用和时间。
    #
    #   边界：决策只**分类**不**动手** —— 动手仍由确定性代码完成，
    #   避免模型自由改写破坏三段式契约。
    #   且决策失败会退化为原有重试路径（repair_decision 内部已兜底），
    #   **Agent 在这里是加速器，不是新的单点故障**。
    while errors and rounds < MAX_REPAIR and not ctx.stop():
        rounds += 1
        decision = classify_and_route(errors, card=card, doc=doc)
        act = decision.get("action")
        ctx.report(f"校验自修第 {rounds} 轮 · 处置={act}")
        logger.info("家族校验未通过，第 %d 次自修：%s（处置=%s：%s）",
                    rounds, errors[:2], act, decision.get("reason", "")[:40])

        # give_up：模型判断修不好 —— 立刻停，别烧剩余预算
        if act == "give_up":
            logger.info("决策为 give_up（%s），停止自修并如实上报剩余问题",
                        decision.get("reason", ""))
            errors = list(errors) + [
                f"[决策终止] {decision.get('reason', '判定无法修复')}"]
            break

        # code_repair / normalize：先让确定性代码试（零成本、不惊动模型）
        if act in ("code_repair", "normalize"):
            doc2, fixed2 = _resolve_unknown_slots(doc, card or {})
            if fixed2:
                logger.info("代码消解了 %d 个占位符（处置=%s），重跑校验",
                            fixed2, act)
                doc = doc2
                doc, errors, report = _finalize_doc(doc, card, preq)
                continue
            logger.info("代码兜底未消解任何项（处置=%s），降级为重编译", act)

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
                wall_budget=min(FORGE_REPAIR_TIMEOUT_SEC, ctx.budget_left()),
                response_format={"type": "json_object"})
            fixed = extract_json(_strip_fence(repair))
            if isinstance(fixed, dict) and fixed:
                doc = _normalize(fixed)
                # 自修产物同样过保护+残留+校验+QC
                doc, errors, report = _finalize_doc(doc, card, preq)
                if decision:
                    report.setdefault("repair_decisions", []).append(
                        {"round": rounds, "action": act,
                         "source": decision.get("source"),
                         "confidence": decision.get("confidence")})
        except LLMError as e:
            logger.warning("自修轮 %d 调用失败：%s", rounds, e)
            break
    return doc, errors, report, rounds


# ══════════════════ 阶段 ⑦ 收尾 ═══════════════════


def stage_finish(ctx: ForgeCtx, *, doc: dict, card: dict, decodes: list[dict],
                 evidence: list[dict], errors: list[str], report: dict,
                 warnings: list[str], rounds: int, decode_sec: float,
                 intent_calls: int) -> dict:
    """分段耗时统计 + 最终返回体（不碰任何闸门语义，收尾方式由 forge 决定）"""
    elapsed = round(time.time() - ctx.started, 2)
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


__all__ = [
    "DECODE_SHARE",
    "ForgeCtx",
    "stage_card",
    "stage_compile",
    "stage_evidence",
    "stage_finish",
    "stage_intent",
    "stage_qc_repair",
]
