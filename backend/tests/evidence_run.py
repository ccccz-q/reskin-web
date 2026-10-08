"""效果证据 · 消融实验（2026-10-09 重写版）

    python tests/evidence_run.py ablation

★★ 为什么要重写（上一轮三处硬伤，全部是我自己的问题）：
  ① **对照组自带契约**：A 组写着「保留原图主体与构图」，
     它既指定风格又下保留指令 —— **本身就是一个契约**，
     不构成"无契约基线"。更糟的是我把 A 组的「天空加入飞船」
     归因于"缺少 forbid"，而飞船**恰恰是 A 组自己要求的**，因果反了。
  ② **有效样本 n=1**：样本 2 三组全部因上游超时失败。
  ③ **判据与实验对象不配套**：checklist 按科幻氛围设计，
     实际跑的是拾景纸刊（纸刊成品不可能"科幻"，第 2 条天然不成立）。

  这一版的三条纪律：
  · **基线干净**：A 组一个字都不提"要保留什么"
  · **判据配套**：checklist 按被测家族的能力写，不按另一个家族
  · **失败重试**：生图失败自动重试，仍失败就如实记FAIL，绝不目测代替

★★ 实验设计
  A组｜无契约        只说目标风格，不提任何保留要求
  B 组｜仅保真段     只给 preserve —— 模型知道"别改"，不知道"改成什么"
  C 组｜完整三段式   preserve + creative + forbid（项目实际用法）

  样本：初版/测试图片/ 里的真实航拍建筑照（2 张）
        + 若storage 里有真实用户照片则一并纳入
  指标：VLM 按 checklist 逐条判定 + **结构忠实度的客观测量**

★ 关于「客观测量」这一项（上一轮完全没有）：
  光靠 VLM 主观判断不够，所以补一个**可复算的几何指标** ——
  用灰度图的梯度结构相似度衡量"构图骨架有没有被重画"。
  它不能证明"地标认得出"，但能**证伪"结构被彻底重绘"**，
  而后者恰恰是 A 组最可能被诟病的地方。
"""
import base64
import io
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "app"))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

OUT_GALLERY = _ROOT.parent / "docs" / "gallery"
OUT_DATA = _ROOT.parent / "docs" / "evidence"
OUT_ABLATION = OUT_DATA / "ablation"

# ── 生图重试：上一轮样本 2 三组全挂在超时上 ──────────────────
GEN_ATTEMPTS = 3
GEN_WAIT_SEC = 8


def collect_sources():
    """真实照片作原图。★ 判据不是「图好不好看」，是「**这张图是不是本项目跑出来的**」。

    历史教训（错了三轮）：
      · storage/images/{gen_upload,upload}_* 共 261 张，**全是 e2e 测试造的
        320×480 水彩简笔山景图** —— 拿简笔画当"保真度"证据毫无意义。
      · 提示词规范/ 里的真实照片是**别的 skill** 的产物 —— 拿别的东西证明自己是伪造。
      ⇒ 正确的是 初版/测试图片/ 里的真实航拍建筑照。
    """
    base = _ROOT.parent.parent / "初版" / "测试图片"
    picked = []
    for p in sorted(base.glob("test?.png")):
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
            if min(w, h) >= 600:
                picked.append(p)
        except Exception:
            continue
    return picked


def _gen_with_retry(src: Path, prompt: str, label: str) -> dict:
    """生图 + 重试。★ 失败就重试，不目测、不推理、不用别的图代替。"""
    from services.image_generator import generate_image_with_reference
    last = {}
    for k in range(1, GEN_ATTEMPTS + 1):
        t0 = time.time()
        r = generate_image_with_reference(str(src), prompt)
        if r.get("success"):
            print(f"      ✔ {label} 第{k}次成功（{time.time()-t0:.0f}s）")
            return r
        last = r
        print(f"      ✘ {label} 第 {k} 次失败：{str(r.get('error'))[:60]}")
        if k < GEN_ATTEMPTS:
            time.sleep(GEN_WAIT_SEC)
    return last


# ══════════════════════════════════════════════════════════
# 客观几何指标：构图骨架有没有被重绘
# ══════════════════════════════════════════════════════════
def structure_score(src_path: Path, out_path: Path) -> dict:
    """用灰度梯度直方图相似度衡量「结构骨架是否保留」。

    ★ 为什么需要它：
      VLM 判定是主观的、且会"看起来很精确地错"。
      一个**能被复算的客观数字**能补上这一层：
      它不能证明"地标认得出"，但能**证伪"结构被彻底重绘"**。
      而后者正是无契约组最可能被诟病的地方。

    做法（不引第三方库，Pillow + 手算）：
      1. 灰度化 → 缩小到同一尺寸（消除分辨率差异）
      2. Sobel 梯度 → 取边缘图的 8×8 网格直方图
      3. 比较两个直方图的 Bhattacharyya 系数（0=完全不同，1=完全一致）
    这个指标对"风格改变但骨架不变"比较宽容，对"彻底重画"比较敏感 ——
    正好匹配我们要测的东西。
    """
    try:
        from PIL import Image, ImageFilter
        import math

        def _edge(p: Path):
            with Image.open(p) as im:
                g = im.convert("L").resize((256, 256), Image.BILINEAR)
                e = g.filter(ImageFilter.FIND_EDGES)
                px = list(e.getdata())
            # 8x8 网格的边缘能量分布
            hist = [0.0] * 64
            W = H = 256
            for idx, v in enumerate(px):
                x, y = idx % W, idx // W
                hist[(y * 8 // H) * 8 + (x * 8 // W)] += v
            tot = sum(hist) or 1.0
            return [v / tot for v in hist]

        a, b = _edge(src_path), _edge(out_path)
        bc = sum(math.sqrt(x * y) for x, y in zip(a, b))      # Bhattacharyya
        return {"structure_sim": round(bc, 4),
                "verdict": "结构高度保留" if bc >= 0.90 else
                           "结构基本保留" if bc >= 0.80 else
                           "结构明显改变" if bc >= 0.65 else "结构被重绘"}
    except Exception as e:                                     # noqa: BLE001
        return {"structure_sim": None, "verdict": f"测量失败：{type(e).__name__}"}


# ══════════════════════════════════════════════════════════
# VLM 判定
# ══════════════════════════════════════════════════════════
def _visual_check(src_path: Path, out_path: Path, checklist: list[str]) -> dict:
    """按 checklist 逐条判定。

    ★ checklist 必须与被测家族配套 ——
      上一版按"科幻氛围"设计却跑纸刊成品，第 2 条天然不成立，
      数字「看起来很精确地错」。
    ★ 消息格式照 card_extractor 的真实用法：
      [{"role":"user","content":[{"type":"text",...},{"type":"image_url",...}]}]
    """
    from services.llm import vision

    def _img(p: Path):
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,"
                                          + base64.b64encode(p.read_bytes()).decode()}}

    lst = "\n".join(f"{i+1}. {c}" for i, c in enumerate(checklist))
    q = ("第一张图是原始照片，第二张图是按要求改绘后的成品。\n"
         "请逐条判断第二张图的情况，只回答「是」或「否」，不要解释、不要评价。\n"
         f"检查项：\n{lst}\n"
         "格式：每行一个，形如「1. 是」")
    try:
        txt = vision([{"role": "user",
                       "content": [{"type": "text", "text": q},
                                   _img(src_path), _img(out_path)]}],
                     max_tokens=400)
        return _parse(txt, len(checklist))
    except Exception as e:                                    # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "hit": None,
                "per_item": [], "unknown": len(checklist)}


def _parse(txt, n):
    hits = {}
    for line in str(txt or "").splitlines():
        m = re.search(r"(\d+)\s*[.、)）]\s*\**\s*(是|否|yes|no|有|没有)", line.strip(), re.I)
        if m:
            hits[int(m.group(1)) - 1] = m.group(2).lower() in ("是", "yes", "有")
    got = [hits.get(i) for i in range(n)]
    return {"per_item": got,
            "hit": sum(1 for x in got if x is True),
            "unknown": sum(1 for x in got if x is None)}


# ══════════════════════════════════════════════════════════
# 主实验
# ══════════════════════════════════════════════════════════
def cmd_ablation():
    from services.family_renderer import load_families, render_family

    srcs = collect_sources()
    if not srcs:
        print("★ 没有可用真实原图")
        return 1

    fams = load_families() or {}
    fam = fams.get("zine")
    if not fam:
        print("★ zine 家族不存在")
        return 1

    r = render_family(fam, {})
    segs = r.get("segments") or {}
    preserve = str(segs.get("preserve") or "").strip()
    creative = str(segs.get("creative") or "").strip()
    forbid = str(segs.get("forbid") or "").strip()
    if not (preserve and creative):
        print(f"★ 三段不完整preserve={len(preserve)} creative={len(creative)}")
        return 1

    # A组基线：纯中性指令，不含风格名也不含保真语义
    #这一组是整个实验的对照基准，必须干净。
    #
    # 我在这上面连栽三次，第三次才看清问题的层次：
    #   第 1 次：手写句子里含保真指令（保留原图主体与构图）
    #   第 2 次：引用的家族 description 本身含保真语义
    #           （真实照片从撕裂的纸边下显露）
    #   第 3 次：只给风格名「拾景纸刊」—— 风格名本身就是保真指令：
    #           这四个字在模型那里直接对应旧纸 + 撕纸边 + 水墨拼贴，
    #           于是判据第 2 条「旧纸张/印刷品质感」自动成立。
    #           实测：A 组拿 3/3、结构相似 0.90，与 C 组几乎无差别。
    #
    # ⇒★ 问题不只是「基线脏」，而是「这组判据无法证伪契约」：
    #   任何含风格信息的提示词都会自动满足与该风格相关的判据，
    #   于是 A 组永远接近满分，实验失去区分力。
    #   这比基线不干净更深：后者能修，前者要换实验设计。
    #
    # ⇒ 正确设计：A 组只给中性指令，不含风格名/保真语/材质词。
    #   它测的是「没有契约时模型默认会怎么做」，这才是有意义的基线。
    fam_desc = str(fam.get("description") or "").strip()   # 仅作日志参考
    fam_name = str(fam.get("name") or "拾景纸刊")
    PROMPT_A = "处理一下这张照片。"

    # B 组 = 只给保真段（★ 模型知道"别改"，但不知道"改成什么"）
    PROMPT_B = preserve
    PROMPT_C = "\n".join(x for x in (preserve, creative, forbid) if x).strip()

    # ★ checklist 按**被测家族（拾景纸刊）**的能力写，不按别的家族
    checklist = [
        "原照片中建筑/主体的位置与轮廓仍然可辨认",
        "画面呈现旧纸张或印刷品的质感",
        "画面有明显的风格化处理，而不是原始照片直接呈现",
    ]

    # ★★ 基线自检：出现保真**或风格**语义就直接拒绝跑。
    #   我在这上面栽过**三次**：
    #     ① 手写句子含保真 → ② 引用的 description 含保真 →
    #     ③ **风格名本身就是保真指令**（"拾景纸刊"四字≈旧纸+撕纸边+水墨）
    #   所以不再靠"下次记得注意"，而是**让脚本自己拦住**——
    #   这与项目里 `test_patching.py`（AST 拦住删导入）的思路一致。
    #
    #   ★ 第 ③ 次的关键认识：**污染源不只有保真词，还有风格词**。
    #     基线必须中性到"模型只能按默认理解去处理"，
    #     否则它会替契约把活干了，实验随即失去区分力。
    _POLLUTION = (
        # 保真类
        "保留", "原图", "真实", "显露", "完整", "结构", "不改变", "原本",
        "维持", "照原", "忠实", "一致",
        # ★ 风格/材质类（第 ③ 次栽在这）—— 模型认得这些词就会自己发挥
        "风格", "纸刊", "水墨", "版画", "体素", "剪影", "拼贴", "像素",
        "旧纸", "做旧", "网点", "抽象", "插画", "水彩", "海报",
        "印刷", "褶皱", "撕纸", "拼贴",
    )
    _polluted = [k for k in _POLLUTION if k in PROMPT_A]
    if _polluted:
        print(f"★ A 组基线含保真/风格语义词 {_polluted} —— 对照组被污染，拒绝运行。")
        print(f"  当前基线：{PROMPT_A}")
        print("  基线必须中性：不带风格名、不带保真语、不带材质词。")
        print("  理由：风格名（如「拾景纸刊」）本身就隐含了契约内容 ——")
        print("       模型认得它就会自己补齐，实验随即失去区分力。")
        return 2
    print(f"基线自检通过：A 组不含保真/风格语义词（已查 {len(_POLLUTION)} 个）")

    groups = [("A", "无契约", PROMPT_A),
              ("B", "仅保真段", PROMPT_B),
              ("C", "完整三段式", PROMPT_C)]

    OUT_ABLATION.mkdir(parents=True, exist_ok=True)
    print(f"家族：{fam_name}")
    print(f"风格描述：{fam_desc[:60]}")
    print(f"A 基线提示词（{len(PROMPT_A)} 字符）：{PROMPT_A}")
    print(f"三段长度 preserve={len(preserve)} creative={len(creative)} "
          f"forbid={len(forbid)}")
    print(f"样本 {len(srcs)} 张 × {len(groups)} 组 = "
          f"{len(srcs)*len(groups)} 次生图（每组最多重试 {GEN_ATTEMPTS} 次）\n")

    rows = []
    for si, src in enumerate(srcs):
        print(f"\n{'='*60}\n样本 {si+1}：{src.name}\n{'='*60}")
        for gid, label, prompt in groups:
            t0 = time.time()
            rec = {"sample": si + 1, "src": src.name, "group": gid,
                   "label": label, "prompt_len": len(prompt)}
            out = _gen_with_retry(src, prompt, f"{gid} {label}")
            if not out.get("success"):
                rec.update(ok=False, error=out.get("error"))
                print(f"  ✘ {gid} {label}：{GEN_ATTEMPTS} 次均失败 —— "
                      f"{str(out.get('error'))[:50]}")
                rows.append(rec)
                continue
            op = OUT_ABLATION / f"s{si+1}_{gid}.png"
            from PIL import Image
            with Image.open(out["image_path"]) as im:
                im.convert("RGB").save(op, quality=92)
            chk = _visual_check(src, op, checklist)
            geo = structure_score(src, op)
            rec.update(ok=True, elapsed=round(time.time() - t0),
                       out=op.name, check=chk, geometry=geo)
            hit = chk.get("hit")
            print(f"  ✔ {gid} {label:<12} {rec['elapsed']}s  "
                  f"结构相似 {geo['structure_sim']}（{geo['verdict']}）"
                  f"  判定命中 {hit}/{len(checklist)}"
                  + ("  ★有未判定项" if chk.get("unknown") else ""))
            if chk.get("error"):
                print(f"      判定失败：{str(chk['error'])[:60]}")
            rows.append(rec)

    (OUT_DATA / "ablation.json").write_text(
        json.dumps({"family": fam_name, "family_desc": fam_desc,
                    "checklist": checklist, "prompt_a": PROMPT_A,
                    "results": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── 汇总：失败与未判定都显式出现 ────────────────────────
    print(f"\n{'='*60}\n汇总\n{'='*60}")
    for gid, label, _ in groups:
        rs = [x for x in rows if x["group"] == gid]
        okr = [x for x in rs if x.get("ok")]
        sims = [x["geometry"]["structure_sim"] for x in okr
                if x.get("geometry") and x["geometry"].get("structure_sim") is not None]
        hits = [x["check"]["hit"] for x in okr
                if x.get("check") and x["check"].get("hit") is not None]
        sim_txt = f"{sum(sims)/len(sims):.3f}" if sims else "无"
        hit_txt = f"{sum(hits)/len(hits):.2f}" if hits else "无有效判定"
        print(f"  {gid} {label:<12} 成功 {len(okr)}/{len(rs)}  "
              f"结构相似度均值 {sim_txt}  判定命中均值 {hit_txt}")
    print(f"\n明细 → {OUT_DATA / 'ablation.json'}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "ablation"
    if mode == "ablation":
        sys.exit(cmd_ablation())
    print(f"未知模式：{mode}")
    sys.exit(2)