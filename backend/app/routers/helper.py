"""路由 · 小助手：看图推荐适合的风格家族

★ 这个端点只做**推荐**：看图 → 对照家族清单 → 给出 2–3 个适合的风格与理由。
  不落任何文件、不改任何家族配置 —— 推荐错了没有副作用，用户可以照旧自己选。

★ 推荐质量的一半来自「知道这张图不该用哪个风格」，所以家族清单里
  同时带上 suitable（适合）与 not_suitable（不适合），两者都是硬判据。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (                                    # noqa: E402
    DEEPSEEK_MODEL,
    PROJECT_ROOT,
    VISION_MODEL,
)
# 注：VISION_BASE_URL / VISION_API_KEY / REQUEST_TIMEOUT_SEC 原先在这里
# 被用来临时 new 一个 OpenAI 客户端看图；现在统一由 services/llm.vision()
# 按通道配置取用（见 llm.py 顶部「通道」一节），本文件不再重复接线。
from infra.logging import audit, logger                 # noqa: E402
from services.llm import (                              # noqa: E402
    chat,
    chat_interactive,
    extract_json,
)   # ★ extract_json 在 llm 里
from services.template_manager import load_families     # noqa: E402

router = APIRouter(prefix="/api/helper", tags=["helper"])


class RecommendRequest(BaseModel):
    image_url: str = Field(..., min_length=1, max_length=1000)


class HelperChatRequest(BaseModel):
    messages: list[dict] = Field(default_factory=list)
    image_url: str = ""


_ADVISOR_SYSTEM = """你是「换颜」的风格顾问。

我会给你一张照片，以及一组风格家族清单。每个家族都有：
- name：风格名
- description：这个风格会把照片变成什么样
- suitable：适合的照片类型
- not_suitable：明确不适合的照片类型（硬约束）

你的任务：
1. 先真正看懂这张照片：主体是什么、场景结构（是否有道路/水岸/天际线/建筑轮廓）、
   光影、是否人像、主体能否从背景里干净剥离、有没有可"物化"的结构。
2. 从清单里挑出 **2–3 个最适合**这个家族集合里最适合这张照片的风格，
   每个推荐必须结合这张照片的实际情况说明理由（不许说套话）。
3. 若有明显不适合的家族，最多列 2 个，说明为什么排除。
4. 严格依据 suitable / not_suitable 判断 —— 被明确排除的类型不能出现在推荐里。

只返回 JSON：
{"summary": "一句话说明这张照片是什么、特点是什么",
 "recommended": [{"id": "<家族id>", "reason": "结合这张照片的具体理由"}],
 "avoided": [{"id": "<家族id>", "reason": "排除原因"}]}
"""


_CHAT_SYSTEM = """你是「换颜」这个应用**内置**的小助手，名字叫小助手，语气自然、简短、有用。

【你的职责范围 —— 严格遵守】
只回答与「换颜」这个应用有关的问题，例如：
  · 某个功能在哪里、怎么用（放原图、选风格、生成、下载、作品仓库、模板工坊、设置等）
  · 怎么给自建风格加示例图、什么时候能加
  · 这张照片适合哪个风格、为什么
  · 提炼/安装/迭代是什么意思、失败了怎么办
  · 效果不满意该怎么调整

【遇到范围外的问题 —— 必须回绝】
如果用户聊的是与应用无关的内容（闲聊、写代码、翻译、其他软件、通用知识、时事等），
不要回答内容本身，而是礼貌说明：**你只负责换颜这个应用内的问题**，
并顺手举 2–3 个你能回答的例子（比如「示例图在哪加 / 这张图适合哪个风格 / 怎么放原图」），
引导用户把问题问回到应用上。

【回答规则】
1. 只依据下面提供的「应用说明」和「风格清单」回答，不要编造应用里不存在的功能或按钮位置。
2. 说位置要说人话（顶栏、左侧列表、画布下方、工坊里……），不要提任何技术实现细节
   （不说内部文件、接口、参数结构、存储目录、模型名等）。
3. 涉及风格的判断必须依据清单里的「适合 / 不适合」，不要凭印象推荐。
4. 回答尽量简短：能三句话说完就不要写一大段；需要步骤时再用 1–4 的编号列出。
5. 用户中文提问就用中文回答。

──── 应用说明（唯一真源：项目根目录的「使用说明.md」）────
{app_doc}

──── 当前可用风格家族清单 ────
{families}
"""


def _app_doc() -> str:
    """读项目根目录的使用说明.md —— 让文档成为唯一真源，
    改文档即改助手知识，不在这里复制第二份。"""
    try:
        p = Path(PROJECT_ROOT) / "使用说明.md"
        if p.exists():
            return p.read_text(encoding="utf-8")[:6000]
    except Exception as e:
        logger.warning("小助手读取应用说明失败：%s", e)
    return "（应用说明暂不可用，请依据风格清单回答，并提示用户查看应用内的功能讲解）"


# ── 知识索引（渐进披露）：使用说明.md 按「## 节 / ### 子节」切开，每节配一张"身份证"
#   （关键词表）。回答时只注入 **命中提问关键词的 1–2 节** ——
#   system 从 ~8000 字降到 ~2500 字，回答明显更快；文档仍是唯一真源。
#
#   ★ 2026-10-05 颗粒度对齐：原先只按「## 节」切，而「六、模板工坊」整节 1874 字
#     超过单节 900 字注入上限 —— 后半段的「安装为家族 / 设置示例图 / 世界观参照」
#     被截掉，助手就会回答「没有这个功能」。现在按两级切：命中哪一小节就注入哪一
#     小节，既不再被截断，还更省 token（注入 1–2 小节而不是整章）。
_DOC_SECTION_KW = {
    "一、快速开始（三步上手）": ["快速", "开始", "怎么用", "上手", "流程", "第一次"],
    "二、画布：放原图": ["原图", "上传", "拖拽", "粘贴", "ctrl", "画布", "放图", "照片", "替换"],
    "三、选风格与调参数": ["风格", "家族", "参数", "选风格", "调参", "必需", "换风格", "示例"],
    "四、生成与成品": ["生成", "成品", "下载", "终止", "微调", "不满意", "进度", "慢",
                # ★ 局部修复（2026-10-05 新增能力）：不登记这些词，用户问「怎么修」
                #   会命中不到任何节，助手只能给概览 —— 功能上线了却问不出来。
                "修复", "局部修复", "改一处", "修一处", "重画", "回到上一版", "上一版",
                "找问题", "诊断", "漂移", "哪里不对"],
    "五、作品仓库": ["仓库", "历史", "打包", "zip", "作品", "找回"],
    "六、模板工坊：做出你自己的风格": ["工坊", "提炼", "参考图", "风格理论", "提示词", "新风格",
                                "多任务", "并行", "迭代", "安装", "移除", "失败", "入库", "我的库",
                                "示例图", "世界观", "中止"],
    "七、小助手 🧭": ["小助手"],
    "八、设置": ["设置", "背景图", "主题", "配色", "外观", "背景", "背景色"],
    "九、状态与额度": ["额度", "状态", "次数", "就绪", "花额度", "够不够", "张数"],
    "十、「换背景」到底指哪一种？": ["换背景", "背景图"],
    "十一、常见问题": ["失败", "灰", "不行", "怎么办", "报错", "点不了", "为什么", "商用",
                 "区别", "要花", "能修好几处吗"],
}

# 子节关键词补录：父节的关键词表覆盖不到的行话
_DOC_SUBSECTION_KW = {
    "4. 安装为家族": ["安装", "变成家族", "装成", "装为", "用自己做的风格", "自建风格", "家族"],
    "5. 给自建风格配示例图": ["示例图", "封面", "配图"],
    "7. 世界观参照": ["世界观", "我的世界", "星露谷", "参照"],
    "2. 并行与提醒": ["并行", "同时", "中止", "停"],
    "3. 查看与迭代": ["迭代", "修改意见", "复制提示词"],
}


def _doc_keywords(label: str) -> list[str]:
    """某节（可能是「父节 · 子节」）的关键词 = 父节词表 + 子节词表 + 子节标题里的词"""
    h2, _, sub = label.partition(" · ")
    kws = list(_DOC_SECTION_KW.get(h2, []))
    if sub:
        kws += list(_DOC_SUBSECTION_KW.get(sub, []))
        # 子节标题本身拆词（去数字编号与标点），让"安装为家族"这类标题自带可命中词
        for tok in re.split(r"[\s、/·（）()【】\[\]：:.,，。0-9]+", sub):
            if len(tok) >= 2:
                kws.append(tok)
    return kws


def _split_doc(doc: str) -> list[tuple[str, str]]:
    """把使用说明按 ## 与 ### 两级切成 (标签, 正文)"""
    entries: list[tuple[str, str]] = []
    h2 = ""
    cur_title, cur_buf = "", []

    def flush() -> None:
        if cur_title:
            entries.append((cur_title, "\n".join(cur_buf).strip()))

    for line in doc.splitlines():
        if line.startswith("## "):
            flush()
            h2 = line[3:].strip()
            cur_title, cur_buf = h2, []
        elif line.startswith("### "):
            flush()
            cur_title, cur_buf = f"{h2} · {line[4:].strip()}", []
        elif cur_title:
            cur_buf.append(line)
    flush()
    return entries


def _read_doc() -> str:
    try:
        p = Path(PROJECT_ROOT) / "使用说明.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("小助手读取应用说明失败：%s", e)
    return ""


def _pick_sections(question: str) -> str:
    """按用户提问匹配最相关的 1–3 节；没命中给全部节的首段概览"""
    doc = _read_doc()
    if not doc:
        return ""
    sections = _split_doc(doc)

    q = (question or "").lower()
    scored = []
    for title, body in sections:
        kws = _doc_keywords(title)
        t_l = title.lower()
        # ★ 记分只看「提问命中了什么」，不看节标题自匹配 ——
        #   旧写法 `3×(关键词出现在节标题)` 让标题里自带"风格/参数"的第三节
        #   恒得 6 分，无论用户问什么都是它排第一（实测：问"怎么修复"取到的也是
        #   第三节），渐进披露等于失效。现在：命中提问 +2，若该词同时是本节主题
        #   词（出现在标题里）再 +1 —— 主题相关性只做加号，不做入场券。
        score = 0
        for k in kws:
            kl = k.lower()
            if kl and kl in q:
                score += 2 + (1 if kl in t_l else 0)
        if score > 0:
            scored.append((score, title, body))
    scored.sort(key=lambda x: -x[0])

    # ★ 准确度优先的分档策略：
    #   强命中（≥3）→ 只给 top2 节全文（又快又准）；
    #   弱命中（1–2）→ top3 节（多带一节防漏）；
    #   零命中（措辞完全不在关键词表）→ 注入**全部节的首段概览**（每节 260 字），
    #   让模型至少看到所有主题的全貌，宁可慢一点也不答错。
    if not scored:
        # ★ 概览也有预算：两级切分后条目变多了，全量铺开会把 system 撑回几千字，
        #   等于把"渐进披露"白做。按 3000 字封顶，够看清全貌又不失控。
        parts, budget = [], 3000
        for t, b in sections:
            if budget <= 0:
                break
            seg = f"【{t}】\n{b[:260]}"
            parts.append(seg)
            budget -= len(seg)
        return ("（以下为全部功能主题的概览——用户的问题措辞没有直接命中任何主题，"
                "请依据这些概览综合判断；概览不够就如实说明并建议用户换个说法）\n\n"
                + "\n\n".join(parts))
    # 强命中（≥6：至少两个主题词落在提问里）→ top2 就够；
    # 否则 top3 多带一节防漏。
    take = 2 if scored[0][0] >= 6 else 3
    picked = scored[:take]
    return "\n\n".join(f"【{t}】\n{b[:900]}" for _, t, b in picked)


@router.post("/chat", summary="小助手对话（仅限本应用相关问题）")
async def helper_chat(req: HelperChatRequest) -> dict:
    """对话式问答：可以打字问，也可以带一张图问

    ★ 范围硬约束在 _CHAT_SYSTEM 里：只答应用内问题，范围外礼貌回绝并引导回来。
    ★ 带图走视觉通道（看得见照片才能回答"这张图适合哪个风格"）；
      不带图走轻量对话通道，省成本也更快。
    """
    msgs = [m for m in (req.messages or [])
            if isinstance(m, dict) and m.get("content")]
    if not msgs:
        raise HTTPException(422, "没有提问内容")

    history = [{"role": "assistant" if m.get("role") == "assistant" else "user",
                "content": str(m.get("content"))[:2000]} for m in msgs[-8:]]

    # 最近一条用户消息（图片挂在它上面；知识节选择也以它为准）
    last = history[-1]
    if last.get("role") != "user":
        raise HTTPException(422, "最后一条消息不是用户提问")

    system = _CHAT_SYSTEM.format(
        app_doc=_pick_sections(last["content"]),
        families=json.dumps(_family_catalog(), ensure_ascii=False),
    )

    # ── 带图：先用已验证的看图分析拿到结论，再把结论当文本交给对话模型 ──
    #   （实测：对话请求直接带图会把进程打挂；这条两段式路径两边都稳）
    image_url = (req.image_url or "").strip()
    vision_ctx = ""
    with_image = False
    if image_url:
        from agents.image_agent import AgentInputError, normalize_reference

        try:
            path = normalize_reference(image_url)
        except AgentInputError as e:
            raise HTTPException(400, f"图片不在允许的目录内：{e}") from e
        if not path or not os.path.exists(path):
            raise HTTPException(404, "图片不存在")
        got, degraded = _analyze_image(path)
        with_image = not degraded
        vision_ctx = (
            "\n\n──── 用户附上的照片，系统已分析（可直接引用；没看懂就如实说）────\n"
            f"看懂了什么：{got.get('summary') or '（未能识别）'}\n"
            f"适合的风格：{json.dumps(got.get('recommended') or [], ensure_ascii=False)}\n"
            f"不适合的风格：{json.dumps(got.get('avoided') or [], ensure_ascii=False)}\n"
            + ("（看图能力未启用，以上是按风格清单给的通用建议）" if degraded else "")
        )

    system_with_ctx = system + vision_ctx
    try:
        # ★ 2026-10-06：改走 chat_interactive（总预算 60s + 空响应立刻换通道）。
        #   原先用 chat() 等于套了批量流水线的档位（单次 180s、主通道排 3 次），
        #   实测一次问答烧到 30.8s，其中 12s 花在明知会空的重试上。
        reply = chat_interactive(
            [{"role": "system", "content": system_with_ctx}, *history],
            temperature=0.4, max_tokens=500)
        audit("helper_chat", with_image=with_image, model=DEEPSEEK_MODEL)
        return {"ok": True, "reply": (reply or "").strip(),
                "model": DEEPSEEK_MODEL, "with_image": with_image}
    except Exception as e:
        logger.warning("小助手对话失败：%s", e)
        raise HTTPException(502, f"小助手暂时不可用：{e}") from e


def _family_catalog() -> list[dict]:
    """给模型看的家族清单：只要推荐需要的字段（不含提示词正文，省 token）"""
    out = []
    for f in load_families():
        out.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "description": str(f.get("description") or "")[:120],
            "suitable": [str(x) for x in (f.get("suitable") or [])][:5],
            "not_suitable": [str(x) for x in (f.get("not_suitable") or [])][:3],
        })
    return out


def _analyze_image(path: str) -> tuple[dict, bool]:
    """看图 → 推荐结果（{summary, recommended, avoided}, degraded）

    ★ 与 /recommend 端点共用这一份实现。对话路径也只走这里拿"看图结论"，
      再把结论当文本交给对话模型 —— 实测教训（2026-10-02）：让对话请求
      直接携带图片曾把后端进程打挂，而这条看图路径在 recommend 上一直是稳的。
    """
    catalog = _family_catalog()
    payload = {"families": catalog}

    if not VISION_MODEL:
        try:
            raw = chat_interactive(
                [{"role": "system", "content": _ADVISOR_SYSTEM},
                 {"role": "user", "content": json.dumps(
                     {**payload, "note": "（未能看到图片，请仅依据清单给出通用建议）"},
                     ensure_ascii=False)}],
                temperature=0.4, max_tokens=700,
                response_format={"type": "json_object"})
            return extract_json(raw) or {}, True
        except Exception as e:
            logger.warning("小助手降级推荐失败：%s", e)
            return {}, True

    try:
        from services.card_extractor import _prepare_for_vlm

        b64, mime = _prepare_for_vlm(path)
    except Exception as e:
        logger.warning("小助手读图失败：%s", e)
        return {}, True

    # ★ 与 card_extractor / style_forge 共用同一份看图容错（services/llm.vision）
    from services.llm import HELPER_VISION_TIMEOUT_SEC, vision

    content = [
        {"type": "text", "text": _ADVISOR_SYSTEM + "\n\n家族清单：\n"
         + json.dumps(payload, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    try:
        # ★ 2026-10-06：看图也按「人等得住」定档。vision 默认用全局
        #   REQUEST_TIMEOUT_SEC=180s，超时最多试 2 次 —— 最坏 6 分钟，
        #   那是给批量提炼定的。实测一次看图约 7s，30s 已是 4 倍余量。
        got = extract_json(vision(
            [{"role": "user", "content": content}],
            max_tokens=900,
            temperature=0.4,
            response_format={"type": "json_object"},
            timeout=HELPER_VISION_TIMEOUT_SEC,
        )) or {}
    except Exception as e:
        logger.warning("小助手看图失败：%s", e)
        raise HTTPException(502, f"看图服务暂时不可用：{e}") from e

    known = {f["id"] for f in catalog}
    for key in ("recommended", "avoided"):
        got[key] = [x for x in (got.get(key) or [])
                    if isinstance(x, dict) and x.get("id") in known]
    return got, False


@router.post("/recommend", summary="看图推荐适合的风格家族")
async def recommend(req: RecommendRequest) -> dict:
    """看图 → 推荐 2–3 个风格（带理由）+ 排除说明

    没配视觉模型时（VISION_MODEL 为空）退回**只按清单文本**推荐 ——
    这时看不到图，会如实标记 degraded，前端提示用户自行判断。
    """
    from agents.image_agent import AgentInputError, normalize_reference

    try:
        path = normalize_reference(req.image_url)
    except AgentInputError as e:
        raise HTTPException(400, f"图片不在允许的目录内：{e}") from e
    if not path or not os.path.exists(path):
        raise HTTPException(404, "图片不存在")

    got, degraded = _analyze_image(path)          # 与对话路径共用同一份看图实现
    audit("helper_recommend", families=len(_family_catalog()),
          recommended=len(got.get("recommended") or []))
    return {"ok": True, "degraded": degraded, **got}
