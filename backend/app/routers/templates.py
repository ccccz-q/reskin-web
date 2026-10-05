"""模板 / 家族路由 —— 前端所有可选风格的来源

════════ 为什么把 families 单独开一层 ════════

方案 §3.10.9 的核心杠杆是「**params 即 UI schema**」：
家族 YAML 里声明的参数表，直接就是前端要渲染的表单。
这样新增一个家族 = 新增一个 YAML，**前端零改动**。

要让这件事成立，后端必须把「参数的完整描述」吐出去：
type（enum / range / bool / string）、options、range、default、required、label。
`GET /api/families/{id}` 就是干这个的。

顺带修掉旧版的几个毛病：
- 旧 `/api/templates` 直接 `t["icon"]` —— 模板没写 icon 就 KeyError → 500
- 完全没有 families 端点，导致前端拿不到六大家族
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import IMAGE_STORAGE_DIR, TEMPLATES_DIR                # noqa: E402
from infra.logging import audit, logger                             # noqa: E402
from services.family_renderer import family_meta, params_schema  # noqa: E402
from services.prompt_builder import available_sources            # noqa: E402
from services.template_manager import (                          # noqa: E402
    TemplateError,
    clear_cache,
    get_family_by_id,
    get_template_by_id,
    inventory,
    load_families,
    load_templates,
)

router = APIRouter(prefix="/api", tags=["catalog"])

# ★ 随仓库内置的家族（不可删除）。其余（工坊提炼安装的）允许用户从主页面删除。
BUILTIN_FAMILY_IDS = {
    "doodle_narrators", "epic_silhouette", "full_restyle", "material_pixel",
    "risograph_travel_print", "second_world", "split_poster",
    "surreal_collage", "zine",
}

_RUNTIME_FAMILIES_DIR = Path(TEMPLATES_DIR) / "families"


def _spec_dir_for_families() -> Path | None:
    """规格层目录（提示词框架/families）—— 安装时同步写入的地方，删除时同步清理。"""
    for parent in Path(TEMPLATES_DIR).parents:
        cand = parent / "提示词框架" / "families"
        if cand.is_dir():
            return cand
    return None


@router.get("/templates", summary="列出所有提示词模板")
async def list_templates() -> dict:
    try:
        templates = load_templates()
    except TemplateError as e:
        raise HTTPException(500, f"模板加载失败：{e}") from e
    return {
        "count": len(templates),
        "items": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "icon": t.get("icon", "🎨"),          # 旧版写死 t["icon"]，缺字段就 500
                "description": t.get("description", ""),
                "recommended_scenes": t.get("recommended_scenes", []),
                "style_tags": t.get("style_tags", []),
                "family_id": t.get("family_id"),
                "default_params": t.get("default_params", {}),
            }
            for t in templates
        ],
    }


@router.get("/families", summary="列出所有创作家族（含完整参数表）")
async def list_families(full: bool = False) -> dict:
    """full=true 时带上完整 params schema（前端表单直接照它渲染）"""
    try:
        fams = load_families()
    except TemplateError as e:
        raise HTTPException(500, f"家族加载失败：{e}") from e

    # ★ 家族示例图：storage/images/examples/<family_id>.jpg
    #   来源是提示词规范里的真实生成图（或用户出图后手动放入的同名文件）。
    #   一个家族"长什么样"文字说不清，一张图就够。
    #   URL 带文件 mtime 作版本号：手动替换同名文件后浏览器会立刻拉新图，
    #   不会因为 URL 没变而一直显示磁盘缓存里的旧图。
    ex_dir = IMAGE_STORAGE_DIR / "examples"
    examples = {}
    if ex_dir.is_dir():
        for f in ex_dir.iterdir():
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            try:
                version = int(f.stat().st_mtime)
            except OSError:
                version = 0
            examples[f.stem.lower()] = f"/images/examples/{f.name}?v={version}"

    items = []
    skipped = []
    for f in fams:
        # ★ 单家族隔离（实测 2026-10-01）：一个结构畸形的家族（提炼产物）曾让
        #   family_meta 抛异常 → 整个 /api/families 500 → 主页面一个家族都看不到。
        #   坏家族跳过并记录，绝不毒害其它家族的展示。
        try:
            meta = family_meta(f)
        except Exception as e:
            fid = str(f.get("id") or f.get("name") or "?")
            skipped.append(fid)
            logger.warning("家族 %s 元信息构建失败，已跳过：%s", fid, e)
            continue
        if full:
            meta = {**meta, "example": examples.get(str(meta["id"]).lower())}
            items.append(meta)
        else:
            items.append({
                "id": meta["id"],
                "name": meta["name"],
                "icon": meta["icon"],
                "description": meta["description"],
                "layout": meta["layout"],
                "default_aspect": meta["default_aspect"],
                "suitable": meta["suitable"],
                "variants": meta["variants"],
                "required": [p["name"] for p in meta["params"] if p["required"]],
                "param_count": len(meta["params"]),
                "example": examples.get(str(meta["id"]).lower()),
            })
    return {"count": len(items), "items": items}


class FamilyExampleRequest(BaseModel):
    image_url: str = Field(..., min_length=1, max_length=1000)


@router.delete("/families/{family_id}", summary="删除一个工坊安装的家族（内置家族不可删）")
async def delete_family(family_id: str) -> dict:
    """把家族从主页面移除：运行时 YAML + 规格层 YAML + 示例图一并清理，
    「我的库」里指向它的已安装标记同步复位。内置家族拒绝删除。
    """
    if family_id in BUILTIN_FAMILY_IDS:
        raise HTTPException(403, "内置家族不支持删除")
    # ★ 按**文件存在性**判断，不走 load_families —— 畸形 YAML 的坏家族会被加载器
    #   静默跳过（主页面看不到、get_family_by_id 返回 None），若按加载结果判断，
    #   坏家族就永远删不掉 —— 那正是「主页面与我的库不一致」的一种实测来源。
    runtime_yaml = _RUNTIME_FAMILIES_DIR / f"{family_id}.yaml"
    spec_dir = _spec_dir_for_families()
    spec_yaml = (spec_dir / f"{family_id}.yaml") if spec_dir else None
    if not runtime_yaml.exists() and not (spec_yaml and spec_yaml.exists()):
        raise HTTPException(404, f"家族 {family_id} 不存在")

    removed: list[str] = []
    # 1) 运行时层
    if runtime_yaml.exists():
        runtime_yaml.unlink()
        removed.append(str(runtime_yaml))
    # 2) 规格层（保证 sync_families.py --check 不因孤儿文件报错）
    if spec_yaml and spec_yaml.exists():
        spec_yaml.unlink()
        removed.append(str(spec_yaml))
    # 3) 示例图
    ex = IMAGE_STORAGE_DIR / "examples"
    if ex.is_dir():
        for f in ex.iterdir():
            if f.is_file() and f.stem.lower() == family_id.lower():
                f.unlink()
                removed.append(str(f))
    # 4) 缓存（不清的话 /api/families 还会返回已删除的家族 —— 「删了还在」实测教训）
    try:
        clear_cache()
    except Exception as e:
        logger.warning("删除家族后清缓存失败：%s", e)
    # 5) 我的库：指向该家族的记录复位 installed 标记（保持两侧一致）
    unmarked = 0
    try:
        from services.context_store import mark_forge_uninstalled
        unmarked = mark_forge_uninstalled(family_id)
    except Exception as e:
        logger.warning("删除家族后复位库标记失败：%s", e)

    audit("family_deleted", family_id=family_id, removed=len(removed),
          library_unmarked=unmarked)
    logger.info("已删除家族 %s（%d 个文件，库标记复位 %d 条）",
                family_id, len(removed), unmarked)
    return {"ok": True, "family_id": family_id, "removed": removed,
            "library_unmarked": unmarked}


@router.post("/families/{family_id}/example", summary="给家族设置示例图")
async def set_family_example(family_id: str, req: FamilyExampleRequest) -> dict:
    """把存储目录里的一张图设为该家族的示例图

    落点固定为 `storage/images/examples/<family_id>.<ext>` —— 与 /api/families
    的扫描规则一致，下次拉列表就会带上 example 字段（带 mtime 版本号，浏览器不会吃旧缓存）。

    ★ 安全：image_url 必须过 normalize_reference（存储目录白名单校验），越界一律拒绝。
      否则这就是一个「把服务器任意文件复制给用户」的洞。
    """
    import shutil

    from agents.image_agent import AgentInputError, normalize_reference

    fam = get_family_by_id(family_id)
    if not fam:
        raise HTTPException(404, {"message": f"没有这个家族：{family_id}"})

    try:
        src = normalize_reference(req.image_url)
    except AgentInputError as e:
        raise HTTPException(400, f"图片不在允许的目录内：{e}") from e
    if not src or not os.path.exists(src):
        raise HTTPException(404, "图片不存在")

    ext = os.path.splitext(src)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(415, f"不支持的图片格式：{ext or '（无扩展名）'}")

    ex_dir = IMAGE_STORAGE_DIR / "examples"
    try:
        ex_dir.mkdir(parents=True, exist_ok=True)
        dest = ex_dir / f"{family_id}{ext}"
        shutil.copy2(src, dest)
    except OSError as e:
        raise HTTPException(500, f"写入示例图失败：{e}") from e

    version = int(os.path.getmtime(dest))
    audit("family_example_set", family_id=family_id, file=dest.name)
    return {"ok": True, "family_id": family_id,
            "example": f"/images/examples/{dest.name}?v={version}"}


@router.get("/families/{family_id}", summary="查看某个家族的完整参数表")
async def get_family(family_id: str) -> dict:
    fam = get_family_by_id(family_id)
    if not fam:
        ids = [f.get("id") for f in load_families()]
        raise HTTPException(404, {"message": f"没有这个家族：{family_id}", "available": ids})

    meta = family_meta(fam)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "icon": meta["icon"],
        "description": meta["description"],
        "layout": meta["layout"],
        "default_aspect": meta["default_aspect"],
        "suitable": meta["suitable"],
        "forbid_scope": meta["forbid_scope"],
        "allow_change": meta["allow_change"],
        "variants": meta["variants"],
        "params": params_schema(fam),
        "required": [p["name"] for p in params_schema(fam) if p["required"]],
    }


@router.get("/sources", summary="列出所有可生成来源（模板 + 家族）")
async def list_sources() -> dict:
    """前端下拉框用 —— 不用自己合并 templates 和 families 两份数据"""
    items = available_sources()
    return {"count": len(items), "items": items}


@router.get("/inventory", summary="模板目录清单（排障用）")
async def get_inventory() -> dict:
    try:
        return inventory()
    except TemplateError as e:
        raise HTTPException(500, f"模板加载失败：{e}") from e


@router.post("/templates/reload", summary="清空模板缓存，重新加载 YAML")
async def reload_templates() -> dict:
    """改了家族 YAML 之后不用重启进程

    ★ 为什么必须有这个端点（审查发现 P0-4）
    ------------------------------------
    template_manager 的加载结果挂在 @lru_cache 上，且 clear_cache()
    曾经全项目零调用。而 sync_families.py 只是把文件拷进运行时目录，
    不会通知任何进程。组合效果：

        跑 sync_families.py → 看到 ✓ 已同步 → 刷新页面 → 还是旧提示词

    不报错、不告警、完全静默 —— 第 7 次改 YAML 时必然踩到。
    """
    from services.template_manager import clear_cache

    clear_cache()
    try:
        inv = inventory()
    except TemplateError as e:
        raise HTTPException(500, f"重载失败（保持旧缓存语义已被清空）：{e}") from e
    return {"reloaded": True, "inventory": inv}
