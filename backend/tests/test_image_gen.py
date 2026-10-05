"""生图层验收 —— 画幅选择、重试计划、失败如实回报

    python tests/test_image_gen.py

★★ 为什么单独一个文件
----------------------
services/image_generator.py 是**唯一会真花钱**的模块，但此前没有专门的测试：
画幅怎么选、重试几次、失败时汇报什么，全靠人肉在真机上试。
后果是同一个坑踩了两次 —— 「正方形原图被强改成竖版」在 10-03 修过一次，
换到 gpt-image 后端又原样复现（家族画幅 3:4 被硬映射到 1024×1536）。

这个文件把那几条规则钉住，全部离线（不调用真实 API、不花钱）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

# 必须先设好后端，因为 image_generator 在 import 时按它构造客户端
os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

from PIL import Image  # noqa: E402

import config  # noqa: E402
from services import image_generator as ig  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


TMP = Path(__file__).resolve().parent / "_img_tmp"
TMP.mkdir(exist_ok=True)


def make(path: Path, size: tuple[int, int]) -> str:
    Image.new("RGB", size, (200, 190, 170)).save(path)
    return str(path)


square = make(TMP / "square.jpg", (1024, 1024))
portrait = make(TMP / "portrait.jpg", (900, 1600))
landscape = make(TMP / "landscape.jpg", (1600, 900))

print("=== 1. 画幅选择：表达不了就跟随原图（原图保真是立身之本）===")
check("方形原图 + 家族 3:4 → 出方图（不再变竖版）",
      ig.choose_oai_size(square, "3:4") == "1024x1024",
      ig.choose_oai_size(square, "3:4"))
check("方形原图 + 家族 4:5 → 出方图",
      ig.choose_oai_size(square, "4:5") == "1024x1024")
check("竖版原图 + 家族 3:4 → 出竖版",
      ig.choose_oai_size(portrait, "3:4") == "1024x1536")
check("横版原图 + 家族 3:4（表达不了）→ 跟随原图出横版",
      ig.choose_oai_size(landscape, "3:4") == "1536x1024")
check("横版原图 + 家族 2:3（能表达）→ 尊重家族出竖版",
      ig.choose_oai_size(landscape, "2:3") == "1024x1536")

print()
print("=== 2. 画幅选择：能精确表达时尊重家族 ===")
check("家族 2:3 → 竖版档", ig.choose_oai_size(square, "2:3") == "1024x1536")
check("家族 3:2 → 横版档", ig.choose_oai_size(square, "3:2") == "1536x1024")
check("家族 1:1 → 方档", ig.choose_oai_size(portrait, "1:1") == "1024x1024")

print()
print("=== 3. 画幅选择：origin / 空值 / 读不到图 ===")
check("aspect=origin → 跟随原图", ig.choose_oai_size(portrait, "origin") == "1024x1536")
check("aspect=None → 跟随原图", ig.choose_oai_size(landscape, None) == "1536x1024")
check("aspect 为空串 → 跟随原图", ig.choose_oai_size(square, "") == "1024x1024")
check("图不存在时不崩、退回方图", ig.choose_oai_size(str(TMP / "nope.jpg"), "3:4")
      == "1024x1024")

print()
print("=== 4. 画幅选择：aspect_wins=True 时取最接近的一档 ===")
check("3:4 最接近 2:3", ig.choose_oai_size(square, "3:4", True) == "1024x1536")
check("4:5 最接近 2:3", ig.choose_oai_size(square, "4:5", True) == "1024x1536")
check("16:9 最接近 3:2", ig.choose_oai_size(square, "16:9", True) == "1536x1024")
check("5:4 最接近 3:2", ig.choose_oai_size(square, "5:4", True) == "1536x1024")
check("乱写的画幅不会崩", ig.choose_oai_size(square, "abc", True) in
      ig.OAI_ASPECT_SIZES.values())

print()
print("=== 5. 重试计划：默认不换模型（换模型=换产出，是降质不是兜底）===")
check("默认只用主模型", ig.image_models() == [ig.IMAGE_MODEL], str(ig.image_models()))
plan = ig.image_plan()
check("主模型 5 次", len(plan) == 5, str(len(plan)))
check("退避 0/5/15/30/45s（容量空窗期实测可长达数十秒）",
      [w for _, w in plan] == [0.0, 5.0, 15.0, 30.0, 45.0], str([w for _, w in plan]))
check("累计等待 ≈95s（够扛一次中等空窗，又不至于让用户干等）",
      abs(sum(w for _, w in plan) - 95.0) < 0.01, str(sum(w for _, w in plan)))

# ★ 超时与预算必须分开：单次上限 ≠ 整轮上限
check("单次尝试上限已收紧（不再单次挂 300s）",
      config.IMAGE_ATTEMPT_TIMEOUT_SEC <= 180, str(config.IMAGE_ATTEMPT_TIMEOUT_SEC))
check("整轮预算有限（空窗期不无限等）",
      0 < config.IMAGE_TOTAL_BUDGET_SEC <= 600, str(config.IMAGE_TOTAL_BUDGET_SEC))
_worst = config.IMAGE_ATTEMPT_TIMEOUT_SEC * len(plan)
check(f"最坏最坏也不超过预算太多（{_worst}s 的单次上限之和 vs {config.IMAGE_TOTAL_BUDGET_SEC}s 预算）",
      config.IMAGE_TOTAL_BUDGET_SEC < _worst,
      "预算必须严格小于「每次都用满超时」的乘积，否则 deadline 形同虚设")

_saved = ig.IMAGE_FALLBACK_MODELS
ig.IMAGE_FALLBACK_MODELS = ("gpt-image-2.5-flare", ig.IMAGE_MODEL)
plan2 = ig.image_plan()
check("显式配了备用模型才扩展（5 + 2）", len(plan2) == 7, str(len(plan2)))
check("备用模型去重（主模型不会重复排队）",
      [m for m, _ in plan2].count(ig.IMAGE_MODEL) == 5)
check("备用模型排在主模型之后", plan2[5][0] == "gpt-image-2.5-flare")
ig.IMAGE_FALLBACK_MODELS = _saved

print()
print("=== 6. 错误分诊：与文本通道同一口径 ===")


class Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


for label, exc, expect in [
    ("503 没有可用通道", Err("Error code: 503 - No available compatible account", 503), True),
    ("502", Err("502 Bad Gateway", 502), True),
    ("429 限流", Err("429 rate limit", 429), True),
    ("上游断流", Err("Upstream stream ended without a terminal response event"), True),
    ("超时", Err("Request timed out."), True),
    ("401 鉴权", Err("401 unauthorized", 401), False),
    ("400 参数非法", Err("400 invalid size", 400), False),
]:
    check(f"{label} → {'重试' if expect else '立刻失败'}",
          ig._is_transient_image_error(exc) is expect)

print()
print("=== 7. 尺寸解析仍然守住下限（不被本次改动带偏）===")
check("1k/2k/4k 原样接受", ig.resolve_size(square, "2k") == "2k")
try:
    ig.resolve_size(square, "640x480")
    check("低于下限的尺寸应报错", False)
except ValueError as e:
    check("低于下限的尺寸报错", "低于模型下限" in str(e), str(e)[:60])
check("origin → 按原图分辨率选档", ig.resolve_size(square, None, "origin") in ("2k", "4k"))

print()
print("=== 8. 画幅校验的判据：跟「我们要求的尺寸」比，不是跟原图比 ===")
# ★ 这条曾经是误报重灾区（2026-10-04 用户报「总是报生成尺寸不符」）：
#   旧实现拿输出跟**原图比例**比，而 gpt-image 只有 1:1 / 2:3 / 3:2 三档 ——
#   于是任何 4:3 / 3:4 / 16:9 的原图注定偏差 25%，每一张非方图都弹警告。
_ISO = {
    "1600x1200": "1536x1024",   # 4:3  → 取 3:2
    "1200x1600": "1024x1536",   # 3:4  → 取 2:3
    "1080x1440": "1024x1536",   # 手机竖图
    "1024x1024": "1024x1024",   # 方图
    "2560x1080": "1536x1024",   # 21:9 宽幅
}
for _ref, _asked in _ISO.items():
    _w, _h = (int(x) for x in _ref.split("x"))
    _r = TMP / f"iso_ref_{_ref}.png"
    Image.new("RGB", (_w, _h), (30, 60, 90)).save(_r)
    _ow, _oh = (int(x) for x in _asked.split("x"))
    _o = TMP / f"iso_out_{_ref}.png"
    Image.new("RGB", (_ow, _oh), (200, 180, 160)).save(_o)
    check(f"原图 {_ref} → 要 {_asked} 且上游照做 → 不该报警",
          ig._aspect_mismatch_warning(str(_o), _asked, str(_r)) == "",
          ig._aspect_mismatch_warning(str(_o), _asked, str(_r))[:34])

# 真问题仍要抓到：上游给了别的尺寸
_r = TMP / "iso_ref2.png"
Image.new("RGB", (1600, 1200), (1, 2, 3)).save(_r)
_o = TMP / "iso_out2.png"
Image.new("RGB", (1024, 1024), (1, 2, 3)).save(_o)
_w = ig._aspect_mismatch_warning(str(_o), "1536x1024", str(_r))
check("要 3:2 却给了方图 → 仍报警（这才是真问题）", bool(_w), _w[:34])
_o2 = TMP / "iso_out3.png"
Image.new("RGB", (1024, 1536), (1, 2, 3)).save(_o2)
check("要 3:2 却给了竖图 → 仍报警", bool(ig._aspect_mismatch_warning(str(_o2), "1536x1024", str(_r))))
check("拿不到请求尺寸时静默跳过（不崩不乱报）",
      ig._aspect_mismatch_warning(str(_o), None, str(_r)) == ""
      and ig._aspect_mismatch_warning(str(_o), "abc", str(_r)) == "")
check("坏路径也不炸", ig._aspect_mismatch_warning("/nope/x.png", "1024x1024") == "")

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
