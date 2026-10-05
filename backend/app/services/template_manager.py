"""模板加载器 —— 递归 + 按 kind 过滤 + 加载即校验

重写要点（对照审查报告 P0-2）
----------------------------
旧版：

    for filename in os.listdir(TEMPLATES_DIR):
        if filename.endswith(".yaml"):

两处致命问题：
  ① 不递归 → `templates/families/` 下的 6 个家族 YAML **永远读不到**
     （实测 load_templates() 只返回 1 个模板）
  ② 不看 kind → 只靠「目录名不以 .yaml 结尾」这个巧合躲过 families/
     只要有人在 templates/ 根放一个 yaml，立刻混进模板广场

而 kind 字段在 YAML 里早就写好了（`kind: template` / `kind: family`），
只是从来没人用它。现在 kind 是唯一判据。

另外：YAML 语法错误以前会被 yaml.safe_load 抛到调用栈最外层，
变成一次莫名的 500。现在「加载即校验」，文件名和行号直接进报错。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from config import TEMPLATES_DIR
from infra.logging import logger

KIND_TEMPLATE = "template"
KIND_FAMILY = "family"
VALID_KINDS = (KIND_TEMPLATE, KIND_FAMILY)

# 家族 YAML 必需字段 / 段名 —— ★ 单一真源在 family_renderer
# （这里曾经各存一份，是「同一契约两处定义」的典型物证。
#   两份常量只要有一处改动就会判定分叉，现在统一从渲染器导入。）
from services.family_renderer import (  # noqa: E402
    FAMILY_REQUIRED,
    SEGMENT_NAMES,
)

__all__ = [
    "FAMILY_REQUIRED",
    "SEGMENT_NAMES",
    "TemplateError",
    "clear_cache",
    "get_family_by_id",
    "get_template_by_id",
    "inventory",
    "load_documents",
    "load_families",
    "load_templates",
]


class TemplateError(Exception):
    """模板 / YAML 加载失败 —— 携带文件名，便于定位"""


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行）" if mark else ""
        raise TemplateError(f"YAML 语法错误 {path.name}{where}: {e}") from e
    except OSError as e:
        raise TemplateError(f"读取失败 {path.name}: {e}") from e

    if not isinstance(data, dict):
        raise TemplateError(f"{path.name} 顶层不是映射，实际是 {type(data).__name__}")
    return data


def _validate_family(doc: dict, source: str) -> None:
    """加载期校验 —— **委托给 family_renderer 的强校验**

    ★ 为什么必须只有一份校验（审查发现 P0-3）
    ----------------------------------------
    这里曾经是一套独立的弱校验，而 family_renderer.validate_family 是强校验
    （多了 enum→dicts 映射、占位符来源等检查）。同一个 YAML：

        template_manager: 通过 ✓
        family_renderer : 拒绝 ✗

    而消费链是：`/api/families` 用 template_manager 列出（弱校验放行），
    `/api/image/render` 用 renderer 渲染（强校验炸掉）→
    一个「能列出、能选中、一点渲染就 500」的家族就这么上线了。

    renderer 模块零本地依赖（只用 yaml + 标准库），被这里 import 是安全的。
    """
    from services.family_renderer import FamilyRenderError, validate_family as strong_validate

    missing = [k for k in FAMILY_REQUIRED if k not in doc]
    if missing:
        raise TemplateError(f"家族 {source} 缺必需字段: {missing}")

    try:
        strong_validate(doc, source=source)
    except FamilyRenderError as e:
        raise TemplateError(f"家族 {source} 校验失败：{e}") from e


def load_documents(kind: str | None = None) -> list[dict]:
    """递归加载并校验 —— **逐个文件隔离失败**

    ★ 为什么要隔离（审查发现 P2-6 的健壮性缺口）
    -------------------------------------------
    旧实现里 `_read_yaml` / `_validate_family` 抛的 TemplateError 会一路
    冒出 load_documents，于是：

        只要有一个家族 YAML 写坏了 → 整批加载失败 →
        /api/families、/api/health、/api/inventory 全线 500 →
        现场演示时「六个风格一个都用不了」

    但「静默跳过坏文件」是另一个极端，同样不能接受。
    所以取中间：**跳过坏的那个，其余照常可用，同时把错误高调收集起来**
    暴露给 /api/health、/api/inventory 和启动自检。
    ——可用性优先，但绝不掩盖问题。
    """
    if kind is not None and kind not in VALID_KINDS:
        raise ValueError(f"未知 kind: {kind!r}，可选 {VALID_KINDS}")

    root = Path(TEMPLATES_DIR)
    if not root.is_dir():
        # 目录都找不到是更根本的问题，这个必须硬失败
        raise TemplateError(f"模板目录不存在: {root}")

    docs: list[dict] = []
    seen: dict[str, str] = {}
    errors: list[dict] = []

    for path in sorted(root.rglob("*.y*ml")):
        if not path.is_file():
            continue
        # ★ 下划线开头的 YAML 是**配置/片段**，不进文档索引。
        #   例：`_labels.yaml`（参数与选项的中文名映射）。没有这条规则的话，
        #   它会被当成家族文档去校验，然后因为「缺少 id」被记为 load_errors →
        #   /api/health 的 healthy 变成 false，一个纯配置问题会装成加载故障。
        if path.name.startswith("_"):
            continue
        try:
            doc = _read_yaml(path)

            doc_kind = doc.get("kind") or KIND_TEMPLATE     # 老模板无 kind 时向后兼容
            if doc_kind == KIND_FAMILY:
                _validate_family(doc, source=path.name)
        except TemplateError as e:
            logger.error("模板加载失败（已跳过，其余不受影响）：%s", e)
            errors.append({"file": path.name, "error": str(e)})
            continue

        doc["_kind"] = doc_kind
        doc["_path"] = str(path)
        doc["_file"] = path.name

        doc_id = doc.get("id")
        if not doc_id:
            errors.append({"file": path.name, "error": "缺少 id，未进入索引"})
            continue                                     # 没 id 的是片段，不进索引

        if doc_id in seen:
            msg = f"模板 id 重复：{doc_id} 同时出现在 {seen[doc_id]} 和 {path.name}"
            logger.error("%s（已保留先出现的那份）", msg)
            errors.append({"file": path.name, "error": msg})
            continue

        seen[doc_id] = path.name

        if kind is None or doc_kind == kind:
            docs.append(doc)

    _load_errors.clear()
    _load_errors.extend(errors)
    return docs


_load_errors: list[dict] = []


def load_errors() -> list[dict]:
    """上一次加载中被跳过的文件及其原因 —— 给健康检查与启动自检用"""
    return [dict(e) for e in _load_errors]


@lru_cache(maxsize=8)
def _cached(kind: str | None) -> tuple[dict, ...]:
    """进程内缓存 —— 避免每个请求重复解析 YAML

    返回元组（不可变语义）。改完 YAML 想热更新 → 调 clear_cache()。
    """
    return tuple(load_documents(kind))


def clear_cache() -> None:
    _cached.cache_clear()
    _id_index.cache_clear()          # 索引与文档必须同生同灭，否则装新家族后查不到


# ─────────────── 对外 API ───────────────

def load_templates() -> list[dict]:
    return [dict(d) for d in _cached(KIND_TEMPLATE)]


def load_families() -> list[dict]:
    return [dict(d) for d in _cached(KIND_FAMILY)]


# ★ id → 文档索引（审查速度项：旧实现每次 get_*_by_id 都把**全部**文档浅拷贝一遍
#   再线性扫描 —— O(n) 拷贝发生在每个渲染请求上。工坊家族会持续增多，
#   这个成本随安装数量线性涨。索引基于 _cached 的同一批 dict 引用，
#   命中后仍返回顶层浅拷贝，调用方改顶层不会污染缓存 —— 与旧语义一致。
@lru_cache(maxsize=4)
def _id_index(kind: str) -> dict[str, dict]:
    return {d["id"]: d for d in _cached(kind) if d.get("id")}


def get_template_by_id(template_id: str) -> dict | None:
    d = _id_index(KIND_TEMPLATE).get(template_id)
    return dict(d) if d else None


def get_family_by_id(family_id: str) -> dict | None:
    d = _id_index(KIND_FAMILY).get(family_id)
    return dict(d) if d else None


def inventory() -> dict:
    """给健康检查用的清单：确认所有文档都被正确识别"""
    all_docs = load_documents()
    by_kind: dict[str, list[str]] = {}
    for d in all_docs:
        by_kind.setdefault(d["_kind"], []).append(d["id"])
    errs = load_errors()
    return {
        "templates_dir": str(Path(TEMPLATES_DIR).resolve()),
        "total": len(all_docs),
        "by_kind": {k: sorted(v) for k, v in by_kind.items()},
        # ★ 被跳过的文件必须出现在这里 —— 「其余可用」不等于「一切正常」
        "errors": errs,
        "healthy": not errs,
    }


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        info = inventory()
    except TemplateError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(f"模板目录: {info['templates_dir']}")
    print(f"文档总数: {info['total']}")
    for kind, ids in info["by_kind"].items():
        print(f"  {kind:9} ({len(ids)}): {', '.join(ids)}")
