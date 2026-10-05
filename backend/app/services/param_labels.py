"""参数与选项的中文名解析 —— 「params 即 UI schema」的中文那半

数据源是 `templates/_labels.yaml`（下划线开头 = 配置，不进家族索引）。
这里只做三件事：读它、缓存它、按三层顺序查一个名字出来。

★★ 为什么中文名必须来自后端
--------------------------
前端 ParamForm 里曾经有一张 `LABELS` 兜底表 —— 能跑，但它是**第二份真源**：
后端加一个参数，前端不补翻译就显示英文；后端改一个名字，两边就悄悄分叉。
实测症状就是「风格/颜色/渲染显示成 style/palette_source/render」。

现在 schema 自带 `label` 与 `option_labels`，前端退化成「有就用、没有才兜底」，
新增家族依然是零前端改动 —— 这是项目一开始就定的杠杆，不该在翻译这里破例。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml                                                       # noqa: E402

from config import TEMPLATES_DIR                                  # noqa: E402
from infra.logging import logger                                  # noqa: E402

LABELS_FILE = Path(TEMPLATES_DIR) / "_labels.yaml"

_EMPTY: dict = {"params": {}, "options": {}, "by_param": {}}


@lru_cache(maxsize=1)
def _load() -> dict:
    """读标签文件。任何问题都降级成空表 —— 翻译缺失绝不该让服务起不来。"""
    try:
        raw = yaml.safe_load(LABELS_FILE.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("标签文件不存在，UI 将回退到参数名：%s", LABELS_FILE)
        return _EMPTY
    except yaml.YAMLError as e:
        logger.error("标签文件 YAML 语法错误（UI 回退到参数名）：%s", e)
        return _EMPTY
    except OSError as e:
        logger.error("标签文件读取失败：%s", e)
        return _EMPTY

    if not isinstance(raw, dict):
        return _EMPTY

    def _str_map(key: str) -> dict:
        src = raw.get(key) or {}
        if not isinstance(src, dict):
            return {}
        # YAML 里 `1:1` / `0` 这类键会被解析成非字符串，统一转成 str
        return {str(k): str(v) for k, v in src.items() if v is not None}

    by_param: dict[str, dict] = {}
    raw_bp = raw.get("by_param") or {}
    if isinstance(raw_bp, dict):
        for pname, mapping in raw_bp.items():
            if isinstance(mapping, dict):
                by_param[str(pname)] = {
                    str(k): str(v) for k, v in mapping.items() if v is not None
                }

    return {"params": _str_map("params"), "options": _str_map("options"),
            "by_param": by_param}


def clear_cache() -> None:
    _load.cache_clear()


def param_label(name: str) -> str:
    """参数的中文名；查不到就原样返回参数名（绝不返回空串）"""
    return _load()["params"].get(name) or name


def option_label(param: str, value: str) -> str:
    """选项的中文名。三层顺序：by_param → options → 原 token"""
    d = _load()
    v = str(value)
    per_param = d["by_param"].get(param) or {}
    if v in per_param:
        return per_param[v]
    if v in d["options"]:
        return d["options"][v]
    return v


def option_labels(param: str, values: list) -> dict:
    """一次把一个参数的所有选项翻好，返回 {token: 中文}"""
    return {str(v): option_label(param, str(v)) for v in values}


def stats() -> dict:
    d = _load()
    return {
        "file": str(LABELS_FILE),
        "exists": LABELS_FILE.exists(),
        "param_labels": len(d["params"]),
        "option_labels": len(d["options"]),
        "per_param_overrides": sum(len(m) for m in d["by_param"].values()),
    }


def audit_coverage() -> list[str]:
    """列出「schema 里有、标签表里没有」的参数名与选项 token

    给自测与排障用 —— 新增家族时能立刻知道漏翻了什么，
    而不是等用户截图说「这里怎么是英文的」。
    """
    from services.family_renderer import load_families, params_schema

    d = _load()
    missing_params: set[str] = set()
    missing_options: set[str] = set()

    for fam in load_families().values():
        for item in params_schema(fam):
            name = item["name"]
            if name not in d["params"]:
                missing_params.add(name)
            for opt in item.get("options") or []:
                v = str(opt)
                per_param = d["by_param"].get(name) or {}
                if v not in per_param and v not in d["options"]:
                    missing_options.add(f"{name}={v}")

    out = []
    if missing_params:
        out.append("缺少中文名的参数：" + "、".join(sorted(missing_params)))
    if missing_options:
        out.append("缺少中文名的选项：" + "、".join(sorted(missing_options)))
    return out


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=== 标签文件 ===")
    for k, v in stats().items():
        print(f"  {k}: {v}")

    print()
    print("=== 覆盖度检查（新增家族时看这里）===")
    missing = audit_coverage()
    if missing:
        for m in missing:
            print("  ✗", m)
    else:
        print("  ✓ 所有参数与选项都有中文名")

    print()
    print("=== 抽查 ===")
    for param, value in [("style", "national_geo"), ("render", "ink_wash"),
                         ("substrate", "warm_ivory"), ("text_role", "none"),
                         ("substrate", "none"), ("figures", "0_2"),
                         ("resolution_hint", "4K"), ("aspect", "origin")]:
        print(f"  {param}={value:16} → {param_label(param)} · {option_label(param, value)}")
