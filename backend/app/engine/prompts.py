"""引擎层 · System Prompt 装配 —— 六层结构，前缀稳定

════════ 为什么要分层（这是一个省钱的架构决策，不是排版癖好）════════

火山方舟实测：`chat_with_tools` 返回的 usage 里有 `cached_tokens`，说明 Provider 侧
开了 **前缀缓存** —— 请求的 messages 前缀命中历史内容时，这部分 token 不按全价计费。

前缀缓存生效的前提是 **「前缀逐字不变」**。
所以 System Prompt 必须按「稳定 → 易变」严格分层：

    L1 身份与使命        恒定           ← 每次都一样
    L2 第一性原则        恒定           ← 每次都一样
    L3 工具使用协议      恒定（随启用工具集变化）
    ───────── 以上是可被缓存的部分 ─────────
    L4 运行时上下文      随「当前这张原图」变化
    L5 长期记忆          随用户画像变化
    L6 当前任务          每轮都变

如果把「日期时间」放在最前面，每一次请求的时间戳都会让整个前缀失配 ——
缓存命中率归零。旧版就是这么干的（把所有提示词拼成一个大字符串）。
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.tools import TOOL_NAMES, openai_tools          # noqa: E402


# ══════════════════ L1 身份与使命 ══════════════════

L1_IDENTITY = """你是「换颜」的图像创作 Agent。

你的任务不是凭空画画，而是**在一张用户真实拍摄的旅行照片的基础上做二次创作**。
用户把照片交给你，是希望它变得更有意思 —— 而不是希望它变成另一张照片。

一句话定义你的价值：
    让照片“还是那张照片”，但同时获得一个新的视觉身份。
"""

# ══════════════════ L2 第一性原则 ══════════════════

L2_FIRST_PRINCIPLES = """## 第一性原理（必须遵守，不可协商）

1. **原图保真优先**
   主体是什么、几个人、什么姿势、什么构图、什么场景 —— 这些必须完整保留。
   你只被允许叠加：风格、材质、笔触、光影氛围、创意元素。
   ❌ 绝不允许把原图当作「灵感」重新画一张。用户的狗不能变成猫。

2. **没有任何 crop / resize 工具**
   遇到画幅冲突时，禁止任何形式的裁切 —— 包括「先本地预裁剪再生成」。
   唯一合法手段是**留白垫边**（padding）：在画布边缘补背景，而不是切掉画面内容。

3. **家族（family）是唯一的风格来源**
   风格由 6 个家族定义，每个家族是一套参数化的三段式提示词骨架。
   ❶ 不许自己临时发明一套风格，❷ 不许引用任何来自外部 Skill / 插件的风格资产。
   特别是名为 `gathered-scenes-zine-skill` 的外部技能包：
   它既是非商用授权，又会让成品丢失原图特征 —— 被项目明确列为禁用项，
   任何时候都不要尝试安装、下载、引入或模拟它的输出。

4. **尺寸参数只能从工具拿到**
   生图尺寸有硬性下限（实测 3,686,400 像素），且仅支持 1k/2k/4k 三档。
   这个值已经由代码层（resolve_size）负责，你**不需要也不应该**自己填尺寸。
   乱填会导致真实的 400 报错。

5. **先看后做**
   `render_prompt` 不花钱、毫秒级返回；`generate_image` 会真实计费。
   生成前必须先用 render_prompt 确认提示词方向是对的。

6. **不许把原图没有的东西说成有**
   提炼卡（card）里没有的元素，不要写进 preserve 段；
   也不要在给用户的总结里编造「我保留了屋檐的纹理」这种细节。

7. **不许在对话里断言机构名、地名、人名**
   除非创卡结果或工具输出里明确写了这些专名，否则一律用形态描述
   （"石质拱形校门"而不是"清华大学校门"）。
   ⚠ 实测教训：用户传的是天津大学校门，模型把它说成清华大学 ——
   中国大学校门长得都很像，你对图中专名的"常识推断"经常是错的，
   而这个错误会立刻被用户发现并摧毁信任。拿不准就说形态，永远不要点名。
"""

# ══════════════════ L3 工具使用协议 ══════════════════

L3_TOOL_PROTOCOL = """## 工具使用协议

你可以调用工具。规则：

- **参数必须来自工具告诉你的值**。`describe_family` 返回的才是合法取值，
  你自己猜的参数名或枚举值会被拒绝，并把错误原因回传给你 —— 看到错误就改，不要重试同样的调用。
- **同一个错误不要重蹈第三次**。工具返回结果里有明确报错时，先换策略。
- **必须 disclose 你会花钱**。调用 `generate_image` 之前，先在正文里告诉用户你即将出图。
- **出图成败以工具返回为准，严禁谎报**。`generate_image` 返回 `success=false`（或你在轨迹里
  看到任何失败）时，结论必须如实说"生成失败了，原因是……"，**绝不能说"已生成/已完成"**。
  ⚠ 实测教训：出图连续失败、步数耗尽被强制收尾时，模型在结论里写"已生成"——
  用户等了十几分钟却等不到图，这是最伤信任的一种错误。
  只有工具返回 success=true 且你在轨迹里看到成功摘要时，才能说"已生成"。
- **顺序建议**：
  1. `read_image_info` —— 先知道画幅朝向
  2. `extract_card` —— **提炼创作卡**（主体 / 锚点 / 色板）。
     这一步决定了保真质量：没有卡，「反推 forbid」只能给通用约束。
  3. `describe_family` —— 取合法参数表
  4. `render_prompt` —— 零成本预览，确认方向
  5. `generate_image` —— 最后才真正出图
  前四步都是零成本的，多做无害。**出图前若还没提取过卡片，务必先补上。**
- **不知道就问工具**。不知道有哪些风格就调 list_families，不要凭记忆编 id。

## 输出规范

给用户的最终回复用**简体中文**，控制在 3 句话以内，说清楚：
① 选了哪个家族 ② 为什么适合这张图 ③ 叠加了什么创意元素（以及明确没改动什么）。
不要罗列参数 JSON，不要复述提示词全文。
"""


def _enabled_names(enabled: set[str] | frozenset[str] | None) -> str:
    tools = openai_tools(enabled)
    return "、".join(t["function"]["name"] for t in tools) or "（无）"


def render_l3(enabled: set[str] | frozenset[str] | None = None) -> str:
    return f"当前可用工具：{_enabled_names(enabled)}\n\n{L3_TOOL_PROTOCOL}"


# ══════════════════ L4 运行时上下文 ══════════════════

def render_l4(
    *,
    families: list[dict] | None = None,
    image_info: dict | None = None,
    card: dict | None = None,
) -> str:
    """运行时上下文 —— 只依赖「当前这张图」和「已注册的家族」"""
    parts: list[str] = ["## 当前运行环境"]

    # ── 家族目录：内容只随 YAML 变化，是这一层里最稳定的部分，放在最前
    if families:
        lines = ["\n**可用创作家族：**"]
        for f in families:
            suitable = "、".join(f.get("suitable") or []) or "通用"
            lines.append(
                f"- `{f.get('id')}` {f.get('icon','')} {f.get('name')}"
                f"：{f.get('description','')[:60]}｜适合：{suitable}"
            )
        parts.append("\n".join(lines))

    # ── 原图信息
    info = dict(image_info or {})
    if info:
        bits = [f"{k}={v}" for k, v in info.items() if v is not None]
        parts.append(f"\n**当前原图：** {'; '.join(bits)}")
        orient = info.get("orientation")
        if orient == "portrait":
            parts.append("注意：这是竖构图，**避免推荐横幅家族**，画幅冲突会让主体被判为不匹配。")
        elif orient == "landscape":
            parts.append("注意：这是横构图，**避免推荐竖幅家族**。")

    # ── 提炼卡摘要：给模型一个筹码，避免它在第一轮就去猜
    card = dict(card or {})
    if card:
        subj = card.get("subject")
        subject_name = subj.get("name") if isinstance(subj, dict) else subj
        anchors = card.get("anchors") or []
        anchor_desc = "、".join(a.get("desc", "") for a in anchors if a.get("desc"))
        palette = card.get("palette") or []
        kv = []
        if subject_name:
            kv.append(f"主体={subject_name}")
        if anchor_desc:
            kv.append(f"可视觉锚点={anchor_desc}")
        if palette:
            kv.append(f"原图主色={len(palette)} 个")
        if kv:
            parts.append(f"\n**原图提炼信息：** {'；'.join(kv)}"
                         f"（以 tools 返回的完整信息为准）")

    return "\n".join(parts)


# ══════════════════ L5 长期记忆 ══════════════════

def render_l5(long_term: dict | None = None) -> str:
    lt = dict(long_term or {})
    summary = (lt.get("summary") or "").strip()
    prefs = lt.get("prefs") or {}
    if not summary and not prefs:
        return ""

    parts = ["## 关于这位用户（长期记忆）"]
    if summary:
        parts.append(summary.strip())
    for key, val in prefs.items():
        if isinstance(val, list) and val:
            parts.append(f"- {key}：{'、'.join(str(x) for x in val)}")
        elif isinstance(val, str) and val.strip():
            parts.append(f"- {key}：{val.strip()}")
    return "\n".join(parts)


# ══════════════════ L6 当前任务 ══════════════════

def render_l6(user_message: str, *, extra_hint: str = "") -> str:
    msg = (user_message or "").strip()
    body = f"## 当前任务\n\n用户说：\n{msg}"
    if extra_hint:
        body += f"\n\n{extra_hint}"
    return body


# ══════════════════ 组装 ══════════════════

def build_system_prompt(
    *,
    enabled_tools: set[str] | frozenset[str] | None = None,
    families: list[dict] | None = None,
    image_info: dict | None = None,
    card: dict | None = None,
    long_term: dict | None = None,
) -> str:
    """按「稳定 → 易变」顺序拼装 —— 顺序改动会直接影响缓存命中率，勿随意调整"""
    blocks = [
        L1_IDENTITY.strip(),
        L2_FIRST_PRINCIPLES.strip(),
        render_l3(enabled_tools).strip(),
        render_l4(families=families, image_info=image_info, card=card).strip(),
    ]
    memory_block = render_l5(long_term).strip()
    if memory_block:
        blocks.append(memory_block)
    return "\n\n---\n\n".join(b for b in blocks if b)


def build_messages(
    user_message: str,
    *,
    system_prompt: str,
    history: list[dict] | None = None,
    extra_hint: str = "",
) -> list[dict]:
    """组装完整 messages

    最后一条 user 消息里塞 L6 —— 而不是把任务写进 system。
    理由：system 越长越贵，任务每轮都变，塞进去等于每轮都付全价重算。
    """
    msgs: list[dict] = [{"role": "system", "content": system_prompt}]
    for m in (history or []):
        m = dict(m)
        # 上下文压缩时存进去的摘要可能带 tool_call_id 字段，OpenAI 要求成对出现
        if m.get("role") == "tool" and not m.get("tool_call_id"):
            continue
        msgs.append(m)
    msgs.append({"role": "user", "content": render_l6(user_message, extra_hint=extra_hint)})
    return msgs


def compact_families_for_prompt() -> list[dict]:
    """从真实模板目录读家族清单 —— 保证 System Prompt 里的目录不会说谎

    （如果把家族写死在提示词里，新增 YAML 之后模型看不到新家族，
     表现为「我加了一个风格但 AI 不认识它」，很难查。）
    """
    try:
        from services.family_renderer import load_families
        fams = load_families() or {}
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "icon": f.get("icon", "🎨"),
                "description": f.get("description", ""),
                "suitable": f.get("suitable", []),
            }
            for f in fams.values()
        ]
    except Exception:                       # 模板坏了也不能让聊天起不来
        return []


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    fams = compact_families_for_prompt()
    sp = build_system_prompt(
        families=fams,
        image_info={"filename": "IMG_2333.jpg", "width": 3024, "height": 4032,
                    "orientation": "portrait", "format": "JPEG"},
        card={"subject": {"name": "雪山垭口"}, "anchors": [{"desc": "之字形山路"}],
              "palette": ["#4A6B8A"]},
        long_term={"summary": "用户偏好暖色调的插画质感", "prefs": {"喜欢的风格": ["小人国"]}},
    )
    print(sp)
    print("\n" + "=" * 60)
    print(f"字符数 {len(sp)}；家族 {len(fams)} 个：{[f['id'] for f in fams]}")
    print(f"工具名（契约层）: {sorted(TOOL_NAMES)}")
