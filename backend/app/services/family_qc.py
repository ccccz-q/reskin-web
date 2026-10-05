"""服务层 · 家族「出厂检验」（语义级质量闸门）

★ 为什么需要这一层（2026-10-03 用户定性：链路健壮性不被允许这么差）
--------------------------------------------------------------
在 QC 之前，链路上所有校验都是**结构/语法层**的（validate_family / 渲染冒烟）：
占位符能不能解析、dicts 覆不覆盖、hard_forbid 够不够 4 条。
它们拦得住「装不上」的家族，拦不住「装得上、出图才炸」的**语义缺陷**：

  ① 画幅字段缺失          → 渲染不报错，正方形原图被生成端强改成竖条
  ② 正向要求被反转成禁止项  → 渲染不报错，用户要的 YOU DIED 反而看不见
  ③ forbid 与 creative 自相矛盾（creative 要准星、forbid 禁准星）
                          → 渲染不报错，生图模型收到两个方向随机站队 → 出图飘忽
  ④ UI 元素只记名字不记规格 → 渲染不报错，模型自由发挥出完全不像的界面

QC 在两个位置设闸：
  - forge() 收尾：自动修的修掉，修不掉的进 errors（该版标为不可安装）
  - install 之前：最后一道关，带病家族被拦下并列明原因，绝不带病上岗

★ 只做**确定性**检查（纯代码、可解释、零 LLM 调用）——质量闸门自己必须是稳定的。
启发式检查只产 warning 不产 blocker，绝不误杀可用家族。
"""
from __future__ import annotations

import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.logging import logger  # noqa: E402

# 与 image_generator.ASPECT_TO_PIXELS 对齐的合法画幅（避免跨模块循环导入，此处复制）
_VALID_ASPECTS = {"3:5", "2:3", "3:4", "4:5", "1:1", "16:9", "9:16", "origin"}

# forbid↔creative 冲突扫描的泛词白名单：这些词出现在两边不算矛盾
# （「不得出现无关文字」vs creative 里的界面文字 —— 泛词重叠是正常的）
_CONFLICT_STOPWORDS = {
    "文字", "水印", "乱码", "元素", "内容", "画面", "图案", "样式",
    "整体", "风格", "效果", "质感", "细节", "结构", "构图", "光源",
    # 实测误报源（内置家族 dry-run 体检收集）：这些词出现在两边是常态
    "照片", "原照片", "摄影", "画面上", "上半区", "下半区", "上半部分",
    "大面积", "平滑", "渐变", "留白", "负空间", "真实", "自然", "人物",
    "核心主体", "像素物件", "像素物品", "道具", "界面", "场景",
}

# 禁令动词：forbid 段的行应当以它们之一表达「不许」
_FORBID_VERBS = ("不得", "禁止", "不要", "避免", "不许", "严禁")

# 空洞词（移植造梦师 Hard Avoids）：无法变成可见像素的描述 —— 只会稀释提示词精度
_HOLLOW_WORDS = ("电影感", "氛围感", "梦幻", "高级感", "精致感", "胶片感",
                 "景深虚化", "虚化背景", "HDR", "电影级")

# 流程备注特征（实测 2026-10-03「俯拍死亡界面」教训）：这些字样出现在
# forbid/hard_forbid 里 = 解构/编译模型把**观察记录**写成了**禁止令** ——
# 「不得出现：XXX 未观察到」「不得出现：…是否保留应由后续任务要求决定」
# 既不是可执行的禁令，还会与 creative 的正向保留直接打架（那次正是
# creative ④ 要求保留完整 YOU DIED 界面、forbid 却禁止它）。正常禁令
# 不可能包含这些词 —— 命中即 blocker，零误杀。
_NOTE_IN_FORBID_MARKS = (
    "未观察到", "不足以确认", "是否保留应由后续", "是否保留由后续",
    "均为画面中可见元素", "均为画面可见元素", "无法确认身份",
    "由后续任务", "由后续流程", "由用户后续",
)

# UI 规格启发式：creative 提到 UI 元素时应伴随规格词
_UI_HINT_WORDS = ("按钮", "标题", "准星", "血条", "图标", "界面", "计分", "菜单")
_UI_SPEC_WORDS = ("色", "描边", "底板", "外框", "边框", "占比", "位置", "材质",
                  "像素字", "宽", "高", "发光", "浮雕", "比例")


def _split_clauses(text: str) -> list[str]:
    """把 forbid 文本拆成禁令短句（分号/换行/顿号级）"""
    parts = re.split(r"[；;。\n、]+", str(text or ""))
    return [p.strip(" -•\t") for p in parts if len(p.strip(" -•\t")) >= 4]


def _core_of_clause(clause: str) -> str:
    """提取禁令的核心宾语：去掉「不得出现/禁止/不要/避免」等动词前缀"""
    c = re.sub(r"^(不得|禁止|不要|避免|严禁|不许)[出现使用添加保留泄漏复制复现把]{0,3}", "", clause)
    return c.strip("：:，, ")


# 否定语境标记：片段出现在这些词附近时，creative 是在「否定它」而不是「要求它」
_NEGATION_MARKS = ("不是", "非", "不得", "禁止", "不要", "避免", "不许", "严禁",
                   "没有", "无", "绝不", "而非", "去掉", "排除",
                   # ★ 消失语境（discard_list 教训）：creative 写「远处栏杆和天空
                   #   必须从画面中消失」与 forbid「禁止远处栏杆」方向一致，不是矛盾
                   "消失", "丢弃", "剔除", "删除", "去除", "移除")


def _in_negation_context(text: str, pos: int, span: int) -> bool:
    """片段在 text 的 pos 处是否处于否定语境（前 10 字 / 后 14 字窗口内）。

    后窗比前窗宽：discard_list 的消失指令是「片段 + 理由…必须消失」句式，
    「消失」落在片段后较远处（实测「远处栏杆和天空必须从画面中消失」差一个字漏判）。
    """
    lo = max(0, pos - 10)
    hi = min(len(text), pos + span + 14)
    head = text[lo:pos]
    tail = text[pos + span:hi]
    return any(m in head or m in tail for m in _NEGATION_MARKS)


def _clause_conflicts_creative(clause: str, creative: str) -> str | None:
    """判断一条禁令是否与 creative 直接矛盾（返回矛盾的片段，无则 None）。

    判法：禁令核心宾语按 2~12 字滑窗取片段，若某片段**原样**出现在 creative
    且不在泛词白名单 → creative 要求了 forbid 禁止的东西 → 矛盾。
    用「原样片段」而不是模糊相似，宁可漏报不误杀。
    ★ 2 字滑窗只允许宾语本身≤2 字（如「准星」）—— 长宾语滑出的 2 字片段
    （如「像素」「文字」）是误杀大户（实测：「均匀像素滤镜」被 creative 里的
    「像素场景」误杀），必须拦掉。
    """
    core = _core_of_clause(clause)
    if len(core) < 2:
        return None
    # 碎片精度（实测 dry-run 噪音收集）：长宾语滑出 3 字碎片（「户要求」
    # 「可辨认的」「 + 」「o gradients,」）全是无意义匹配 —— 最小窗口提到 4。
    min_n = len(core) if len(core) <= 3 else 4
    for n in range(min(12, len(core)), min_n - 1, -1):
        for i in range(0, len(core) - n + 1):
            frag = core[i:i + n]
            if frag in _CONFLICT_STOPWORDS or any(w in frag for w in _CONFLICT_STOPWORDS):
                continue
            if re.fullmatch(r"[\W_0-9a-zA-Z ]+", frag):
                continue                      # 纯符号 / 数字 / 英文短语，不是中文实词
            hit = creative.find(frag)
            if hit < 0:
                continue
            # ★ 否定语境豁免（实测 doodle_narrators 教训）：creative 写
            #   「不是上下拼接的海报」与 forbid「严禁上下拼接」语义一致 ——
            #   字面相同但不是矛盾。只在非否定语境下才算冲突。
            if _in_negation_context(creative, hit, n):
                continue
            return frag
    return None


def qc_family(spec: dict, preq: dict | None = None,
              card: dict | None = None,
              enforce_aspect: bool = True) -> tuple[dict, list[str], list[str], list[str]]:
    """家族出厂检验。返回 (fixed_spec, blockers, warnings, fixes)。

    blockers：修不掉、必须拦下的语义缺陷（安装/入库前为空才放行）
    warnings：启发式提示（不阻断，写进 warnings 供用户参考）
    fixes：QC 自动修复记录（透明，让用户知道动过什么）

    ★ enforce_aspect：画幅铁律是否生效。内置家族（人工精修、海报比例是设计
      的一部分）必须传 False —— 批量体检内置家族时曾把它们从 3:4 改成 origin。
      提炼产物走 install 路径（默认 True）。
    ★ 矛盾扫描**只报告不自动删**：正向要求的保护已由 _protect_positive_requirements
      与 _enforce_residue 负责；QC 再删会误伤「creative 提到 A、forbid 禁 B」的
      合法设计（内置家族实测被误删 12 条）。且含占位符的条目一律跳过 ——
      {dynamic_forbid}/{hard_forbid_joined} 的真实内容渲染时才确定，扫模板等于扫未知。
    """
    out = dict(spec)
    blockers: list[str] = []
    warnings: list[str] = []
    fixes: list[str] = []

    segs = out.get("segments")
    segs = dict(segs) if isinstance(segs, dict) else {}
    creative = str(segs.get("creative") or "")
    forbid = str(segs.get("forbid") or "")
    hard_forbid = [str(x) for x in (out.get("hard_forbid") or [])]

    # ── ① 画幅：必须存在且合法（实测：缺失 → 正方形原图被强改竖条）──
    #  ★ 覆写类铁律（移植造梦师「orientation 是源图空间逻辑」）：家族若允许
    #    局部改（text_add/background/edge/object_form 任意一项），它就是
    #    「在原图上覆写」而非「另起构图」—— 画幅必须跟随原图。
    da = str(out.get("default_aspect") or "").strip()
    allow = {str(x) for x in (out.get("allow_change") or [])}
    overlay_dims = {"text_add", "background", "edge", "object_form"}
    is_overlay = bool(allow & overlay_dims)
    if not da:
        out["default_aspect"] = "origin"
        fixes.append("default_aspect 缺失，已补为 origin（跟随原图宽高比）")
    elif enforce_aspect and is_overlay and da != "origin":
        out["default_aspect"] = "origin"
        fixes.append(f"覆写类家族（allow_change 含 {'/'.join(sorted(allow & overlay_dims))}）"
                     f"却指定了固定画幅 {da}，已改回 origin（跟随原图）")
    elif da not in _VALID_ASPECTS:
        out["default_aspect"] = "origin"
        fixes.append(f"default_aspect={da} 不是合法画幅，已回退为 origin")

    # ── ② 正向短语存活：用户引号要求的文字必须真的写在 creative/preserve ──
    quoted = [q for q in (preq or {}).get("quoted") or [] if q]
    if quoted:
        missing = [q for q in quoted
                   if q not in creative and q not in str(segs.get("preserve") or "")]
        if missing:
            add = "\n按用户要求呈现这些元素：" + "、".join(f"“{q}”" for q in missing[:6]) + "。"
            if isinstance(segs.get("creative"), str):
                segs["creative"] = creative.rstrip() + add
                creative = segs["creative"]
                fixes.append("creative 补上了用户要求出现但缺失的元素："
                             + "、".join(missing[:6]))
            else:
                blockers.append(f"用户要求出现的元素未写入提示词：{'、'.join(missing[:6])}")

    # ── ③ forbid↔creative 矛盾扫描（只报告，不自动删）──
    # creative 要 X 而 forbid/hard_forbid 禁 X → 生图模型方向随机 → 出图飘忽。
    # ★ 自动删除已废除（实测误伤内置家族 12 条合法禁令）：
    #   正向要求的保护由 _protect_positive_requirements / _enforce_residue 负责，
    #   QC 只负责「看得见」—— 把矛盾摆到 warnings 里，用户能自己判断。
    # ★ 含占位符的条目一律跳过：{dynamic_forbid}/{hard_forbid_joined}/{x_desc}
    #   的真实内容渲染时才确定，扫模板等于扫未知文本。
    clashes: list[str] = []
    for label, lines in (("forbid", _split_clauses(forbid)),
                         ("hard_forbid", hard_forbid)):
        for line in lines:
            if re.search(r"\{[a-zA-Z_]", line):        # 含占位符 → 跳过
                continue
            hit = None
            for clause in _split_clauses(line):
                frag = _clause_conflicts_creative(clause, creative)
                if frag:
                    hit = frag
                    break
            if hit:
                clashes.append(f"{label}：「{line[:28]}…」与 creative 的"
                               f"「{hit}」矛盾（{label} 禁、creative 要）")
    if clashes:
        warnings.append("forbid 与 creative 存在方向矛盾（生图模型会随机站队）："
                        + "；".join(clashes[:4])
                        + " —— 建议迭代一版把正向要求写进 creative、对应禁令删掉")

    # ── ④ UI 规格启发式：提到 UI 元素却毫无规格词 → warning（不阻断）──
    if any(w in creative for w in _UI_HINT_WORDS) and \
            not any(w in creative for w in _UI_SPEC_WORDS):
        warnings.append(
            "creative 提到了界面元素但没有任何视觉规格（颜色/描边/位置等）——"
            "生图模型会自由发挥，建议迭代一版把规格写具体")

    # ── ⑤ forbid 段存在不含禁令动词的行 → 可能是写错段的正向描述 ──
    for line in _split_clauses(forbid):
        core = _core_of_clause(line)
        if not any(v in line for v in _FORBID_VERBS) and len(core) >= 4 \
                and not line.startswith("除用户"):
            warnings.append(f"forbid 段有一行不像禁止项：「{line[:30]}…」，请检查是否写错段")
            break

    # ── ⑥ 空洞词检查（移植造梦师 Hard Avoids 的核心禁令）──
    # 「电影感/氛围感/梦幻/景深」这类词无法变成可见像素 —— 实测
    # voxel_death_photo 写出了「柔和的夏日环境光与电影感高光」，生图模型
    # 只会把它翻译成泛泛的调色，抹掉像素语言的锐度。
    all_text = creative + forbid + "\n".join(out.get("hard_forbid") or [])
    hollow = [w for w in _HOLLOW_WORDS if w in all_text]
    if hollow:
        warnings.append("提示词里出现无法执行的空洞词：" + "、".join(hollow[:5])
                        + "——请改成具体可见的行为（如「边缘硬朗、无抗锯齿」）")

    # ── ⑦ 文案写死比例检查（实测 2026-10-03：voxel_death_photo 的 preserve
    #    里硬写「保持竖构图，画面比例为9:16」，而 default_aspect 已是 origin ——
    #    API 按原图出方图、prompt 文字却叫模型出竖条，两者打架 → 出图必错。
    #    渲染层与出图层都不报错，只有这里扫得出来）──
    seg_text = "\n".join(str(v or "") for v in segs.values())
    ratio_hits = re.findall(r"\b(16:9|9:16|1:1|3:4|4:5|2:3|3:5|4:3|5:4)\b", seg_text)
    if ratio_hits:
        uniq = sorted(set(ratio_hits))
        if str(out.get("default_aspect") or "origin") == "origin":
            warnings.append(
                f"segments 文案里写死了画幅比例（{'、'.join(uniq)}），"
                "但 default_aspect=origin（应跟随原图）——文字与实际输出冲突，"
                "请删掉文案里的比例数字，只用 default_aspect 表达画幅")
        else:
            warnings.append(
                f"segments 文案里写死了画幅比例（{'、'.join(uniq)}）——"
                "请确认与 default_aspect 一致，避免文字与实际输出冲突")

    # ── ⑧ 流程备注混入禁令清单（实测 2026-10-03「俯拍死亡界面」）──
    # 「不得出现：…是否保留应由后续任务要求决定」= 观察记录被写成禁止令，
    # 与 creative 的正向保留直接打架 → 生图方向随机。这里是最后一道闸：
    # 上游 prompt 约束（templates/prompts/ 的 forge_decode / forge_synth 资产）
    # 与代码兜底（_enforce_residue 备注过滤）之外，编译模型自己写 forbid 也可能犯。
    note_hits: list[str] = []
    for label, lines in (("forbid", _split_clauses(forbid)),
                         ("hard_forbid", hard_forbid)):
        for line in lines:
            hit = next((m for m in _NOTE_IN_FORBID_MARKS if m in line), None)
            if hit:
                note_hits.append(f"{label}：「{line[:26]}…」（备注特征：{hit}）")
    if note_hits:
        blockers.append(
            "forbid 里混入了观察备注而非禁令（会被生图模型当成禁止项，"
            "与创作段的保留要求打架）：" + "；".join(note_hits[:3])
            + " —— 请迭代一版，或人工删掉这些备注式条目")

    out["segments"] = segs
    if fixes:
        logger.info("家族 QC 自动修复 %d 处：%s", len(fixes), fixes[:3])
    return out, blockers, warnings, fixes
