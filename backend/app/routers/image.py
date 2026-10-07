"""图像路由 —— 预览（0 成本）与生成（计费）分道而行

════════ 重写要点 ════════

旧版这个文件的核心有三处致命问题（审查报告 P0-1 / P1-10 / P1-12）：

    UPLOAD_DIR = "./storage/images"           # ① 相对路径 → 双份存储
    template = get_template_by_id(...)
    prompt = template["prompt_template"]      # ② travel_sketch.yaml 早就没这字段了 → 100% KeyError
    f.write(await file.read())                # ③ 无大小限制、不校验格式 → 上传就是个洞

第三条已经在 `services/upload.py` 修好，这个文件负责修前两条：

- **提示词一律走 `services/prompt_builder`**，不再直接读模板字段。
  形态 A（prompt_template）与形态 B（family_id + params）都支持，
  新增任何一种都不会再让这里崩。
- **所有出图路径先过 `governance`**（配额 / 总开关）。
- **预览与生成彻底分开**：`/preview` 0 成本毫秒级，`/generate` 才计费。
  这是方案 §5.2「用提示词实时重渲染替代真·实时生图」的接口化体现。
"""
from __future__ import annotations

import asyncio
import json
import os
import stat as stat_module   # S_ISREG：一次 stat 同时判"是不是文件"与取大小/时间
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.image_agent import (                                      # noqa: E402
    AgentInputError,
    normalize_reference,
    resolve_thread_id,
)
from config import ConfigMissing, IMAGE_STORAGE_DIR                  # noqa: E402
from governance.guard import (                                       # noqa: E402
    GovernanceError,
    new_generation_guard,
    release_generation,
    remaining_quota,
    reserve_generation,
    settle_generation,
)
from infra.logging import audit, logger, step                           # noqa: E402
from infra.storage import (                                       # noqa: E402
    is_retired_seed,
    retired_seed_names,
    to_url,
)
from services.card_extractor import build_card, summarize_card       # noqa: E402
from services.identity import (                                      # noqa: E402
    quota_key,
    DEFAULT_SESSION,
    is_anonymous_fallback,
    merge_thread,
    session_dep,
)
from services.image_generator import generate_image_with_reference    # noqa: E402
from services.prompt_builder import (                                # noqa: E402
    MissingRequiredParams,
    RenderFailure,
    SourceNotFound,
    build_prompt,
)
from services.template_manager import get_family_by_id               # noqa: E402
from services.upload import validate_and_save                        # noqa: E402

router = APIRouter(prefix="/api/image", tags=["image"])


class RenderRequest(BaseModel):
    source_id: str
    params: dict = Field(default_factory=dict)
    card: dict = Field(default_factory=dict)
    # USER-LOCKED：用户在 UI 里显式设置过的参数名 —— 渲染时跳过默认值与 auto 兜底
    locked: list[str] = Field(default_factory=list)
    # 用户自定义提示词（见 routers/chat.ChatRequest 的说明）
    extra_prompt: str = ""
    extra_mode: str = "append"


def _public_source(saved: dict) -> dict:
    """给前端的「来源」视图 —— **剔除服务器绝对路径**

    ★ 为什么（审查发现 P2-16）
    `/api/chat/upload` 特意只回 url/key/filename/width... 说明「不回绝对路径」
    是既有约定；但 preview / generate 直接回 `saved` 整个 dict，
    把 `C:////Users////<用户名>////Desktop////...` 一起吐给了前端。
    绝对路径既没必要（前端只用 url），又是无谓的信息暴露。
    """
    return {k: v for k, v in saved.items() if k != "path"}


def _parse_params(raw: str | None) -> dict:
    """把 multipart 表单里传来的 JSON 字符串解析成 dict

    multipart/form-data 没法直接表达嵌套对象，所以前端传 JSON 字符串。
    解析失败要给 400，而不是让 json.loads 的异常变成 500。
    """
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"params 不是合法 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise HTTPException(400, "params 必须是一个 JSON 对象")
    return obj


def _parse_locked(locked: str) -> list:
    """locked 表单值 → 参数名列表（畸形输入一律当没锁，不 500）

    此前这段解析在 preview / generate 内联各写一份（审查 P2-10），
    改一处漏一处就是分叉的开始。
    """
    if not locked:
        return []
    try:
        val = json.loads(locked)
    except json.JSONDecodeError:
        return []
    return val if isinstance(val, list) else []


def _build_prompt_locked(source_id: str, params: dict, card: dict,
                         extra_prompt: str, extra_mode: str, locked: list):
    """带 USER-LOCKED 的渲染入口。

    直接走 build_prompt(locked=...)：那条链会统一路由模板/家族（不会因
    工坊家族装在别处而找不到）、抛 SourceNotFound / MissingRequiredParams /
    RenderFailure 给端点的 except 链、并完整应用 extra_prompt。
    之前手搓 render_family(_spec_by_id(...)) 的版本在这三点上全是坑
    （模板 id 直接 500 / extra_prompt 被静默丢弃 / 缺参不再上报）。
    """
    return build_prompt(source_id, params or {}, card or {},
                        extra_prompt, extra_mode, locked or [])


def _aspect_of(built: dict) -> str | None:
    fid = built.get("family_id")
    if not fid:
        return None
    fam = get_family_by_id(fid)
    return (fam or {}).get("default_aspect") if fam else None


@router.post("/preview", summary="上传图片 + 渲染提示词（不调用生图模型，0 成本）")
async def preview(
    file: UploadFile = File(...),
    source_id: str = Form(...),
    params: str = Form(""),
    extra_prompt: str = Form(""),
    extra_mode: str = Form("append"),
    locked: str = Form(""),          # JSON 数组字符串，如 '["substrate"]'
    sid: str = Depends(session_dep),
) -> dict:
    """滑杆一动就调它，把三段式实时显示给用户看 —— 毫秒级、不花钱"""
    # ★ 同步重 IO（Pillow 解码 + 落盘）必须丢进线程池：
    #   直接放在 async def 里会冻结整个事件循环 —— 一次出图能让健康检查都超时。
    saved = await run_in_threadpool(validate_and_save, file, "upload", sid)
    parsed = _parse_params(params)
    # ★ 本地档创作卡：只做 Pillow 取色，**不调任何模型** ——
    #   /preview 承诺「0 成本、毫秒级」，不能因为有了 card 提取就偷偷加成本。
    #   色板足以驱动 fidelity=heavy 类的保留约束与 palette_keep 派生。
    card = await run_in_threadpool(build_card, saved["path"], use_vlm=False)
    try:
        built = await run_in_threadpool(
            build_prompt, source_id, parsed, card, extra_prompt, extra_mode,
            _parse_locked(locked),
        )
    except SourceNotFound as e:
        raise HTTPException(404, str(e)) from e
    except MissingRequiredParams as e:
        raise HTTPException(422, {"code": "missing_params",
                                  "missing": e.missing, "message": str(e)}) from e
    except RenderFailure as e:
        raise HTTPException(500, f"渲染失败：{e}") from e

    return {
        "ok": True,
        "mode": "preview",
        "source": _public_source(saved),
        "family_id": built.get("family_id"),
        "source_kind": built.get("source_kind"),
        "params": built.get("params"),
        "segments": built.get("segments"),
        "prompt": built.get("prompt"),
        "prompt_chars": len(built.get("prompt") or ""),
        "warnings": built.get("warnings"),
        "extra_applied": built.get("extra_applied"),
        "auto_resolved": built.get("auto_resolved"),
        # ★ 漂移自检结果单独给一份（warnings 里也有一份，但那里混着渲染告警）。
        #   阶段一的目标就是「先收集真实数据」—— 分开存才好看零误报率。
        "preflight": built.get("preflight") or [],
    }


@router.post("/generate", summary="上传图片 + 生成（会真实计费）")
async def generate(
    request: Request,
    file: UploadFile = File(...),
    source_id: str = Form(...),
    params: str = Form(""),
    thread_id: str = Form("default"),
    extra_prompt: str = Form(""),
    extra_mode: str = Form("append"),
    locked: str = Form(""),
    sid: str = Depends(session_dep),
) -> dict:
    tid = merge_thread(thread_id, sid)   # 历史命名空间
    # ★ 配额账本另算：merge_thread 的回退分支用的是用户可控的 thread_id，
    #   拿它当配额键等于「换个 thread_id 就绕过额度」（见 identity.quota_key）
    qkey = quota_key(thread_id, sid, request)

    # ① 上传（内部已做体积 + 真实格式校验，不合格直接 400/415）
    saved = await run_in_threadpool(validate_and_save, file, "upload", sid)

    # ② 治理：预扣额度 —— 检查与占位一次完成，避免并发穿过检查窗口
    #    ★ 2026-10-06：归还改由 quota_guard 幂等负责（见 governance/guard.py
    #      generation_guard 的说明）。此前这里只捕ConfigMissing 来 release，
    #      而出图内部还会 raise ValueError —— 那类异常穿过 except，
    #      票据就悬着不动：用户没拿到图、额度却一直被扣到 TTL 到期。
    try:
        token = reserve_generation(qkey, reference_image=saved["path"])
    except GovernanceError as e:
        status = 429 if e.code == "quota_exhausted" else 403
        raise HTTPException(status, {"code": e.code, "message": str(e)}) from e
    quota_guard = new_generation_guard(qkey, token, reference_image=saved["path"])

    try:
        # ③ 提炼创作卡 —— 出图路径本来就在花钱，多一次小调用换「反推 forbid」生效。
        #    失败会自动退回本地档（见 card_extractor：VLM 挂了不该让出图挂掉）。
        card = await run_in_threadpool(build_card, saved["path"], use_vlm=True)

        # ④ 渲染提示词（走唯一入口）
        parsed = _parse_params(params)
        try:
            built = await run_in_threadpool(
                build_prompt, source_id, parsed, card, extra_prompt, extra_mode,
                _parse_locked(locked),
            )
        except (SourceNotFound, MissingRequiredParams, RenderFailure) as e:
            if isinstance(e, SourceNotFound):
                raise HTTPException(404, str(e)) from e
            if isinstance(e, MissingRequiredParams):
                raise HTTPException(422, {"code": "missing_params",
                                          "missing": e.missing,
                                          "message": str(e)}) from e
            raise HTTPException(500, f"渲染失败：{e}") from e

        # ⑤ 出图（尺寸一律交给 resolve_size 决定，遵守「输出尺寸跟随原图」）
        #    缺 key 是「依赖未就绪」而不是「程序出错」→ 503 而不是 500
        try:
            result = await run_in_threadpool(
                generate_image_with_reference,
                reference_image_path=saved["path"],
                prompt=built["prompt"],
                size=None,
                aspect=_aspect_of(built),
            )
        except ConfigMissing as e:
            # 「依赖未就绪」不是「程序出错」—— 配好 key 就能用，所以是 503
            raise HTTPException(503, {"code": e.code, "message": str(e)}) from e

        if not result.get("success"):
            raise HTTPException(502, {"code": "generation_failed",
                                      "message": result.get("error")})

        # ⑥ 只有真的拿到图才核销预扣（语义与原settle_generation 一致）
        quota = quota_guard.commit(size=result.get("size", ""))
    finally:
        # 已commit 过就是 no-op；任何异常/提前返回都在这里归还
        quota_guard.release(reason="出图未完成")
    audit("http_generated", thread_id=tid, family=built.get("family_id"))

    return {
        "ok": True,
        "mode": "generate",
        "source": _public_source(saved),
        "family_id": built.get("family_id"),
        "image_url": result.get("url"),
        "size": result.get("size"),
        "aspect_warning": result.get("aspect_warning") or "",
        "prompt_chars": len(built.get("prompt") or ""),
        "card": {"origin": card.get("_origin"), "summary": summarize_card(card)},
        "quota": quota,
        # ★ 出图路径是采集零误报率最关键的一条链（审查 P1-2 实锤：
        #   此前只补了 /preview 与 /render，真正的 /generate 反而漏了）
        "preflight": built.get("preflight") or [],
    }


@router.post("/render", summary="已有原图 + 参数 → 渲染提示词（0 成本）")
async def render(req: RenderRequest) -> dict:
    """给前端参数面板用：拖动滑杆 → 立刻看到提示词怎么变"""
    try:
        built = await run_in_threadpool(
            _build_prompt_locked, req.source_id, req.params, req.card,
            req.extra_prompt, req.extra_mode, req.locked,
        )
    except SourceNotFound as e:
        raise HTTPException(404, str(e)) from e
    except MissingRequiredParams as e:
        raise HTTPException(422, {"code": "missing_params",
                                  "missing": e.missing, "message": str(e)}) from e
    except RenderFailure as e:
        raise HTTPException(500, f"渲染失败：{e}") from e
    return {
        "ok": True,
        "family_id": built.get("family_id"),
        "params": built.get("params"),
        "segments": built.get("segments"),
        "prompt": built.get("prompt"),
        "allow_change": built.get("allow_change"),
        "warnings": built.get("warnings"),
        # 自定义提示词的落地说明 —— 前端用它把「已生效」如实告诉用户，
        # 而不是让用户自己猜那段文字到底有没有进去
        "extra_applied": built.get("extra_applied"),
        # ★ 同上：漂移自检单独给一份，便于采集零误报率
        "preflight": built.get("preflight") or [],
    }


# ═══════════════ 局部修复 / AI 找问题（外科修复直通道）═══════════════

@router.post("/repair", summary="局部修复刚生成的图（外科修复：只改指定处，其余保持）")
async def repair(
    request: Request,          # ★ 用于推导配额账本键（见 identity.quota_key）
    change: str = Form(..., description="要修的具体问题（每行一条，最多 3 条）"),
    reference: str = Form(..., description="刚生成图的 url（gallery/生成结果返回的 url 形态）"),
    thread_id: str = Form("default"),
    family_id: str = Form("", description="生成该图用的家族 —— 用于重申硬禁令，防修复破坏约束"),
    extra_prompt: str = Form("", description="用户当初的自定义提示词 —— 修复同样要遵守"),
    sid: str = Depends(session_dep),
):
    """主画布「局部修复」按钮的直通端点 —— 确定性操作，不让模型转手。

    与 chat 流的 repair_image 工具共用 `_build_repair_prompt` 与治理三段式；
    区别只在参考图由前端显式传入（刚生成的 result.url），不依赖 agent 会话记忆。

    ★ 配额线程必须走 merge_thread（2026-10-05 补齐）：
      这里曾经直接用表单里的 thread_id 做额度键，于是公开版里所有访客共用
      同一个 "studio" 桶 —— 别人修几张就把你的额度耗光是其一，
      更糟的是任何人都能改表单里的 thread_id 去动别人的额度。
      现在与 /generate 同口径：header 会话优先（sid），只有当它是 default
      （本地单机）时才回退表单里的 thread_id。
    """
    change = (change or "").strip()
    if not change:
        raise HTTPException(422, {"code": "missing_change",
                                  "message": "必须说明要修的某一处具体问题"})

    try:
        ref_path = normalize_reference(reference)
    except AgentInputError as e:
        raise HTTPException(400, str(e)) from e
    if not os.path.exists(ref_path):
        raise HTTPException(404, "参考图不存在")
    from services.image_access import may_read                  # 与 /thumb 同一把锁
    if not may_read(ref_path, sid):
        raise HTTPException(404, "图片不存在")

    tid = merge_thread(thread_id, sid)   # 与 /generate 同口径
    qkey = quota_key(thread_id, sid, request)
    try:
        token = reserve_generation(qkey, reference_image=ref_path)
    except GovernanceError as e:
        status = 429 if e.code == "quota_exhausted" else 403
        raise HTTPException(status, {"code": e.code, "message": str(e)}) from e

    from tools.registry import _build_repair_prompt, build_repair_constraints  # 与 agent 工具同源
    prompt = _build_repair_prompt(
        change, build_repair_constraints(extra_prompt, family_id))

    # ★ 与 /generate 同一套归还保证：release 幂等，放在 finally 里
    quota_guard = new_generation_guard(qkey, token, reference_image=ref_path)
    try:
        try:
            result = await run_in_threadpool(
                generate_image_with_reference,
                reference_image_path=ref_path,
                prompt=prompt,
                size=None,                    # 尺寸跟随参考图（上一版成品）
            )
        except ConfigMissing as e:
            raise HTTPException(503, {"code": e.code, "message": str(e)}) from e

        if not result.get("success"):
            raise HTTPException(502, {"code": "generation_failed",
                                      "message": result.get("error")})

        quota = quota_guard.commit(size=result.get("size", ""))
    finally:
        quota_guard.release(reason="修复未完成")
    audit("http_repaired", thread_id=tid, change=change[:60])
    return {
        "ok": True,
        "mode": "repair",
        "change": change,
        "image_url": result.get("url"),
        "size": result.get("size"),
        "quota": quota,
        "note": "只改了这一处，其余画面保持原样；还不满意可以继续修复。",
    }


@router.post("/diagnose", summary="AI 找问题：对比原图与成品，给出漂移候选（最多 3 条）")
async def diagnose(
    original: str = Form(..., description="用户原图的 url"),
    generated: str = Form(..., description="生成成品的 url"),
    family_id: str = Form("", description="生成该图用的家族 —— 让 VLM 知道模板会刻意加什么"),
    request: Request = None,
    sid: str = Depends(session_dep),
):
    """repair v1 的漂移判定：VLM 自动对比，替代用户口述。

    造梦师 Decode Repair 的判据被翻译成两分类：
      adaptation = 为服从风格模板而发生的合理改变（不算问题）
      drift      = 模板没要求、也不该发生的意外改变（值得修）
    诊断本身不生图，只花一次 VLM 调用；失败返回 ok=false，前端可继续手填。

    ★ 三道护栏（2026-10-05 补齐，都是「这一终点会真花钱」的直接后果）：
      ① 限速 —— 每次真实判定都是一次付费视觉调用，必须挡住连点与脚本刷。
         记账点放在参数校验**之后**：填错参数的请求本来就到不了付费那一步，
         不该替攻击者白白消耗正常用户的窗口。
      ② 图片预处理失败不能变 500 —— 用户正等着填修复框，宁可让他手填。
      ③ 任何降级都给一句能照做的话，而不是把模型原始报错吐回去。
    """

    from services.image_access import may_read

    paths: list[str] = []
    for u in (original, generated):
        try:
            path = normalize_reference(u)
        except AgentInputError as e:
            raise HTTPException(400, str(e)) from e
        if not os.path.exists(path) or not may_read(path, sid):
            raise HTTPException(404, "图片不存在")
        paths.append(path)

    # ① 限速：同一来源 60 秒内最多 6 次真实判定
    if request is not None:
        from services.identity import client_key, rate_ok
        if not rate_ok(client_key(request, "diagnose"), limit=6, window_sec=60):
            raise HTTPException(429, {"code": "diagnose_rate_limited",
                                      "message": "AI 找问题点得太快了，稍等一分钟再试"})

    # ★ 真正的判定在 services/drift.py（纯逻辑、可单测）——端点只负责 HTTP 语义。
    #   家族上下文必须带上（2026-10-05 用户实测反馈）：不知道模板是谁，
    #   VLM 会把「模板刻意添加的元素」（如涂鸦小人、手写文案）判成 drift——
    #   用户一修反而把风格修没了。内置模板再额外套一层口径：连
    #   「模板没要求」这种模板内部理由都不许用来指控自己的成品。
    from routers.templates import BUILTIN_FAMILY_IDS
    from services.drift import diagnose_drifts
    fid = (family_id or "").strip()
    family_hint = ""
    fam = get_family_by_id(fid)
    if fam:
        meta = [str(fam.get(k) or "").strip() for k in ("name", "description")]
        family_hint = "｜".join(x for x in meta if x)
    result = await run_in_threadpool(diagnose_drifts, paths[0], paths[1],
                                     family_hint, fid in BUILTIN_FAMILY_IDS)

    audit("image_diagnosed", thread_id=sid, ok=bool(result.get("ok")),
          drifts=len(result.get("drifts") or []),
          elapsed=result.get("elapsed_sec"))
    return result


def _gallery_sync(limit: int, sid: str = DEFAULT_SESSION,
                  kinds: set | None = None) -> dict:
    """递归扫描 —— 修掉「日期子目录里的图看不到」

    旧版只认平铺目录，而 `_save_generated` 是按日期分子目录存的，
    结果画廊永远是空的。这里用 rglob 递归，并按修改时间倒序。

    ★ 多用户（2026-10-03 公开版）：
      - default（本地版）→ 扫全目录，行为与旧版一致；
      - 匿名/登录会话   → 只扫 images/<sid>/（自己的）+ images/_seed/（公共展示），
        其余访客的目录完全不进列表。

    ★ kinds 下推过滤（2026-10-03 实测教训）：图库堆过 500 个文件后，
      「先取 500 再过滤」会让 9 月的种子图永远进不了列表 —— 过滤必须
      发生在**截断之前**。kind 判定不涉及 stat，放循环最前面还省 IO。

    ★ 公开版的 default 不等于全目录（2026-10-03 线上实测）：
      旧写法只有「default → 扫全目录 / 否则 → 自己的目录」两分支，
      于是公开版里任何没带头（爬虫、裸 curl、localStorage 被清）的请求
      都会拿到**全体访客**的图片列表。现在多一条分支：公开版的身份未知
      请求只给公共展示图。判定统一问 identity.is_anonymous_fallback。

    ★★ 目录 mtime 增量（2026-10-07 压测实测：831 张图冷扫0.87s）
      --------------------------------------------------------
      目录的 mtime 只在**该目录下有条目被增/删/改名**时才变，
      而文件内部的修改（改字节、touch）不会让它变。所以：
      mtime 没变 == 这个目录的文件清单没变，可以直接复用上次的
      stat 结果，不必对每个文件再 stat 一次。

      **取舍：mtime 粒度不够细时宁可多扫一次**
      mtime 的精度取决于文件系统：NTFS/ext4 是纳秒级（本机实测
      连续三次增删能分辨出三个不同的 mtime_ns），但 ext3 / 部分
      网络盘只有**秒级**，同一秒内的增删在索引看来就是"没变"。
      那会让刚落盘的新图在最长一个 TTL 周期内不出现 —— 正是
      "漏掉新图"这种绝对不能接受的错。

      所以判定分两层，任一不满足就回落到逐文件 stat：
        ① 目录 mtime 变了 → 重扫；
        ② **本次 os.walk 拿到的文件名字典序与上次不同** → 重扫。
      ② 的成本是O(目录内文件数 × 字符串比较)，不碰磁盘，
      而它把 mtime 粒度这个洞**彻底堵上了**：只要文件清单变了
      （同秒内增删也算），就一定重扫。mtime 从"唯一依据"降级成
      "快速路径的门票"，正确性不再依赖文件系统的精度。
    """
    if is_anonymous_fallback(sid):
        roots = [IMAGE_STORAGE_DIR / "_seed"]
    elif sid == DEFAULT_SESSION:
        roots = [IMAGE_STORAGE_DIR]
    else:
        roots = [IMAGE_STORAGE_DIR / sid, IMAGE_STORAGE_DIR / "_seed"]

    # ★ 退役名单**每次扫描读一次**，而不是每个文件读一次（见下方注释）
    retired = retired_seed_names()

    items: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # ★ 遍历期剪枝：派生缓存目录（.thumbs 等）整棵跳过，
            #   不再"进去了再逐个文件判断后丢弃"。
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            # ★ 先按后缀筛（零 IO），只有留下来的才需要 stat。
            #   ★ 必须 sort()：os.walk 给的文件名顺序不保证稳定，
            #     而下面拿它跟索引里的旧名单做等值比较 ——
            #     顺序不同就会误判成"目录变了"，退化成每次全量重扫。
            candidates = sorted(fn for fn in filenames
                                if Path(fn).suffix.lower() in _GALLERY_SUFFIXES)
            key = os.path.normcase(os.path.abspath(dirpath))
            hit = _gallery_index.get(key)
            if hit is not None:
                dir_mtime, cached_names, cached = hit
                # 见 docstring：mtime 之外再比文件名，把秒级 mtime 的洞堵上
                if dir_mtime == _dir_mtime_ns(dirpath) and cached_names == candidates:
                    items.extend(cached)
                    continue
            entries = []
            for fn in candidates:
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()      # 一次 stat 同时解决"是不是文件"与"大小/时间"
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                # ★ 退役种子图（2026-10-05 用户要求把首屏重复图真正下掉）：
                #   平台是"上传覆盖"语义，包里删文件线上不会消失，所以退役图
                #   仍躺在磁盘上；靠这份名单在**列表层**跳过，首屏与仓库就都是 17 张。
                #   ★ 名单是本次扫描开头**一次性**读好的（见上方 retired）：
                #     旧写法每个文件都重读一次 retired.txt，831 个文件就是
                #     831 次读盘 —— 实测占整个冷扫描的 10%。语义不变：
                #     同一份名单、同样的比较，只是读一次而不是 N 次。
                if fn in retired:
                    continue
                try:
                    rel_root = fp.relative_to(IMAGE_STORAGE_DIR)
                except ValueError:
                    continue
                kind = (
                    "seed" if ("_seed" in rel_root.parts or "seed" in rel_root.parts)
                    else "generated" if fn.startswith("gen_")
                    else "upload"
                )
                # ★★ kinds 过滤**故意不放这里**，而是在索引命中之后、
                #   组装 items 时统一做。理由：索引是跨会话共享的，
                #   若把"已按某会话的 kinds 筛过的结果"存进去，
                #   A 会话筛 upload 就会污染 B 会话看到的结果。
                #   索引只存**与 kinds 无关**的产物（kind 已是条目字段），
                #   各会话在 items 上按 kind 自行过滤 ——
                #   依然满足"过滤发生在截断之前"（截断在最后一行）。
                entries.append({
                    "url": to_url(fp),
                    "filename": fn,
                    "kind": kind,
                    "bytes": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                        timespec="seconds"),
                    "subdir": rel_root.parent.as_posix(),
                })
            items.extend(entries)
            _gallery_index_put(key, dirpath, candidates, entries)
    if kinds:
        # ★ 截断前过滤（见上面的注释）：先按 kind 筛，再排序、再截断
        items = [x for x in items if x["kind"] in kinds]
    items.sort(key=lambda x: x["modified"], reverse=True)
    cap = max(1, min(limit, 500))
    return {"count": len(items[:cap]), "items": items[:cap]}


# ★ 后缀白名单 —— 与旧版逐字一致，抽成常量只为让"筛后缀"与
#   "索引里存什么"两处不会各写一份、改一处漏一处。
_GALLERY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

# ★ 目录级扫描索引：{规范化目录路径: (目录 mtime_ns, 文件名元组, 条目列表)}
#   条目里已经是**最终 dict**（含 url/kind/bytes/modified/subdir），
#   所以命中时是直接 extend 复用，一次 stat、一个字符串比较都不用做。
_gallery_index: dict[str, tuple[int, tuple, list]] = {}
# ★ 索引容量上限。目录数量随会话数增长（每会话一个目录 + 日期子目录），
#   不设上限就是内存泄漏 —— 公开版每个匿名访客都可能在 storage 下留目录。
#   超限时**整体丢弃重建**，不做 LRU：判断"哪几个目录最常用"要额外维护
#   命中顺序，而扫描本来就会被 15秒 TTL 缓存兜住，
#   偶尔全量重建一次的代价远小于 LRU 的复杂度与出错风险。
_GALLERY_INDEX_MAX_DIRS = 2000


def _dir_mtime_ns(dirpath: str) -> int:
    """取目录 mtime（纳秒）；读不到就返回 -1，逼迫调用方走重扫分支

    读失败时**绝不能**返回"看起来没变"的值：那会让索引把一个
    已经消失的目录当成有效，宁可多扫一次。
    """
    try:
        return os.stat(dirpath).st_mtime_ns
    except OSError:
        return -1


def _gallery_index_put(key: str, dirpath: str, names: tuple, entries: list) -> None:
    """写入索引，并在超限时整体丢弃重建"""
    if len(_gallery_index) >= _GALLERY_INDEX_MAX_DIRS:
        _gallery_index.clear()
    _gallery_index[key] = (_dir_mtime_ns(dirpath), names, entries)


_GALLERY_TTL = 15.0          # 列表缓存秒数：仓库打开频繁、目录变化低频，短 TTL 足够
# ★ 缓存按会话分桶（dict[sid] → {ts,data}）：不同访客的列表互不可见，
#   共用一个缓存会把 A 的图泄露给 B —— 这是隐私问题不是性能问题。
#   桶数上限：防匿名访客无限增长（见 gallery() 内的淘汰逻辑）
_GALLERY_MAX_BUCKETS = 128
_gallery_cache: dict[str, dict] = {}
# ★ 扫描单飞锁（防缓存击穿，见 gallery() 里的说明）。
#   ★★ 这里**必须用 asyncio.Lock，不能用 threading.Lock** ——
#      第一版写成了 threading.Lock，结果更糟：p50 从 25s 变成 **60s 全超时**。
#      原因很直白：`threading.Lock.acquire()` 是**阻塞**调用，写在协程里
#      会把整个事件循环卡住，别人连"等锁"都做不到 —— 锁没解决击穿，
#      反而把整个服务冻住了（这正是「在 async 里用同步锁」这个经典错误）。
#      asyncio.Lock 的等待是**挂起协程**而不是卡线程，持有它跨 await 是安全的。
_gallery_scan_lock = asyncio.Lock()


@router.get("/gallery", summary="列出最近的图片（按会话隔离 + 公共展示图）")
async def gallery(limit: int = 24, kinds: str = "", force: bool = False,
                  sid: str = Depends(session_dep)) -> dict:
    """画廊 —— 目录递归是同步 IO，丢线程池跑，别冻住事件循环

    kinds：逗号分隔的类型过滤（generated / seed / upload）。
    ★ 为什么必须能过滤：storage 里可能堆着上百张测试上传图（实测 180 张），
      按时间倒序取 200 条的话，种子图和真正的生成图会被挤出上限
      （实测 22 张种子只露出 16 张）。前端按需取，别在全量里捞。

    ★ TTL 缓存 + force：15 秒内的重复打开直接回缓存（rglob 全目录 + 逐个 stat
      是仓库打开慢的第一半）。前端「生成完成后对账」必须带 force=true，
      否则刚落盘的新图会被缓存挡住，对账就白做了。
    """
    import threading as _threading
    import time as _time

    now = _time.monotonic()
    bucket = _gallery_cache.setdefault(sid, {"ts": 0.0, "data": None})
    # ★ 桶数上限（审查 P2-3）：此前按会话分桶但永不淘汰 —— 公开版每个匿名
    #   访客永久占一格内存（桶里还挂着最多 500 条 items）。到这里说明来了新访客，
    #   丢掉最旧的桶即可：丢了只是下次重新扫目录，正确性不受任何影响。
    if len(_gallery_cache) > _GALLERY_MAX_BUCKETS:
        oldest = min(_gallery_cache, key=lambda k: _gallery_cache[k]["ts"])
        if oldest != sid:                      # 极端并发下别把自己刚建的桶丢了
            _gallery_cache.pop(oldest, None)
    # ★ 单飞（single-flight）—— 2026-10-06 压测发现的缓存击穿
    #   现象：缓存命中率明明很高，并发 40 时 /api/image/gallery 的 p50 却是
    #   **25 秒**、吞吐只有 1.5 req/s（同机器上 /api/families 有 230 req/s）。
    #   原因：40 个请求在同一瞬间**全部 miss**，于是同时去 rglob 全目录 + 逐个
    #   stat，把磁盘 IO 和 GIL 一起打满。缓存本身没问题，问题是"没命中之后没人排队"。
    #   做法：扫描这一步用进程内锁串行化，后来者直接拿**别人刚扫完的结果**——
    #   等于把 N 次全量扫描压成 1 次。
    #   （force=true 不参与复用：它是"刚生成完要立刻看到新图"的旁路。）
    if force or bucket["data"] is None \
            or (_time.monotonic() - bucket["ts"]) >= _GALLERY_TTL:
        async with _gallery_scan_lock:
            need_scan = force or bucket["data"] is None or (
                (_time.monotonic() - bucket["ts"]) >= _GALLERY_TTL)
            if need_scan:
                allow = {k.strip() for k in kinds.split(",") if k.strip()} \
                    if kinds else None
                bucket["data"] = await run_in_threadpool(
                    _gallery_sync, 500, sid, allow)
                bucket["ts"] = _time.monotonic()
    items = bucket["data"]
    # 后台预热缩略图（fire-and-forget）：用户点开仓库前，缩略图已在磁盘
    _threading.Thread(target=_prewarm_thumbs, args=(items["items"],),
                      name="thumb-prewarm", daemon=True).start()
    allow = {k.strip() for k in kinds.split(",") if k.strip()} if kinds else None
    if allow:
        items = {**items, "items": [x for x in items["items"] if x["kind"] in allow],
                 "count": sum(1 for x in items["items"] if x["kind"] in allow)}
    # ★ cap 用 500 而不是 200：内部已按 500 取全量、再按 kinds 过滤，
    #   这里再砍 200 会把过滤结果里"较旧"的一半丢掉
    #   （实测：无过滤时 258 个文件被砍到 200，22 张种子全部被挤出）。
    cap = max(1, min(limit, 500))
    items = {**items, "items": items["items"][:cap], "count": len(items["items"][:cap])}
    return items


class GalleryDownloadRequest(BaseModel):
    """批量下载请求 —— urls 来自 /api/image/gallery 的返回，不在白名单内的直接拒绝"""
    urls: list[str] = Field(default_factory=list, max_length=200)


def _gallery_download_sync(urls: list[str], sid: str) -> tuple[bytes, list[str]]:
    """把一批图片打包成 zip。返回 (zip 字节, 打包进来的文件名列表)

    ★ 安全是这条路径的重点：urls 是客户端传来的，必须逐个走
      normalize_reference（内部做存储目录白名单校验），越界的一律跳过 ——
      否则这就是一个「把服务器任意文件打包给用户」的洞。

    ★ 再走一遍 may_read：白名单只挡「目录外的系统文件」，挡不住
      「同存储目录里别人的会话」。少了这一层，勾选别人的图就能整包下载走。
    """
    import io
    import zipfile as _zip

    from services.image_access import may_read

    buf = io.BytesIO()
    packed: list[str] = []
    seen_names: set[str] = set()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zf:
        for u in urls:
            try:
                path = normalize_reference(u)
            except AgentInputError:
                continue                     # 越界/非法 → 静默跳过，不让一个坏参数毁掉整包
            if not os.path.exists(path):
                continue
            if not may_read(path, sid):
                continue                     # 别人的图：同样静默跳过，不喂线索
            # zip 内不能有重名条目 —— 不同日期子目录可能有同名文件
            name = Path(path).name
            stem, dot, ext = name.partition(".")
            k = 2
            while name in seen_names:
                name = f"{stem}_{k}{dot}{ext}" if dot else f"{stem}_{k}"
                k += 1
            seen_names.add(name)
            zf.write(path, arcname=name)
            packed.append(name)
    return buf.getvalue(), packed


@router.post("/gallery/download", summary="把选中的图片打包成 zip 下载")
async def gallery_download(req: GalleryDownloadRequest,
                           sid: str = Depends(session_dep)):
    if not req.urls:
        raise HTTPException(422, {"code": "nothing_selected",
                                  "message": "没有选中任何图片。"})
    data, packed = await run_in_threadpool(_gallery_download_sync, req.urls, sid)
    if not packed:
        raise HTTPException(422, {"code": "nothing_valid",
                                  "message": "选中的图片都已失效或不在允许目录内。"})
    from datetime import datetime as _dt
    from urllib.parse import quote as _quote
    filename = f"作品-{_dt.now().strftime('%Y-%m-%d')}.zip"
    audit("gallery_downloaded", count=len(packed))
    # ★ 中文文件名不能直接进 HTTP 头（headers 只允许 latin-1）。
    #   按 RFC 5987 双写：ASCII 兜底 + filename* 带 UTF-8 百分号编码，
    #   现代浏览器取 filename* 显示「作品-….zip」，老客户端退回 ASCII 名。
    ascii_name = f"works-{_dt.now().strftime('%Y-%m-%d')}.zip"
    encoded = _quote(filename)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}",
        },
    )


def _thumb_path_for(path: str, w: int):
    """缩略图磁盘缓存路径：内容键 = 绝对路径 + 宽度 + 源文件 mtime"""
    import hashlib
    key = hashlib.md5(f"{path}|{w}|{int(os.path.getmtime(path))}".encode()).hexdigest()[:20]
    return IMAGE_STORAGE_DIR / ".thumbs" / f"{key}.jpg"


def _thumb_make_sync(path: str, w: int) -> str:
    """生成（或命中）缩略图，返回落盘路径。

    写入用临时文件 + os.replace 原子替换：gallery 预热线程与用户请求线程
    可能同时为同一张图生成，直接写目标路径会让对方读走半个 JPEG。
    """
    from PIL import Image
    tdir = IMAGE_STORAGE_DIR / ".thumbs"
    tdir.mkdir(parents=True, exist_ok=True)
    tpath = _thumb_path_for(path, w)
    if tpath.exists():
        return str(tpath)
    # ★ tmp 名带线程 id（审查 P2-4）：预热线程与请求线程可能并发生成同一张图，
    #   固定 .tmp 名会让两者同时打开同一个文件，Windows 上 os.replace 直接
    #   PermissionError（虽被 catch 回退原图，但缩略图白做）。
    tmp = tpath.with_suffix(f".{threading.get_ident()}.tmp")
    with Image.open(path) as im:
        im = im.convert("RGB")
        ratio = w / im.width
        im = im.resize((w, max(1, round(im.height * ratio))), Image.LANCZOS)
        im.save(tmp, "JPEG", quality=82)
    os.replace(tmp, tpath)
    return str(tpath)


def _prewarm_thumbs(items: list, w: int = 360, limit: int = 60) -> None:
    """后台预热缩略图：已缓存的直接跳过，未缓存的逐张生成落盘。

    作品仓库「慢」的体感大头不是列表扫描，而是首次打开时几十张 2-4MB 原图
    现场缩放。预热把这一步提前到用户点开仓库之前（gallery 返回即触发）。
    """
    done = 0
    for it in items:
        if done >= limit:
            break
        try:
            path = normalize_reference(it.get("url", ""))
            if not os.path.exists(path):
                continue
            if _thumb_path_for(path, w).exists():
                continue
            _thumb_make_sync(path, w)
            done += 1
        except Exception:
            continue


@router.get("/thumb", summary="图片缩略图（带磁盘缓存 + 会话归属校验）")
async def thumb(u: str, w: int = 320, sid: str = Depends(session_dep)):
    """按宽度缩放的 JPEG 缩略图，磁盘缓存于 storage/images/.thumbs/

    ★ 为什么必须有：种子图/生成图单张 1–1.5MB，作品流一次渲染 88 个格子，
      eager 加载原图 = 一次性 30MB+，格子长时间空白（实测用户截图）。
      84px 高的带子用 320px 宽的缩略图（~40KB）视觉无差，加载快一个数量级。

    ★ 关心的尺寸/构图不算隐私，但图本身算：这里与 /images/* 走同一个
      may_read —— 只修 /images 而漏了 /thumb，等于换了扇门又没锁。
    """
    from fastapi.responses import FileResponse
    from services.image_access import may_read

    try:
        path = normalize_reference(u)
    except AgentInputError as e:
        raise HTTPException(400, str(e)) from e
    if not os.path.exists(path):
        raise HTTPException(404, "图片不存在")
    if not may_read(path, sid):
        raise HTTPException(404, "图片不存在")
    # ★ 退役名单（见 infra/storage.retired_seed_names）：文件因平台覆盖语义删不掉，
    #   但它已经不该再被取用 —— 缩略图与原图同口径，否则等于留了个后门。
    if is_retired_seed(path):
        raise HTTPException(404, "图片不存在")
    w = max(64, min(int(w), 1600))

    try:
        tpath = await run_in_threadpool(_thumb_make_sync, path, w)
    except Exception as e:
        logger.warning("缩略图生成失败，回退原图：%s", e)
        return FileResponse(path)
    return FileResponse(tpath, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def _size_hint_sync(image_url: str) -> dict:
    """告诉前端「这张图会按哪个档位出」—— 避免用户疑惑为什么选不了自定义尺寸"""
    try:
        path = normalize_reference(image_url)
    except AgentInputError as e:
        raise HTTPException(400, str(e)) from e
    if not os.path.exists(path):
        raise HTTPException(404, "参考图不存在")
    from services.image_generator import pick_size_for_image, resolve_size
    from services.upload import load_upload_as_card_hint
    return {
        "image_url": image_url,
        "recommended": pick_size_for_image(path),
        "resolved": resolve_size(path, None),
        **load_upload_as_card_hint(path),
    }


@router.get("/size-hint", summary="按原图自动推荐的输出档位")
async def size_hint(image_url: str, sid: str = Depends(session_dep)) -> dict:
    # 尺寸/构图本身不算秘密，但「这张图存不存在、多大」是 —— 与其他取图
    # 路径同口径，不给陌生人用 URL 探测别人素材的机会。
    from services.image_access import may_read

    try:
        path = normalize_reference(image_url)
    except AgentInputError as e:
        raise HTTPException(400, str(e)) from e
    if not may_read(path, sid):
        raise HTTPException(404, "参考图不存在")
    return await run_in_threadpool(_size_hint_sync, image_url)
