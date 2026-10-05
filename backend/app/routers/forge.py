"""路由层 · 模板工坊（/api/forge）

把「风格理论 + 参考图」变成一套可复用的家族模板，并支持反复迭代、入库、安装。

设计要点：
- 生成、迭代、安装三件事分开 —— 「不满意就重来」和「改一版」是不同动作，
  合成一个接口会让用户不敢点，因为怕丢掉已有的成果。
- 每一版都落库（同一 lineage 下 version 递增），所以「第 4 版不如第 2 版」能拿回来。
- 安装会同时写规格层与运行时层，保证 sync_families.py 的 --check 依然通过
  （否则一同步就把用户刚装进去的家族冲掉了）。
"""
from __future__ import annotations

import os
import sys
import uuid
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.image_agent import normalize_reference  # noqa: E402
from config import TEMPLATES_DIR  # noqa: E402
from infra.logging import audit, logger  # noqa: E402
from services import context_store  # noqa: E402
from services.identity import DEFAULT_SESSION, session_dep  # noqa: E402
from services.style_forge import forge as do_forge  # noqa: E402
from services.style_forge import revise as do_revise  # noqa: E402

router = APIRouter(prefix="/api/forge", tags=["forge"])


def _ownable(row: dict, sid: str) -> None:
    """写操作属主闸：内置家族（owner 空）人人可见但只读；
    自建家族仅属主可改。default 会话（本地版）全放行。"""
    if sid == DEFAULT_SESSION:
        return
    ow = (row.get("owner_session") or "").strip()
    if not ow:
        raise HTTPException(403, "内置家族是大家共享的模板，不能修改或删除")
    if ow != sid:
        raise HTTPException(404, "找不到这份草稿")   # 不泄露他人记录的存在性

def _find_spec_dir() -> Path | None:
    """向上找规格层目录（提示词框架/families）

    ★ 不要用固定层数算路径 —— 我第一版写的 `parents[2]` 就错了：
      实际结构是 换颜/项目/backend/app/templates，
      parents[2] 是「项目」，而规格层在再上一层的「换颜/提示词框架」。
      层数写死的话，换个目录深度就静默跳过规格层写入 ——
      于是运行时有新家族、规格层没有，sync_families.py --check 立刻失败，
      而用户完全不知道发生了什么（这正是我在 install 注释里警告过的场景）。
      这里改成按名字向上找，找不到就明确返回 None 并提示。
    """
    for parent in Path(TEMPLATES_DIR).parents:
        cand = parent / "提示词框架" / "families"
        if cand.is_dir():
            return cand
    return None


_SPEC_DIR = _find_spec_dir()
_RUNTIME_DIR = Path(TEMPLATES_DIR) / "families"


def _resolve_images(urls: list[str]) -> list[str]:
    """URL → 本地绝对路径。非法的一律跳过（不让用户输入带偏整条链）"""
    out = []
    for u in urls or []:
        try:
            p = normalize_reference(u)
        except Exception:
            continue
        if p and os.path.exists(p):
            out.append(p)
    return out


def _render_prompt(spec: dict, params: dict | None = None, strict: bool = False) -> str:
    """把草稿渲染成可直接复制的提示词

    草稿还没安装成家族，所以 `build_prompt(source_id=...)` 找不到它 ——
    直接走 `render_family` + `render_to_prompt` 这一对，它们是同一套装配逻辑。

    ★ strict=True 用于**安装前冒烟**：与运行时同样严格，未解析占位符直接抛错。
      旧版固定 strict=False → 带占位符缺陷的家族也能装上，最后在用户点生成时才炸
      （实测 voxel_path_reflection：装了却在画布渲染失败）。
    """
    from services.family_renderer import render_family, render_to_prompt

    r = render_family(spec, params or {}, {}, strict=strict)
    return render_to_prompt(r)


class DraftRequest(BaseModel):
    theory: str = Field("", max_length=8000)
    image_urls: list[str] = Field(default_factory=list)
    user_notes: str = Field("", max_length=2000)
    # ★ 用户收集的生图提示词（正/负向整包）：与参考图、风格理论并列为第三输入源。
    #   提炼器把它当风格词汇与质感的直接来源，负向词（SD 反向词）转为禁止项。
    style_prompt: str = Field("", max_length=8000)
    base_family_id: str = ""
    name: str = Field("", max_length=40)


class ReviseRequest(BaseModel):
    forge_id: str
    feedback: str = Field(..., min_length=1, max_length=2000)
    # ★ 用户在迭代时新贴的参考图（如理想效果图）：贴了就**用新图做视觉识别**，
    #   而不是沿用当初提炼时的参考图 —— 用户贴理想图 + 写「保留顶部界面布局」
    #   才能真正闭环（实测 2026-10-03：旧版只传文字，新贴的图被无声忽略）。
    image_urls: list[str] = Field(default_factory=list, max_length=8)


class InstallRequest(BaseModel):
    forge_id: str


class PromptOverrideRequest(BaseModel):
    # 手动修改的提示词成品：非空=生成时直接用它（跳过 spec 渲染），
    # 空串=恢复自动渲染。上限给足 16000（现有家族 prompt 最长 1340 字，
    # 用户手改通常局部增删；再大的输入几乎必然是误粘贴）。
    prompt: str = Field("", max_length=16000)


def _resolve_base(base_family_id: str):
    if not base_family_id:
        return None
    try:
        from services.template_manager import get_family_by_id
        return get_family_by_id(base_family_id)
    except Exception:
        return None


def _forge_and_save(req: DraftRequest, progress=None,
                    cancelled: "callable | None" = None,
                    owner: str = "") -> dict:
    """同步执行一次提炼并入库。draft 端点与后台任务共用；永不抛 HTTP 异常。
    cancelled：中止探测函数 —— 透传给提炼器，在阶段之间检查，命中即终止。
    owner：属主会话（公开版）；空=本地 default 模式，不写属主。
    """
    paths = _resolve_images(req.image_urls)
    base = _resolve_base(req.base_family_id)

    if not paths and not req.theory.strip():
        return {"ok": False, "error": "no_input",
                "message": "至少上传一张参考图（推荐同类型 3–6 张）；风格理论可选。"}

    # 提炼阶段用 VLM：这是「从图里看出风格」的核心，值得花这一次调用
    result = do_forge(req.theory, paths, req.user_notes, base, True,
                      style_prompt=req.style_prompt, progress=progress,
                      cancelled=cancelled)

    # ★ 用户中止：不入库、不渲染，直接把 cancelled 标记带回给任务层
    if result.get("cancelled"):
        result.setdefault("message", "已按你的要求中止，本次提炼没有保存任何版本")
        return {**result, "id": None, "lineage": result.get("spec", {}).get("id")
                or ("lineage_" + uuid.uuid4().hex[:8]),
                "version": 0, "prompt": "", "saved": False}

    # ★ 草稿无论成不成功都存下来 ——
    #   生成一次要钱，用户可能只是想先看看模型给了什么；
    #   存下来才能迭代、才能回头对比，不然「不满意」就等于白花钱。
    prompt = ""
    if result.get("ok"):
        try:
            prompt = _render_prompt(result["spec"])
        except Exception as e:
            logger.warning("草稿渲染失败（草稿仍会保存）：%s", e)
            result.setdefault("warnings", []).append(f"渲染失败：{e}")

    lineage = result.get("spec", {}).get("id") or ("lineage_" + uuid.uuid4().hex[:8])
    version = 1
    forge_id = None
    ok_flag = bool(result.get("ok"))

    # ★ 失败不入库（用户指令 2026-10-01）：失败的版本没有 spec/prompt，
    #   入库只会变成一条「未命名 / 空提示词」的神秘记录误导用户。
    #   失败原因通过任务快照与完成弹窗告知；要重试就重新提炼。
    if ok_flag:
        try:
            version = context_store.next_forge_version(lineage)
            forge_id = context_store.save_forge(
                lineage=lineage, version=version,
                spec=result.get("spec") or {},
                card=result.get("card") or {},     # ★ 视觉卡：解构一次的产物，可换场景反复编译
                name=req.name or result.get("spec", {}).get("name") or "",
                prompt=prompt, theory=req.theory,
                feedback="",
                images=[os.path.basename(p) for p in paths],
                owner_session=owner,
            )
            audit("forge_saved", forge_id=forge_id, lineage=lineage, version=version,
                  ok=True)
        except Exception as e:
            logger.error("草稿存库失败（结果仍然返回）：%s", e)
            result.setdefault("warnings", []).append(
                f"这一版没能存进模板库：{e}（内容仍然可用，可先复制走）"
            )
    else:
        reason = (str(result.get("error") or result.get("message") or "").strip()
                  or "；".join(str(x) for x in (result.get("errors") or []))
                  or "提炼未通过校验")
        audit("forge_failed_not_saved", lineage=lineage, reason=reason[:200])
    return {**result, "id": forge_id, "lineage": lineage,
            "version": version, "prompt": prompt, "saved": forge_id is not None}


# ── 后台提炼任务 ────────────────────────────────────────
# 用户可以在提炼期间离开工坊页去干别的；任务在服务端线程里继续跑，
# 完成后由前端全局轮询发现并弹窗。任务注册表存进程内存：
# 重启会丢任务 —— 但丢的只是「进度显示」，提炼线程也没了是实话，
# 所以重启后前端对 running 超过 30 分钟的任务按失败处理。
_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()
_TASK_TTL_SEC = 3600          # 完成后的任务保留 1 小时供查询，过后清理


def _task_snapshot(t: dict) -> dict:
    """对外只暴露这些字段（结果体里的大件按需再取）。"""
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "phase": t.get("phase", ""),
        "name": t.get("name", ""),
        "images": t.get("images", []),
        "elapsed_sec": round(
            (t.get("finished_at") or time.time()) - t["started_at"], 1),
        "created_at": t["started_at"],
        "cancelling": bool(t.get("cancel") and t["cancel"].is_set()),
        "error": t.get("error"),
        "result": t.get("result"),
    }


def _run_forge_task(task_id: str, req: DraftRequest):
    t = _TASKS.get(task_id, {})
    try:
        def _progress(msg: str):
            with _TASKS_LOCK:
                t["phase"] = str(msg)[:60]

        res = _forge_and_save(req, progress=_progress,
                              cancelled=t.get("cancel").is_set if t.get("cancel") else None,
                              owner=t.get("owner") or "")
        with _TASKS_LOCK:
            t["result"] = {
                "ok": bool(res.get("ok")),
                "cancelled": bool(res.get("cancelled")),
                "id": res.get("id"),
                "lineage": res.get("lineage"),
                "version": res.get("version"),
                "name": (res.get("spec") or {}).get("name") or req.name or "未命名",
                "errors": (res.get("errors") or [])[:4],
                "warnings": (res.get("warnings") or [])[:4],
                "report": res.get("report") or {},
                "message": res.get("message"),
            }
            if res.get("cancelled"):
                t["status"] = "cancelled"
                t["error"] = res.get("message") or "已按你的要求中止"
            else:
                t["status"] = "done" if res.get("ok") else "failed"
                if not res.get("ok"):
                    t["error"] = res.get("error") or res.get("message") or "提炼未通过校验"
            t["finished_at"] = time.time()
        audit("forge_task_done", task_id=task_id,
              ok=bool(res.get("ok")), cancelled=bool(res.get("cancelled")))
    except Exception as e:                     # 线程内绝不让异常静默
        logger.exception("后台提炼任务失败 %s", task_id)
        with _TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = f"{type(e).__name__}: {e}"
            t["finished_at"] = time.time()


@router.post("/draft", summary="同步提炼（等待完成再返回；前台在工坊页时可走这条）")
async def create_draft(req: DraftRequest, sid: str = Depends(session_dep)):
    from fastapi.concurrency import run_in_threadpool
    owner = sid if sid != DEFAULT_SESSION else ""
    res = await run_in_threadpool(_forge_and_save, req, None, None, owner)
    if res.get("error") == "no_input":
        raise HTTPException(422, {"code": "forge_no_input", "message": res.get("message")})
    return res


@router.post("/draft-async", summary="后台提炼：立即返回任务 id，提炼在线程里继续")
async def create_draft_async(req: DraftRequest, sid: str = Depends(session_dep)):
    paths = _resolve_images(req.image_urls)
    if not paths and not req.theory.strip():
        raise HTTPException(422, {"code": "forge_no_input",
                                  "message": "至少上传一张参考图（推荐同类型 3–6 张）；风格理论可选。"})
    # 清理过期任务，防注册表无限膨胀
    now = time.time()
    owner = sid if sid != DEFAULT_SESSION else ""
    with _TASKS_LOCK:
        for tid in [k for k, v in _TASKS.items()
                    if v.get("finished_at") and now - v["finished_at"] > _TASK_TTL_SEC]:
            _TASKS.pop(tid, None)
        task_id = "ftask_" + uuid.uuid4().hex[:12]
        _TASKS[task_id] = {
            "task_id": task_id, "status": "running", "phase": "排队中",
            "name": req.name or "", "images": [os.path.basename(p) for p in paths],
            "started_at": now, "finished_at": None,
            "result": None, "error": None,
            "owner": owner,                  # ★ 任务按属主隔离：别人看不见也取消不了
            "cancel": threading.Event(),     # 协作式中止：提炼器在阶段之间检查
        }
    th = threading.Thread(target=_run_forge_task, args=(task_id, req),
                          name=f"forge-{task_id}", daemon=True)
    th.start()
    logger.info("后台提炼任务已启动 %s（%d 张图）", task_id, len(paths))
    return {"task_id": task_id, "status": "running"}


@router.get("/tasks/active", summary="当前未完成任务（全局轮询用）")
async def active_tasks(sid: str = Depends(session_dep)):
    with _TASKS_LOCK:
        running = [_task_snapshot(t) for t in _TASKS.values()
                   if t["status"] == "running"
                   and (sid == DEFAULT_SESSION or t.get("owner") == sid)]
    return {"count": len(running), "items": running}


@router.get("/tasks/{task_id}", summary="查一个后台提炼任务")
async def get_task(task_id: str, sid: str = Depends(session_dep)):
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if not t:
            raise HTTPException(404, "任务不存在（可能服务已重启，请重新提炼）")
        if sid != DEFAULT_SESSION and t.get("owner") != sid:
            raise HTTPException(404, "任务不存在（可能服务已重启，请重新提炼）")
        return _task_snapshot(t)


@router.post("/tasks/{task_id}/cancel", summary="中止一个进行中的提炼任务")
async def cancel_task(task_id: str, sid: str = Depends(session_dep)):
    """协作式中止：设置取消标志，提炼器在当前阶段结束后立即终止。
    正在进行中的那一次模型调用无法硬中断 —— 通常几秒内任务转为 cancelled。
    """
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if not t:
            raise HTTPException(404, "任务不存在（可能服务已重启或已完成）")
        if sid != DEFAULT_SESSION and t.get("owner") != sid:
            raise HTTPException(404, "任务不存在（可能服务已重启或已完成）")
        if t["status"] != "running":
            return {"ok": False, "status": t["status"],
                    "message": "任务已结束，无需中止"}
        t["cancel"].set()
        t["phase"] = "已请求中止（当前阶段结束后立即终止）"
    audit("forge_task_cancel", task_id=task_id)
    logger.info("提炼任务收到中止请求 %s", task_id)
    return {"ok": True, "status": "running",
            "message": "已请求中止 —— 当前阶段结束后任务会终止（几秒内）"}



@router.post("/revise", summary="在既有草稿上按反馈改一版")
async def revise_draft(req: ReviseRequest, sid: str = Depends(session_dep)):
    row = context_store.get_forge(req.forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    _ownable(row, sid)

    # ★ 参考图选择：用户本轮贴了新图（如理想效果图）→ 用新图并启用 VLM 视觉
    #   识别（理想图的布局/配色/界面规格会被提取进证据）；没贴 → 沿用当初
    #   提炼时的参考图，行为与旧版一致。
    new_paths = _resolve_images(req.image_urls) if req.image_urls else []
    use_new = bool(new_paths)
    paths = new_paths if use_new else _resolve_images(row.get("images") or [])
    use_vlm = use_new                # 新图才值得花一次视觉识别

    from fastapi.concurrency import run_in_threadpool
    try:
        res = await run_in_threadpool(
            do_revise,
            row["spec"], req.feedback, row.get("theory", ""),
            image_paths=paths,
            # ★ 关键字传参（实测教训：位置参数曾把 card/use_vlm 传反 ——
            #   False 落到 card 上，视觉卡在迭代链里丢失了好几轮）
            card=row.get("card") or None,
            use_vlm=use_vlm,
        )
    except Exception as e:
        logger.exception("模板迭代失败")
        # ★ 超时类错误给准确指引（实测：用户会误以为是自己的图片有问题）
        msg = str(e)
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            raise HTTPException(504, {
                "code": "forge_timeout",
                "message": "上游模型服务响应超时（临时拥堵，与你的图片和设置无关）"
                           "—— 已自动重试仍超时，请稍等半分钟再点一次「迭代一版」",
            }) from e
        raise HTTPException(500, {"code": "forge_failed",
                                  "message": f"{type(e).__name__}: {e}"}) from e

    if not res.get("ok"):
        # 迭代失败不丢上一版 —— 原样返回，让用户改个说法再试
        return {**res, "kept_version": row["version"], "kept_id": row["id"]}

    prompt = ""
    try:
        prompt = _render_prompt(res["spec"])
    except Exception as e:
        logger.warning("草稿渲染失败（草稿仍会保存）：%s", e)

    # 同一条迭代链，版本号 +1（★ 参考图随本轮更新：贴了理想图后，
    # 这条迭代链的 images 就延续为理想图，后续迭代继续参照它）
    version = context_store.next_forge_version(row["lineage"])
    new_id = context_store.save_forge(
        lineage=row["lineage"], version=version, spec=res["spec"],
        card=res.get("card") or row.get("card") or {},
        name=res["spec"].get("name") or row.get("name") or "",
        prompt=prompt, theory=row.get("theory", ""), feedback=req.feedback,
        images=[os.path.basename(p) for p in paths] if paths else [],
        owner_session=(row.get("owner_session") or "").strip() or None,
    )
    audit("forge_saved", forge_id=new_id, lineage=row["lineage"], version=version)
    return {
        **res,
        "id": new_id,
        "lineage": row["lineage"],
        "version": version,
        "prompt": prompt,
        "previous_id": row["id"],
    }


@router.get("/library", summary="我的模板库")
async def library(limit: int = 50, sid: str = Depends(session_dep)):
    rows = context_store.list_forge(limit=limit, owner=sid)
    items = []
    for r in rows:
        spec = r.get("spec") or {}
        items.append({
            "id": r["id"],
            "lineage": r["lineage"],
            "version": r["version"],
            # ★ 名字以 spec（家族名）为准 —— 库记录名是工坊随手填的，
            #   与主页面家族名不一致时用户根本对不上（实测 2026-10-03）
            "name": str(spec.get("name") or r.get("name") or "未命名"),
            "family_id": r.get("family_id"),
            "installed": bool(r.get("installed")),
            "created_at": r.get("created_at"),
            "feedback": r.get("feedback") or "",
            "prompt_chars": len(r.get("prompt") or ""),
        })
    return {"count": len(items), "items": items, **context_store.forge_stats()}


@router.put("/{forge_id}/prompt", summary="保存手动修改的提示词（空串=恢复自动渲染）")
async def save_prompt(forge_id: str, req: PromptOverrideRequest,
                      sid: str = Depends(session_dep)):
    """★ 手改提示词直接生效，不走 LLM（实测需求 2026-10-03）

    用户只想改某个片段时：改这里保存即可，秒级生效；
    生成时 prompt_builder 检测到 override 就直接用这份文本。
    只有点「迭代一版」（重新提炼）才会重新跑整个 LLM 流程。
    """
    row = context_store.get_forge(forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    _ownable(row, sid)
    text = (req.prompt or "").strip()
    if text and len(text) < 20:
        raise HTTPException(422, {"code": "prompt_too_short",
                                  "message": "提示词内容太短（至少 20 字）——"
                                             "如果想恢复自动渲染，请保存空白内容"})
    if not context_store.set_forge_prompt(forge_id, text):
        raise HTTPException(404, "找不到这份草稿")
    audit("forge_prompt_edited", forge_id=forge_id,
          chars=len(text), auto=(not text))
    return {"ok": True, "prompt_override": text,
            "prompt": text or row.get("prompt") or "",
            "message": ("已恢复自动渲染（参数调节恢复生效）" if not text
                        else "已保存手改提示词（生成时直接使用这份文本）")}


@router.get("/{forge_id}", summary="取一版草稿")
async def get_one(forge_id: str, sid: str = Depends(session_dep)):
    row = context_store.get_forge(forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    # 读操作：内置家族人人可看；他人的自建记录按不存在处理
    ow = (row.get("owner_session") or "").strip()
    if sid != DEFAULT_SESSION and ow and ow != sid:
        raise HTTPException(404, "找不到这份草稿")
    versions = context_store.list_forge(lineage=row["lineage"], owner=sid)
    # ★ ok 动态判定（实测 2026-10-01：库里没有 ok 字段，前端
    #   disabled={busy || !current.ok} 恒为真 → 「安装为家族」永远点不了）。
    #   可安装 = spec 非空 + 提示词已渲染 + 不是【提炼失败】标记版。
    spec = row.get("spec") or {}
    ok_flag = (
        bool(spec)
        and bool(row.get("prompt"))
        and not str(row.get("feedback") or "").startswith("【提炼失败】")
    )
    return {
        **row,
        "ok": ok_flag,
        "versions": [{"id": v["id"], "version": v["version"],
                      "installed": bool(v.get("installed"))} for v in versions],
    }


@router.post("/{forge_id}/install", summary="安装为可用家族")
async def install(forge_id: str, sid: str = Depends(session_dep)):
    row = context_store.get_forge(forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    _ownable(row, sid)   # 内置家族只读；他人记录按不存在处理

    # ★ 公开版防污染：每位访客最多安装 8 个自建家族（内置不计入）——
    #   安装会写进全局 families/（社区共享），限额是恶意刷装的最后一道闸。
    if sid != DEFAULT_SESSION:
        n = sum(1 for r in context_store.list_forge(limit=500, owner=sid)
                if r.get("installed") and (r.get("owner_session") or "").strip())
        if n >= 8:
            raise HTTPException(429, {"code": "install_quota",
                "message": "每位访客最多安装 8 个自建家族——"
                           "可以先删掉不用的再安装新的"})

    # ★ 安装前强制体检 + 修复（2026-10-01 教训：提炼产物常带结构缺陷，
    #   装得进去、用起来才炸）。修复只动结构与语法，不动模型的创意内容。
    try:
        from services.spec_repair import repair_spec
        spec, repair_notes = repair_spec(row["spec"], row.get("card") or {})
    except Exception as e:                 # 体检自身出错不应拦死安装
        logger.warning("安装前体检失败，按原样继续：%s", e)
        spec, repair_notes = row["spec"], ["体检未执行，按原样安装"]

    # ★ 出厂检验（10-03 新增，语义级）：画幅/正向短语存活/forbid↔creative 矛盾。
    #   结构体检拦不住的「装得上、出图才炸」缺陷在这里拦下；自动修的当场修。
    qc_fixed: list[str] = []
    try:
        from services.family_qc import qc_family
        spec, qc_blockers, qc_warnings, qc_fixed = qc_family(spec, card=row.get("card") or {})
        if qc_blockers:
            raise HTTPException(422, {
                "code": "forge_unusable",
                "message": f"出厂检验未通过，无法安装：{'；'.join(qc_blockers[:3])}"
                           "—— 建议回工坊迭代一版"})
    except HTTPException:
        raise
    except Exception as e:                 # QC 自身故障不拦死安装（结构与渲染冒烟仍兜底）
        logger.warning("出厂检验异常，跳过：%s", e)
        qc_warnings = []

    fid = str(spec.get("id") or "")
    if not fid:
        raise HTTPException(422, "草稿缺少 id，无法安装")

    # ★ 手改提示词随安装写进家族 spec —— 生成端 prompt_builder 短路直接用它。
    #   override 是用户亲手确认的文本，不走 QC / 渲染冒烟的否定性检查
    #   （spec 本身带病没关系：生成时根本不渲染 spec，override 就是成品）。
    override = (row.get("prompt_override") or "").strip()
    if override:
        spec["prompt_override"] = override

    # 装之前先跑一次**严格**渲染冒烟 —— 装一个用不了的家族比不装更糟。
    # strict=True 与运行时一致：有未解析占位符/缺 dict 直接拦住。
    # ★ override 非空时放行渲染失败：spec 坏了但家族靠 override 依然完全可用。
    try:
        prompt = _render_prompt(spec, strict=True)
    except Exception as e:
        if override:
            logger.warning("家族 %s 的 spec 渲染失败，但存在手改提示词，放行安装：%s", fid, e)
            prompt = override
        else:
            raise HTTPException(422, {"code": "forge_unusable",
                                      "message": f"这份草稿无法渲染，请先继续迭代：{e}"}) from e

    yaml_text = _spec_to_yaml(spec)
    written = []
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (_RUNTIME_DIR / f"{fid}.yaml").write_text(yaml_text, encoding="utf-8")
        written.append(str(_RUNTIME_DIR / f"{fid}.yaml"))
        # 同时写规格层，保证 sync_families.py --check 通过（否则一同步就被冲掉）
        if _SPEC_DIR.is_dir():
            (_SPEC_DIR / f"{fid}.yaml").write_text(yaml_text, encoding="utf-8")
            written.append(str(_SPEC_DIR / f"{fid}.yaml"))
    except OSError as e:
        raise HTTPException(500, f"写入家族文件失败：{e}") from e

    # ★ 关键：家族文档是进程内 lru_cache 缓存的，装完不清缓存，
    #   /api/families 会一直返回旧列表 —— 用户"装了却在风格模板里看不到"就出在这。
    try:
        from services.template_manager import clear_cache
        clear_cache()
    except Exception as e:                 # 清缓存失败不该让安装失败
        logger.warning("安装后清家族缓存失败：%s", e)

    # ★ 把修复后的 spec 回写库记录 —— 后续「迭代一版」基于干净版本继续改，
    #   否则下一版会把同一批结构缺陷重新带回来。
    #   prompt_override 必须带上：save_forge 是 INSERT OR REPLACE，
    #   不带就会把用户刚存的手改提示词抹掉（实测级边界）。
    try:
        context_store.save_forge(
            lineage=row["lineage"], version=int(row["version"] or 1),
            spec=spec, card=row.get("card") or {},
            name=row.get("name") or str(spec.get("name") or ""),
            prompt=prompt, theory=row.get("theory", ""),
            feedback=row.get("feedback", ""),
            images=row.get("images") or [],
            forge_id=forge_id,
            prompt_override=row.get("prompt_override") or "",
            owner_session=(row.get("owner_session") or "").strip() or None,
        )
    except Exception as e:
        logger.warning("修复版 spec 回写失败（不影响安装）：%s", e)

    context_store.mark_forge_installed(forge_id, fid)

    # ★ 名字统一（实测 2026-10-03：工坊随手填的风格名进了库，主页面却是模型
    #   起的家族名，用户对不上哪个是哪个）—— 家族名是唯一权威，同链全部版本跟随
    try:
        context_store.sync_forge_name(row["lineage"],
                                      str(spec.get("name") or fid))
    except Exception as e:
        logger.warning("同步库名称失败（不影响安装）：%s", e)

    audit("forge_installed", forge_id=forge_id, family_id=fid)
    logger.info("已安装家族 %s（%s）", fid, ", ".join(written))
    return {"ok": True, "family_id": fid, "written": written,
            "repair_notes": (repair_notes + qc_fixed)[:10],
            "prompt": prompt,
            "note": "已写入规格层与运行时层，sync_families.py --check 仍会通过。"}


@router.post("/{forge_id}/render", summary="渲染当前草稿的提示词（可复制）")
async def render_one(forge_id: str, params: dict | None = None,
                     sid: str = Depends(session_dep)):
    row = context_store.get_forge(forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    ow = (row.get("owner_session") or "").strip()
    if sid != DEFAULT_SESSION and ow and ow != sid:
        raise HTTPException(404, "找不到这份草稿")
    try:
        prompt = _render_prompt(row["spec"], params)
    except Exception as e:
        raise HTTPException(422, f"渲染失败：{e}") from e
    return {"prompt": prompt, "chars": len(prompt), "family_id": row.get("family_id")}


@router.delete("/{forge_id}", summary="删除一版草稿")
async def remove(forge_id: str, sid: str = Depends(session_dep)):
    row = context_store.get_forge(forge_id)
    if not row:
        raise HTTPException(404, "找不到这份草稿")
    _ownable(row, sid)
    ok = context_store.delete_forge(forge_id)
    if not ok:
        raise HTTPException(404, "找不到这份草稿")
    return {"ok": True}


def _spec_to_yaml(spec: dict) -> str:
    """把草稿写成 YAML。用 yaml.safe_dump 而不是手拼字符串 ——
    手拼会在文案含引号/冒号/换行时写出非法 YAML，
    而非法 YAML 会让整个家族目录加载失败（那正是我们已经修过一次的问题）。"""
    import yaml

    doc = {k: v for k, v in spec.items() if k not in ("_kind", "_path", "_file")}
    doc["kind"] = "family"
    header = (
        "# 家族：{name}\n"
        "# 由「模板工坊」从风格理论 + 参考图自动提炼\n"
        "# 安装后可用 /api/image/render 预览；有问题可以继续迭代或手工修改本文件\n"
    ).format(name=doc.get("name") or doc.get("id"))
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=100)
    return header + body
