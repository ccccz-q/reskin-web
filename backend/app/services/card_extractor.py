"""服务层 · 提炼卡（card）提取 —— 补齐全链路唯一缺失的生产者

════════ 为什么这个文件是必须的（审查发现 P0-1）════════

`card` 是唯一贯穿全链路的数据：主体名、可视觉化锚点、原图色板、风险点。
渲染器用它算「反推 forbid」、锚点描述、主体锁定。

但在它出现之前，**没有任何东西能产出 card**：
五个 Agent 工具里没有 `extract_card`，前端恒传 `{}`。
后果不是报错，而是静默产出垃圾指令：

    zine 的 creative: "插画只提炼原图中的 0 个轮廓与路径，删除 75%-85% 的细节"
    6/6 家族的 forbid: "{dynamic_forbid}" 整行被静默删掉（行数 2→1）

也就是「反推 forbid」这条被反复强调的保真机制 100% 不生效。

════════ 两档提取，各司其职 ════════

| 档位 | 手段 | 成本 | 产出 |
|---|---|---|---|
| local | Pillow 量化取色 | **0** | palette / 尺寸 / 朝向 |
| vlm   | 视觉模型 | 一次小调用 | subject / anchors / risk_notes |

这个分层不是凑数，是为了守住项目对「预览 0 成本」的承诺：
`/api/image/preview` 和 `/api/image/render` 只跑 local 档，
拖滑杆的时候绝不会偷偷产生模型调用；VLM 档只在真正花钱的 `/generate`
或 Agent 显式调用 `extract_card` 时才走。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from functools import lru_cache
from typing import Any

from PIL import Image, ImageStat

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import REQUEST_TIMEOUT_SEC, VISION_MODEL                  # noqa: E402
from infra.logging import audit, logger, step                         # noqa: E402

# 送进 VLM 前把图缩到这个边长以内 —— 图太大只是多花 token，识别主体用不着 4K
VLM_MAX_SIDE = 768
VLM_JPEG_QUALITY = 85
MAX_PALETTE = 5
MAX_ANCHORS = 4


# ══════════════════ 色名（把 hex 变成人看得懂的中文） ══════════════════

def _color_name(r: int, g: int, b: int) -> str:
    """粗略但够用的中文色名 —— 派生提示词里写「暖米白」比写 #F2E9DC 有用得多

    判断顺序是有讲究的（踩过一次）：修饰词必须先判「浅」再判「灰」。
    实测反例 —— 暖米白 RGB(242,233,220) 的饱和度只有 0.09，
    如果先判 `s < 0.3 → 灰`，就会被叫成「灰橙」，
    而这个色恰恰是纸质/做旧基底最常出现的那一个。
    """
    import colorsys

    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    hue = h * 360

    # ── 低饱和时「色相」是没有意义的，必须优先判灰阶 ──
    # 实测反例：RGB(80,75,73) 的 delta 只有 7，算出来的 hue 落在橙区，
    # 旧逻辑会叫它「深橙」；但它实际是一块深灰褐。饱和度低于阈值时按灰处理。
    if v < 0.12:
        return "近黑"
    if s < 0.055:
        return "近白" if v > 0.88 else _gray_ladder(v)

    # 暖色系 + 高亮 + 低饱和 → 「米白」。
    # 这是纸质、墙体、沙地、旧书页这类基底最贴切的说法（RGB 242,233,220 正是它）。
    # 必须在「低饱和→灰」之前判，否则会被叫成「浅灰」。
    if 15 <= hue < 70 and v > 0.72 and s < 0.30:
        return "米白"
    # 中调暖灰带（0.72 以下）单独叫「暖灰」，别落到「灰橙」这种词不达意的说法。
    # 复审实测 RGB(210,190,160) / (200,180,150) 旧逻辑都输出「灰橙」。
    if 15 <= hue < 70 and v > 0.5 and s < 0.30:
        return "暖灰"

    if s < 0.15:
        return _gray_ladder(v)

    if hue < 15 or hue >= 345:
        base = "红"
    elif 15 <= hue < 48 and v < 0.58:
        # 「棕」必须在「橙」之前判：深而暖的色（如 RGB 90,60,30）hue 也落在橙区，
        # 但它是棕不是深橙。旧顺序让这个分支永远到不了。
        base = "棕"
    elif hue < 40:
        base = "橙"
    elif hue < 48:
        # 40~48 是橙黄交界（如 RGB 240,200,60 的 hue=46.7）。
        # 这里不能再判一次「棕」—— 棕已经由上面的 v<0.58 条件覆盖，
        # 再来一个兜底会把明黄吃成棕。
        base = "橙"
    elif hue < 68:
        base = "黄"
    elif hue < 160:
        base = "绿"
    elif hue < 200:
        base = "青"
    elif hue < 258:
        base = "蓝"
    elif hue < 300:
        base = "紫"
    else:
        base = "品红"

    if v > 0.86 and s < 0.30:
        return f"淡{base}"
    if v < 0.40:
        return f"深{base}"
    if s < 0.30:
        return f"灰{base}"
    return base


def _gray_ladder(v: float) -> str:
    """灰阶命名。阈值是按「人眼看起来还叫不叫白」定的：
    RGB(200,200,205) 的 v 是 0.804，叫它「近白」不合适，那是浅灰。"""
    if v < 0.25:
        return "深灰"
    if v < 0.5:
        return "中灰"
    if v < 0.85:
        return "浅灰"
    return "近白"


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % rgb


# ══════════════════ 本地档（0 成本） ══════════════════

def extract_local(image_path: str) -> dict:
    """只用 Pillow 提炼：色板 + 尺寸 + 朝向

    任何情况下都可用 —— 没有视觉模型、没有网络、没有 key 都不影响。
    """
    with Image.open(image_path) as im:
        width, height = im.size
        rgb = im.convert("RGB")

        # 缩到小图再量化：取主色不需要原分辨率，这一步让耗时从几百 ms 降到几 ms
        thumb = rgb.copy()
        thumb.thumbnail((160, 160))
        # 自适应调色板（中位切分），比固定 216 色网更贴合这张图的实际配色
        quant = thumb.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        palette_raw = quant.getpalette() or []
        counts = quant.getcolors() or []
        total = sum(c for c, _ in counts) or 1

    # 按出现频次排，取前 N。counts 里的颜色是调色板索引
    picked: list[tuple[int, tuple[int, int, int]]] = []
    for count, idx in sorted(counts, key=lambda x: -x[0]):
        base = idx * 3
        if base + 2 >= len(palette_raw):
            continue
        picked.append((count, (palette_raw[base], palette_raw[base + 1], palette_raw[base + 2])))
        if len(picked) >= MAX_PALETTE:
            break

    palette = []
    for count, (r, g, b) in picked:
        palette.append({
            "name": _color_name(r, g, b),
            "hex": _to_hex((r, g, b)),
            "rgb": [r, g, b],
            "ratio": round(count / total, 3),
        })

    # 平均亮度给一个「明亮/偏暗」的判断，对 render_desc、light 相关措辞有用。
    # ★ 复用已经在内存里的缩略图算，不再开一次原图 ——
    #   4032x3024 的图全分辨率算亮度要 37ms，而缩略图只要 1ms 级别。
    stat = ImageStat.Stat(thumb.convert("L"))
    mean_lum = int(stat.mean[0]) if stat.mean else 128

    return {
        "palette": palette,
        "source_width": width,
        "source_height": height,
        "source_ratio": round(width / height, 4) if height else None,
        "orientation": "landscape" if width > height else ("portrait" if height > width else "square"),
        "mean_luminance": mean_lum,
        "light_hint": "偏亮" if mean_lum > 165 else ("偏暗" if mean_lum < 85 else "中间调"),
        "_origin": "local",
    }


# ══════════════════ VLM 档 ══════════════════

_VLM_PROMPT = """你是一位摄影构图分析员。请看这张照片，只输出一个 JSON 对象，不要任何解释文字。

字段要求：
{
  "subject": {"name": "画面核心主体的简短中文名，6 字以内"},
  "anchors": [
    {"desc": "原图中可被视觉化提炼的结构，8 字以内",
     "materializable": true 或 false}
  ],
  "risk_notes": "一句中文，说明这张照片二次创作时最容易失真的地方（20 字以内）"
}

要求：
- anchors 给 2-4 个，按视觉权重从高到低排。
- anchors 必须是**原图里真实存在**的结构（山脊、水面倒影、道路、云层、枝条、
  窗框、人群轮廓、光影边界等），不要写"氛围""感觉"这类抽象词。
- materializable=true 表示这个结构适合被物化成实体（纸雕、立体、浮雕）。
- 如果照片里有人，subject 写人的角色或姿态（如"侧身行走的女性"）。
- 如果主体不明确，subject 写画面中最有辨识度的景物。
- 不要编造照片里没有的东西。
- **专有名词（机构 / 地名 / 人名）必须来自图中清晰可辨的文字**：
  匾额、招牌、门楣上的字要逐字转写；看不清就写"匾额文字未辨认清"。
  **严禁猜测** —— 中国很多大学校门形态相似（天津大学、清华大学都是拱门式），
  认不出校名时 subject 只写形态描述（如"石质拱形校门"），绝不要替用户"认"学校。
  （实测教训：把天津大学校门看成了清华大学 —— 这种错误会直接写进提示词并毁掉出图。）
"""


def _prepare_for_vlm(image_path: str) -> tuple[str, str]:
    """缩图 + 编码，返回 (base64, mime)"""
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        rgb.thumbnail((VLM_MAX_SIDE, VLM_MAX_SIDE))
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=VLM_JPEG_QUALITY)
        raw = buf.getvalue()
    return base64.b64encode(raw).decode("ascii"), "image/jpeg"


def extract_with_vlm(image_path: str) -> dict:
    """调用视觉模型提炼 subject / anchors / risk_notes

    任何失败都返回 {} —— 调用方会退回到 local 档的产出。
    理由：card 的 VLM 部分是**增强**而不是**前提**，
    它挂了不该让整条出图链路挂掉。
    """
    if not VISION_MODEL:
        logger.debug("未配置 VISION_MODEL，跳过 VLM 档 card 提取")
        return {}

    try:
        from services.llm import extract_json_lenient, vision

        b64, mime = _prepare_for_vlm(image_path)
        # ★ 识别模型可插拔：VISION_* 由 services/llm.vision() 内部读取 ——
        #   否则用户单独配置的视觉供应商会被静默发到 DeepSeek 渠道吃 401，
        #   表面上是"没配好"，实际是接线错误（审查 P1-1）。
        # ★ 看图调用统一走 llm.vision()：那里有「空响应识别 + 退避重试 +
        #   从异常报文里抢救内容」三件套。此前这里是自己 new OpenAI 且不重试，
        #   上游一次抖动就让整张图的 VLM 档永久降级（见 llm.py 顶部注释）。

        with step("VLM 提炼 card", model=VISION_MODEL, px=VLM_MAX_SIDE):
            text = vision(
                [{"role": "user", "content": [
                    {"type": "text", "text": _VLM_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
                # ★ 1200 而不是 500（2026-10-03 实测）：带推理的模型会把额度
                #   先花在思考上，500 时正文刚写到 anchors 就被掐断 →
                #   JSON 残缺 → 整段 VLM 成果被丢弃，card 退回到只有色板的本地档。
                max_tokens=1200,
                temperature=0.2,
                timeout=REQUEST_TIMEOUT_SEC,
            )

        # ★ 允许截断：被掐断时用已得到的完整字段，而不是让这一趟付费调用白跑。
        #   （解析不出来仍然返回 None → 走下面的「退回本地档」分支）
        data = extract_json_lenient(text)
        if not isinstance(data, dict):
            logger.warning("VLM 返回无法解析为 JSON：%s", text[:120])
            return {}

        out: dict[str, Any] = {}

        subj = data.get("subject")
        if isinstance(subj, dict) and str(subj.get("name") or "").strip():
            out["subject"] = {"name": str(subj["name"]).strip()[:20]}
        elif isinstance(subj, str) and subj.strip():
            out["subject"] = {"name": subj.strip()[:20]}

        anchors = []
        for a in (data.get("anchors") or [])[:MAX_ANCHORS]:
            if isinstance(a, dict) and str(a.get("desc") or "").strip():
                anchors.append({
                    "desc": str(a["desc"]).strip()[:30],
                    "materializable": bool(a.get("materializable", False)),
                    "selected": True,
                })
            elif isinstance(a, str) and a.strip():
                anchors.append({"desc": a.strip()[:30], "materializable": False,
                                "selected": True})
        if anchors:
            out["anchors"] = anchors

        notes = str(data.get("risk_notes") or "").strip()
        if notes:
            out["risk_notes"] = notes[:80]

        out["_origin"] = "vlm"
        audit("card_extracted_vlm", file=os.path.basename(image_path),
              anchors=len(anchors), has_subject="subject" in out)
        return out

    except Exception as e:                       # 网络 / 鉴权 / 模型不支持视觉
        logger.warning("VLM 提取 card 失败（退回本地档）：%s: %s", type(e).__name__, e)
        audit("card_extract_vlm_failed", file=os.path.basename(image_path),
              error=f"{type(e).__name__}: {e}"[:200])
        return {}


# ══════════════════ 合并入口 ══════════════════

def _cache_key(image_path: str) -> tuple:
    """用 (路径, mtime, 大小) 做缓存键 —— 图变了就失效，不容易骗"""
    try:
        st = os.stat(image_path)
        # 用纳秒精度：秒级 mtime 在同一秒内改写文件时会碰撞，
        # 复审实测「同尺寸同秒的两张不同图 → 返回旧卡」。
        return (os.path.abspath(image_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (os.path.abspath(image_path), 0, 0)


@lru_cache(maxsize=64)
def _cached_local(key: tuple) -> str:
    """本地档缓存。本地档是纯计算，结果永远可复用。"""
    return json.dumps(_extract_local_dict(key[0]), ensure_ascii=False)


# VLM 档**不缓存失败**（审查发现 P1-7）。
# 旧实现把带 VLM 的完整卡一起 lru_cache，于是「一次网络抖动」
# 就把这张图的反推 forbid 永久降级成通用约束 —— 而 /generate 每次都真花钱，
# 用户却只会看到一条 warning，除了重启进程没有恢复手段。
# 现在：成功的 VLM 结果进缓存（带图指纹），失败只记入这个短命表，
# 下次调用会重新尝试。
_VLM_OK: dict[tuple, str] = {}
_VLM_LOCK = threading.Lock()


def _extract_local_dict(image_path: str) -> dict:
    try:
        return extract_local(image_path)
    except Exception as e:
        logger.warning("本地 card 提取失败：%s", e)
        return {"palette": [], "_origin": "none", "_error": f"{type(e).__name__}: {e}"}


# ── 视觉卡三层派生（确定性代码，不调模型，可离线单测）──────────
# 迁移范围三档 + 来源残留 + 核心规则，全部从提取卡的客观字段派生。
# 规则必须是"可观察的关系或行为"；禁止用 影视感/氛围类 形容词充当规则
# （有一个专门的校验函数 check_observable_rules 兜底）。

# 充当"视觉规则"会被打回的形容词——它们不描述任何可观察关系
_NON_OBSERVABLE = (
    "电影感", "电影般", "梦幻", "梦幻般", "优雅", "高级", "高级感",
    "复古", "惊艳", "精致", "氛围感", "唯美", "大气", "有质感", "艺术感",
    "胶片感", "大片感", "史诗感", "治愈感", "故事感", "叙事感", "油画感",
)


def check_observable_rules(text: str) -> list[str]:
    """返回文本里充当视觉规则的非可观察形容词。空列表 = 通过。"""
    t = text or ""
    return [w for w in _NON_OBSERVABLE if w in t]


def build_visual_card(card: dict) -> dict:
    """从提取卡派生「视觉卡」：核心规则 + 迁移范围 + 来源残留

    ⚠️ 语义说明（与造梦师不同之处，是刻意设计）：
    造梦师的解梦卡服务于"参考图迁移"——原图内容属于不迁移的残留。
    换颜的提取卡服务于"**原图重绘**"——原图的主体与构图恰恰是
    必须强继承的东西（这是项目的立身之本：原图保真）。
    所以这里的 transfer_scope 把"主体身份与姿态"放在**强继承**档。
    """
    palette = card.get("palette") or []
    names = [c.get("name") for c in palette if c.get("name")]
    ratios = [c.get("ratio") for c in palette if c.get("ratio")]
    orient = card.get("orientation")
    light = card.get("light_hint")
    subject = card.get("subject")
    anchors = [a.get("desc") for a in (card.get("anchors") or [])
               if isinstance(a, dict) and a.get("desc")]
    # ★ risk_notes 在提取卡里是**字符串**（notes[:80]），不是 list ——
    #   对 str 迭代会得到单字符列表，关键词匹配永远失败 →
    #   source_residue / anti_clichés 在提取卡路径下恒为空（审查 P1-2 实锤）。
    #   兼容两种形状：str 包成单元素列表；list 逐个过滤。
    raw_risks = card.get("risk_notes")
    if isinstance(raw_risks, str) and raw_risks.strip():
        risks = [raw_risks.strip()]
    elif isinstance(raw_risks, list):
        risks = [r for r in raw_risks if isinstance(r, str) and r.strip()]
    else:
        risks = []

    core_rules: list[str] = []
    if names:
        lead = names[0]
        core_rules.append(f"画面以{lead}为最大面积的基底色，其余颜色从属于它")
    if len(names) >= 2:
        second = names[1]
        core_rules.append(f"{second}作为对比色与{names[0]}并置，承担视觉焦点，面积明显小于基底色")
    if len(names) >= 3:
        core_rules.append(f"第三色{names[2]}仅作小面积点缀（点缀色面积不超过画面的一成）")
    if light:
        core_rules.append(f"整体明度落在「{light}」区间；最亮处贴近纸白而不发光，暗部保留密度")
    if orient == "landscape":
        core_rules.append("画面横向展开：主体不居中正面呈现，沿水平方向组织层次")
    elif orient == "portrait":
        core_rules.append("画面纵向展开：保留自上而下的空间层次与视线动线")
    if anchors:
        core_rules.append(f"以{anchors[0]}建立前景与中景的遮挡或并置关系，主体不悬空")
    if ratios and isinstance(ratios[0], (int, float)) and ratios[0] > 0:
        core_rules.append(f"主色占比约 {int(ratios[0] * 100)}%，其余色按此比例从属分配")

    # 收敛到 5–8 条：不足则保留现状（有几条算几条），超出截断
    core_rules = [r for r in core_rules if r][:8]

    transfer_scope = {
        "strong": [
            "主体身份与姿态（原图保真是本项目立身之本）",
            "色彩结构与占比",
            "明度与曝光行为",
            "材质与表面反应",
            "媒介特征（纸纹/印刷/笔触等）",
        ],
        "conditional": [
            {"item": "构图与空间组织", "action": "适应新场景主体"},
            {"item": "视角与主体尺度", "action": "适应新场景叙事"},
        ],
        # 原图重绘场景：主体属于强继承，没有"来源残留"意义上的身份排斥
        "do_not": [],
    }

    # 来源残留：本场景 = 原图里不该被"风格化复制"的东西（文字、水印、logo、畸变）
    source_residue = []
    for r in risks:
        if any(k in r for k in ("文字", "水印", "logo", "Logo", "标识", "畸变", "伪影")):
            source_residue.append(r)

    return {
        "core_rules": core_rules,
        "transfer_scope": transfer_scope,
        "source_residue": source_residue,
        # 双键兼容：模型可能输出无重音的 anti_cliches
        "anti_clichés": ([r for r in risks if "套路" in r or "避免" in r]
                         or [r for r in (card.get("anti_cliches") or [])
                             if isinstance(r, str) and r.strip()]),
    }


def build_card(image_path: str, *, use_vlm: bool = False, refresh: bool = False) -> dict:
    """提炼卡主入口

    use_vlm=False（默认）：只走本地档，0 成本 —— 预览/渲染路径用这个。
    use_vlm=True：额外调一次视觉模型，出图路径和 Agent 工具用这个。

    失败永远不抛：最差返回一个只有 _origin="none" 的壳，
    渲染器的 `_card_signal_level()` 会据此给出明确告警。
    """
    if not image_path or not os.path.exists(image_path):
        return {}

    key = _cache_key(image_path)
    if refresh:
        # ★ 只失效当前这张图（审查 P2-8）：旧实现清空**整个**缓存 ——
        #   单个用户的刷新请求会把其他会话的卡全部打掉，VLM 档下次全部重算（重新花钱）。
        #   本地档的键含 (path, mtime, size)：文件没变结果必然相同，无需失效；
        #   真要全量清的场合走 clear_card_cache()（管理员工具语义）。
        with _VLM_LOCK:
            _VLM_OK.pop(key, None)

    try:
        card = json.loads(_cached_local(key))
    except Exception as e:                       # 缓存层自己出问题也不能拖垮出图
        logger.warning("本地 card 缓存读取失败，回退直算：%s", e)
        card = _extract_local_dict(image_path)

    if not use_vlm:
        return card

    # VLM 档：只认缓存里的**成功**结果
    with _VLM_LOCK:
        cached = _VLM_OK.get(key)
    if cached is not None:
        card.update(json.loads(cached))
        card["_origin"] = "vlm+local"
        return card

    vlm = extract_with_vlm(image_path)
    if vlm:
        merged = {k: v for k, v in vlm.items() if k != "palette"}
        card.update(merged)
        card["_origin"] = "vlm+local"
        # 只有成功才写缓存
        with _VLM_LOCK:
            _VLM_OK[key] = json.dumps(merged, ensure_ascii=False)
            if len(_VLM_OK) > 64:                # 简单的上限保护
                _VLM_OK.pop(next(iter(_VLM_OK)))
    # 失败时 card 只带本地档字段，且**不缓存** —— 下次会重试
    return card


def clear_card_cache() -> None:
    _cached_local.cache_clear()
    with _VLM_LOCK:
        _VLM_OK.clear()


def vlm_cache_stats() -> dict:
    with _VLM_LOCK:
        n = len(_VLM_OK)
    return {"local_entries": _cached_local.cache_info().currsize,
            "vlm_ok_entries": n}


def summarize_card(card: dict | None, limit: int = 3) -> str:
    """给日志 / 工具观察结果用的一句话摘要"""
    if not card:
        return "（无）"
    subj = card.get("subject")
    name = subj.get("name") if isinstance(subj, dict) else subj
    anchors = [a.get("desc") for a in (card.get("anchors") or []) if a.get("desc")]
    bits = []
    if name:
        bits.append(f"主体={name}")
    if anchors:
        bits.append("锚点=" + "、".join(anchors[:limit]))
    pal = card.get("palette") or []
    if pal:
        bits.append("主色=" + "、".join(p.get("name", "") for p in pal[:3]))
    return "；".join(b for b in bits if b) or "（无有效字段）"


if __name__ == "__main__":
    import tempfile

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    # 自检：造几张不同色调的图，看色名与朝向判断是否正确
    print("=== 本地档自检（0 成本，不需要任何 key）===")
    cases = [
        ("暖米白纸张", (242, 233, 220), (900, 600)),
        ("深蓝夜空", (18, 32, 70), (600, 900)),
        ("草木绿", (74, 128, 62), (800, 800)),
        ("砖红", (176, 62, 44), (1200, 800)),
        ("近黑", (6, 6, 8), (500, 500)),
    ]
    d = tempfile.mkdtemp(prefix="card_")
    for label, color, size in cases:
        p = os.path.join(d, f"{label}.jpg")
        Image.new("RGB", size, color).save(p, quality=95)
        card = build_card(p, use_vlm=False)
        pal = card.get("palette") or [{}]
        print(f"  {label:8} → 主色={pal[0].get('name'):6} {pal[0].get('hex')} "
              f"| {card.get('orientation'):9} {card.get('light_hint')} "
              f"| 色数={len(card.get('palette') or [])}")
        print(f"           摘要: {summarize_card(card)}")
    print(f"\nVISION_MODEL 配置: {VISION_MODEL or '（未配置 → 只走本地档）'}")
