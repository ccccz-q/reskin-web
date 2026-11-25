"""结构保真校验 —— 出图后自动检查「主体结构有没有被重绘」

★ 为什么要有这个模块（而不是留在测试脚本里）：
  之前 `structure_score` 只存在于 `tests/evidence_run.py`，
  **产品跑的时候根本不执行它** ——
  我们在文档里宣称"出图后做结构校验"，可代码里没有这一步。
  那是**宣称与实现不一致**，比没有这个功能更糟。
  ⇒ 把它提升为正式服务，让主链路真的跑它。

★ 这个指标能证明什么、不能证明什么（很重要，别夸口）：
  · 能**证伪**"结构被彻底重绘"—— 骨架若被重画，分数会掉
  · 不能证明"地标认得出"—— 那需要视觉语义判定（VLM）
  ⇒ 所以本模块只做**结构层**这一道；
    "地标识别"那一道留 VLM，两者分工不同，不能互相替代。

★ 设计原则：**校验绝不能成为新的失败来源**
  · 任何异常都返回 `status="unknown"`，不抛
  · 分数缺失时调用方按"通过"处理（宁可漏判，不可崩）
  · 阈值可配置，不硬编码在业务里
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path

logger = logging.getLogger("travelnote")

# ── 阈值：低于此值判定为「结构被重绘」，需要重生成 ────────────
# ★ 实测依据（2026-10-09 消融实验，六次真实生图）：
#   完整三段式 0.921~0.956　无契约 0.961~0.979（但它几乎没做风格化）
#   ⇒ 阈值定在 0.75：低于它说明骨架真被改了，
#     高于它不代表"做得好"（那是 VLM 判定的事）。
DEFAULT_MIN_SIM = float(os.getenv("STRUCTURE_MIN_SIM", "0.75"))
WARN_SIM = float(os.getenv("STRUCTURE_WARN_SIM", "0.85"))

_GRID = 8


def _edge_hist(path: str | Path, grid: int = _GRID) -> list[float] | None:
    """灰度 → Sobel 边缘 → 网格直方图（消除分辨率差异后比较骨架分布）"""
    try:
        from PIL import Image, ImageFilter
        with Image.open(str(path)) as im:
            g = im.convert("L").resize((256, 256), Image.BILINEAR)
            e = g.filter(ImageFilter.FIND_EDGES)
            px = list(e.getdata())
    except Exception as e:                                    # noqa: BLE001
        logger.warning("结构校验：读图失败 %s", type(e).__name__)
        return None
    hist = [0.0] * (grid * grid)
    W = H = 256
    for idx, v in enumerate(px):
        x, y = idx % W, idx // W
        hist[(y * grid // H) * grid + (x * grid // W)] += v
    tot = sum(hist) or 1.0
    return [v / tot for v in hist]


def structure_score(src_path: str | Path, out_path: str | Path) -> dict:
    """比较原图与成品的结构骨架相似度。

    返回（**永不抛异常**）：
      {
        "structure_sim": float | None,   # Bhattacharyya 系数，0~1
        "verdict": str,                  # 中文结论
        "status": "pass" | "warn" | "fail" | "unknown",
        "min_sim": float,                # 当次使用的阈值（便于复算）
      }
    """
    out = {
        "structure_sim": None, "verdict": "未测量",
        "status": "unknown", "min_sim": DEFAULT_MIN_SIM,
    }
    try:
        a = _edge_hist(src_path)
        b = _edge_hist(out_path)
        if a is None or b is None:
            out["verdict"] = "测量失败：无法读取图像"
            return out
        bc = sum(math.sqrt(x * y) for x, y in zip(a, b))
        sim = round(min(max(bc, 0.0), 1.0), 4)
        out["structure_sim"] = sim
        if sim >= WARN_SIM:
            out["status"], out["verdict"] = "pass", "结构高度保留"
        elif sim >= DEFAULT_MIN_SIM:
            out["status"], out["verdict"] = "warn", "结构基本保留"
        elif sim >= 0.65:
            out["status"], out["verdict"] = "fail", "结构明显改变"
        else:
            out["status"], out["verdict"] = "fail", "结构被重绘"
        return out
    except Exception as e:                                     # noqa: BLE001
        # ★ 校验失败不该让生图失败 —— 降级为 unknown，调用方按通过处理
        logger.warning("结构校验异常，降级为 unknown：%s", type(e).__name__)
        out["verdict"] = f"测量失败：{type(e).__name__}"
        return out


def check_structure(src_path: str | Path, out_path: str | Path,
                    min_sim: float | None = None) -> dict:
    """带阈值的判定入口。

    ★ 为什么单独一层：让"测量"与"判定"分开。
      测量可能失败（unknown），判定要明确告诉调用方该怎么处理
      —— 是重生成，还是照常继续。
    """
    thr = DEFAULT_MIN_SIM if min_sim is None else float(min_sim)
    r = structure_score(src_path, out_path)
    sim = r.get("structure_sim")
    r["min_sim"] = thr
    if sim is None:
        # 测不了 ⇒ 不拦（宁可漏判，不可把可用成品判死）
        r["should_regenerate"] = False
        r["verdict"] = (r.get("verdict") or "未测量") + "（不拦截）"
    else:
        r["should_regenerate"] = sim < thr
    return r