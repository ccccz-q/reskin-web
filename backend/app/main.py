"""FastAPI 主入口

════════ 重写要点（对照审查报告 P0-5 / P1-10 / CORS 反模式）════════

1. **消灭相对路径**。旧版 `os.makedirs("./storage/images")` +
   `StaticFiles(directory="./storage/images")`，基准是进程 cwd。
   已经造成的后果：项目里同时存在 `storage/` 和 `backend/storage/` 两份存储，
   前端上了一张图，画廊却是空的 —— 因为服务读的是另一份。
   现在一律引用 `config` 里由 `__file__` 推导出的绝对路径。

2. **CORS 不再 `allow_origins=["*"] + allow_credentials=True`**。
   这个组合会被浏览器直接拒绝，也是明确的反模式。
   改成白名单（默认只允许 vite dev server），credentials 恒 False。

3. **健康检查不再只回一句"跑起来了"**。
   `/api/health` 返回：配置是否就绪、模板清单、上下文库状态、治理策略。
   现场演示 / 排障时，这一个接口就能回答 80% 的"为什么不能用"。

4. **启动自检**：家族 YAML 有没有加载、工具契约和实现有没有对齐、
   配置文件缺没缺 —— 在启动时喊出来，而不是等点到某一步才 500。
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError         # noqa: E402
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (                                             # noqa: E402
    BACKEND_HOST,
    BACKEND_PORT,
    CORS_ALLOW_CREDENTIALS,
    CORS_ORIGINS,
    MAX_GENERATIONS_PER_SESSION,
    FRONTEND_DIST,
    IMAGE_STORAGE_DIR,
    PUBLIC_MODE,
    public_dict,
    SERVE_API_DOCS,

    IMAGE_BACKEND,
    IMAGE_API_KEY,
    IMAGE_MODEL,
)
from governance.guard import policy_snapshot, reconcile_reservations  # noqa: E402
from infra.logging import logger, recent_audit                   # noqa: E402
from infra.security import LocalAccessMiddleware, security_snapshot  # noqa: E402
from infra.worker_guard import check_multiworker                   # noqa: E402
# ── 管理后台（routers/admin.py 等三个文件）只在私库存在 ──────────
#   开源版给比赛评审看，不含任何管理入口，那三个文件整块不派生。
#   下面这行 import 同步时会被 tools/sync_oss.py 剥掉：
#   带过去就会 ModuleNotFoundError，连累后面所有测试（2026-10-07 事故）。
from routers.auth import router as auth_router                     # noqa: E402
from routers.chat import router as chat_router                   # noqa: E402
from routers.forge import router as forge_router                 # noqa: E402
from routers.helper import router as helper_router               # noqa: E402
from routers.image import router as image_router                 # noqa: E402
from routers.templates import router as templates_router         # noqa: E402
from services import context_store                               # noqa: E402
from services.identity import session_dep                        # noqa: E402
from services.template_manager import TemplateError, inventory   # noqa: E402
from tools.registry import missing_implementations               # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动自检 —— 把"运行到第 8 步才发现"的坑提前到进程启动的那一刻"""
    logger.info("=" * 56)
    logger.info("换颜 · 图像创作 Agent 后端启动")
    # ★ 配额护栏的前提自检（多进程会让进程内计数失效）
    _qg = check_multiworker(logger)
    if _qg["safe"]:
        # 配额上限已从 config 具名导入（见文件头的 from config import (...)），
        # 不在函数体里 import 整个 config —— 那是这个项目与 infra 的循环依赖雷区。
        logger.info("配额护栏：单进程，计数有效（上限 %s）",
                    MAX_GENERATIONS_PER_SESSION)
    else:
        logger.error("★ 配额护栏风险：%s", _qg["message"])
    logger.info("=" * 56)

    # ① 家族 / 模板能否全部加载（YAML 写错在这里就暴露，而不是点生成时 500）
    try:
        info = inventory()
        logger.info("模板清单：%s", info["by_kind"])
        if not info["by_kind"].get("family"):
            logger.error("没有加载到任何家族 YAML —— Agent 将无从选择风格")
        # 坏文件是被跳过的（其余仍可用），但绝不静默 —— 启动时就喊出来
        for err in info.get("errors", []):
            logger.error("模板文件被跳过：%s → %s", err["file"], err["error"])
    except TemplateError as e:
        logger.error("模板加载失败：%s", e)

    # ② 工具契约 vs 实现是否对齐（差一个就会运行时「工具没实现」）
    missing = missing_implementations()
    if missing:
        logger.error("工具契约与实现不一致：%s", missing)
    else:
        logger.info("工具契约与实现一致")

    # ③ 存储与上下文库
    logger.info("图片存储：%s", IMAGE_STORAGE_DIR)
    try:
        st = context_store.stats()
        logger.info("上下文库：%s（%sKB，FTS5=%s，分词器=%s）",
                    st["db_path"], st["size_kb"], st["fts5_enabled"], st["fts_tokenizer"])
    except Exception as e:
        logger.error("上下文库不可用：%s", e)

    # ③b 额度对账：回收上次进程「预扣了但没结算」的残票。
    #     不做这一步的话，每次崩溃/重启都会白吃一张额度（只能 /reset-quota 手动清）。
    try:
        recovered = reconcile_reservations()
        pending = context_store.reservations_stats()["pending"]
        if recovered:
            logger.warning("额度对账完成：回收 %d 张残票，当前仍待结算 %d 张",
                           recovered, pending)
        else:
            logger.info("额度对账完成：无残票（待结算 %d 张）", pending)
    except Exception as e:
        logger.error("额度对账失败：%s", e)

    # ④ 关键配置是否配齐
    cfg = public_dict()
    for k in ("seedream_key_set", "seedream_model_set", "llm_key_set", "llm_model_set"):
        if not cfg.get(k):
            logger.warning("%s 未配置 —— 相关功能不可用（详见 /api/health）", k)

    logger.info("监听 %s:%s；CORS 白名单：%s", BACKEND_HOST, BACKEND_PORT, CORS_ORIGINS)
    try:
        yield
    finally:
        # ★ 2026-10-06：退出时把清扫线程停掉。它是 daemon，进程本来也会退出，
        #   但留着它会让「重复起进程」（测试、uvicorn --reload）时线程越积越多。
        #   放finally 里 —— 无论正常退出还是抛异常都收。
        try:
            from infra.tasks import shutdown_sweeper

            shutdown_sweeper()
        except Exception as e:                      # noqa: BLE001
            logger.warning("停止清扫线程失败（不影响退出）：%s", e)
        logger.info("后端已退出")


app = FastAPI(
    title="换颜 · 图像创作 Agent",
    description="img2img 保真 + 创意叠加。自建 Tool-use Loop，无 LangGraph。",
    version="1.0.0",
    lifespan=lifespan,
    # ★ 见 config.SERVE_API_DOCS 的注释：默认开放（本地/演示要能翻），
    #   PUBLIC_MODE=1 时自动关闭——公网地址不该白送一份完整接口字典。
    docs_url="/docs" if SERVE_API_DOCS else None,
    redoc_url="/redoc" if SERVE_API_DOCS else None,
    openapi_url="/openapi.json" if SERVE_API_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,                    # 白名单，不再是 "*"
    allow_credentials=CORS_ALLOW_CREDENTIALS,      # 本项目无凭据体系，恒 False
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # ★ X-Session-Id / X-Auth-Token：公开版身份头（自定义头必触发预检，
    #   不在白名单里浏览器会直接拦掉全部 API 调用）
    allow_headers=["Content-Type", "Authorization", "X-Local-Token",
                   "X-Session-Id", "X-Auth-Token"],
    expose_headers=["Content-Disposition"],
)

# ★ 访问控制必须在 CORS 之后注册（Starlette 中间件是「后注册的先执行」，
#   这样 Origin 校验跑在 CORS 处理之前，被拒的请求不会带上 CORS 头，
#   浏览器也就读不到响应体）。
app.add_middleware(LocalAccessMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """给**每个响应**补上浏览器安全响应头

    ★ 为什么加（2026-10-08 参赛收尾）：
      本项目处理的是**用户上传的照片**——一个典型的
      「把不可信文件吃进内存、再把派生结果吐回浏览器」的场景。
      之前只做了 CORS 白名单（管的是"谁能读我"），
      但"我返回的东西浏览器该怎么看"这条线是空的：
        · 没有 nosniff → 浏览器会猜响应类型（MIME sniffing），
          构造一个 `x.png` 实为 HTML 的响应就可能被当脚本执行；
        · 没有 frame-ancestors/X-Frame-Options → 页面能被任何站iframe 套壳，
          用户在假页面里点"确定"；
        · 没有 Referrer-Policy → 用户点站外链接时会把
          `/api/image/...` 这样的路径带出去（含会话 id 的 URL）。
      三行响应头的成本，换掉一整类攻击面。

    ★ 为什么 CSP 写得比"全禁"松：
      这个应用的核心交互就是 **<img> 取图 + fetch 调API**，
      `img-src 'self' data: blob:` 与 `connect-src 'self'` 是刚需。
      所以禁掉 inline script 是可以的（Vite 产物是外链 .js），
      但要说清：**这不是 CSP 完备性证明**，是"挡住最容易得手的那一类"。

    ★ 为什么安全头放在响应生成后统一补，而不是逐个路由写：
      漏一个路由就是漏一个洞，而漏写是**默认行为**（不是显式选择）。
      放在中间件里，"忘了加"这件事就不再可能发生。
    """
    response = await call_next(request)
    # 与现有 CORS 头可能重复？CORS 中间件先注册（执行更晚），
    # 但两者写的是不同头名，不冲突。
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # 图片是本站自己产出的，宽高不限、也不允许被第三方页面套壳引用
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'",
    )
    # 这个 API 不产生 HTML 响应，锁死 MIME 声明
    if "Content-Type" in response.headers and "html" not in response.headers.get("Content-Type", ""):
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


@app.middleware("http")
async def identity_probe(request: Request, call_next):
    """每请求算一次会话键 → 存进 scope（供 Depends 复用）+ 回写 Cookie

    ★ Cookie 不是锦上添花，是刚需：`<img src>` 发不出自定义请求头。
      前端把会话放在 X-Session-Id 里，偏偏**每页几十张图全是 <img>**。
      没有 Cookie 这一份，取图路由看谁都是陌生人，页面会一片空白。

    ★ 只在「真有身份」时写 Cookie：不给陌生人发身份。
      公开版里无头请求的隔离键仍是 default → 只见公共展示图（画廊已改），
      这里不擅自给他造一个会话，避免又多一条隐蔽的写下方式。
    """
    from services.identity import (
        DEFAULT_SESSION,
        SESSION_COOKIE,
        TOKEN_TTL_SEC,
        issue_token,
        resolve_session,
    )

    sid = resolve_session(request)
    request.scope["sid"] = sid
    response = await call_next(request)
    if sid != DEFAULT_SESSION:
        token = issue_token(sid)
        # httponly=True：JS 不需要读它；SameSite=Lax：不影响同站图片携带。
        # httponly 之外不做 secure —— 本地 http 调试也要能用（access 不由它保护）。
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=TOKEN_TTL_SEC, httponly=True, samesite="lax", path="/",
        )
    return response


@app.exception_handler(RequestValidationError)
async def validation_failed(request: Request, exc: RequestValidationError):
    """请求体校验失败 → 干净的 422，**不回显原始请求体**

    ★ 为什么必须自定义（2026-10-06 端到端实测时撞到）：
      FastAPI 的默认处理器会 `jsonable_encoder(exc.errors())`，
      而 `errors()` 里带着 `input` —— 也就是**整个原始请求体**。
      两个后果，一个比一个严重：
        ① 请求体是二进制（客户端误把 multipart 打到 JSON 端点）时，
           `jsonable_encoder` 对 bytes 用 `o.decode()` → UnicodeDecodeError
           → 兜底处理器再抛一次，整条链变成 **500**，客户端拿到的是
           「服务端内部错误」而不是本该给的 422。排障方向被彻底带偏。
        ② 即使解码成功，用户上传的图片/隐私数据也会被**整段写进日志**
           （实测日志里出现了整张 PNG 的二进制），日志体积与信息泄露两头失控。

      正确做法：只回「哪个字段、哪一条约束不满足」，不回显值本身 ——
      校验错误的价值在**定位字段**，不在回显用户数据。
    """
    from infra.logging import new_trace_id

    detail = []
    for err in (exc.errors() or [])[:8]:             # 最多 8 条，够定位了
        loc = ".".join(str(p) for p in (err.get("loc") or []) if p != "body") or "(根)"
        detail.append({"field": loc, "type": err.get("type", ""),
                       "msg": str(err.get("msg", ""))[:120]})
    trace_id = new_trace_id()
    logger.warning("请求体校验失败 %s %s [trace=%s]：%s",
                   request.method, request.url.path, trace_id,
                   "; ".join(f"{d['field']}:{d['type']}" for d in detail) or "未知")
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "code": "invalid_request",
            "error": "请求参数不合法，请检查后重试。",
            "detail": detail,
            "trace_id": trace_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """兜底异常处理 —— 保证前端拿到的永远是 JSON，且**不带内部细节**

    ★ 旧版把 f"{type(exc).__name__}: {exc}" 原样返回：
      OpenAI SDK 的异常消息常含完整 URL / request_id，
      SQLite 异常含 DB 绝对路径 —— 都是信息泄露。
    日志里保留全量（含堆栈），响应只给一个可对账的 trace_id。
    """
    from infra.logging import new_trace_id

    trace_id = new_trace_id()
    logger.exception("未处理异常 %s %s [trace=%s]",
                     request.method, request.url.path, trace_id)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "code": "internal_error",
            "error": "服务端内部错误，请稍后再试或联系开发者。",
            "trace_id": trace_id,
            "path": request.url.path,
        },
    )


# ─────────────── 路由 ───────────────
app.include_router(auth_router)          # 身份：访客码 / 绑定账号 / 登录
# 管理面板整块不派生（理由见文件上方的 import 处），这行同步时剥掉。
app.include_router(image_router)
app.include_router(templates_router)
app.include_router(chat_router)
app.include_router(forge_router)
app.include_router(helper_router)        # 小助手：看图推荐风格


@app.get("/api/health", tags=["system"], summary="健康检查 + 运行全景")
async def health_check(thread_id: str = "default") -> dict:
    cfg = public_dict()
    # 图像生成就绪判定跟随 IMAGE_BACKEND：openai 走 IMAGE_*，ark 走 SEEDREAM_*
    if IMAGE_BACKEND == "openai":
        gen_ok = bool(IMAGE_API_KEY and IMAGE_MODEL)
    else:
        gen_ok = all(cfg[k] for k in ("seedream_key_set", "seedream_model_set"))
    ready = {
        "generation": gen_ok,
        "llm": all(cfg[k] for k in ("llm_key_set", "llm_model_set")),
    }
    try:
        inv = inventory()
    except TemplateError as e:
        inv = {"error": str(e)}
    body = {
        "status": "ok",
        "ready": ready,
        "config": cfg,
        "templates": inv,
        "context": context_store.stats(),
        "governance": policy_snapshot(thread_id),
        "security": security_snapshot(),
        # ★ 多worker 自检（2026-07）：配额护栏是**进程内**计数，
        #   部署成多进程就会静默失效。把它放进 health 是为了让部署者
        #   **自己看得到**，而不是读日志才发现。详见 infra/worker_guard.py
        #   与 tests/probe_multiworker.py 的实证。
        "quota_guard": check_multiworker(logger),
        "reservations": context_store.reservations_stats(),
    }
    if PUBLIC_MODE:
        # 公开版瘦身：服务器绝对路径与内网拓扑不属于访客。
        # 只做值打码、不改结构 —— 前端与排障脚本的取值路径不破。
        # （六节全过一遍 —— 实测 governance.storage_dir 也带绝对路径，漏一节露一处）
        body["config"] = {k: ("（隐藏）" if isinstance(v, str) and ":\\" in v else v)
                          for k, v in cfg.items()}
        for sect in ("config", "templates", "context", "security",
                     "governance", "reservations"):
            body[sect] = _mask_paths(body.get(sect))
    return body


def _mask_paths(obj):
    """递归把含 Windows/Unix 绝对路径痕迹的字符串值打码（health 公开版用）"""
    if isinstance(obj, dict):
        return {k: _mask_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_paths(x) for x in obj]
    if isinstance(obj, str) and (":\\" in obj or obj.startswith("/")):
        return "（隐藏）"
    return obj


@app.get("/api/audit", tags=["system"], summary="最近的操作审计记录")
async def audit_log(limit: int = 20) -> dict:
    if PUBLIC_MODE:
        # 审计是本机排障接口（能看到全部会话的操作流水），公开版一律关闭
        raise HTTPException(403, "这个接口只在本地调试模式开放")
    return {"count": limit, "events": recent_audit(max(1, min(limit, 200)))}


# ─────────────── 静态图片（带会话归属校验）───────────────
#
# ★ 原来这里是一行 `app.mount("/images", StaticFiles(...))`：
#   静态挂载不做任何鉴权，谁拿到路径谁就能取。画廊会把 URL 明文发给调用方，
#   于是「列表已经按会话过滤、取图却门户大开」各判一半 —— 知道一条别人图的
#   路径（历史上画廊泄露过）就能一直取，会话隔离形同虚设。
#   现在换成下面的路由，判定统一问 services/image_access.may_read：
#      · _seed/ 公共展示图 → 人人可读
#      · <自己 sid>/...    → 本人可读（含 <img> 自动带上的 Cookie 身份）
#      · 其他              → 404（不给“它存在”这个信息）
#   目录来自 config（绝对路径），与进程 cwd 无关。
@app.get("/images/{rest:path}", tags=["system"], summary="取图（按会话隔离）")
async def serve_image(rest: str, background_tasks: BackgroundTasks,
                      sid: str = Depends(session_dep)):
    from agents.image_agent import AgentInputError, normalize_reference
    from fastapi.responses import FileResponse
    from services.image_access import may_read, media_type

    try:
        path = normalize_reference("/images/" + unquote(rest))
    except AgentInputError:
        # ★ 越界串统一当「没这图」：给 404 而不是 400，
        #   免得把存储结构当谜题给人猜（400 + 具体原因 = 免费的路径探测 oracle）
        raise HTTPException(404, "图片不存在") from None
    if not os.path.isfile(path):
        raise HTTPException(404, "图片不存在")
    if not may_read(path, sid):
        raise HTTPException(404, "图片不存在")

    return FileResponse(
        path, media_type=media_type(path),
        # private：结果取决于访客身份，不能被公共缓存/CDN 存下来再发给别人
        headers={"Cache-Control": "private, max-age=300"},
        background=background_tasks,
    )

# ─────────────── 前端托管（公开发布模式）───────────────
# 挂在最后：/api/* 与 /images 已注册为路由不受影响；html=True 让 / 直达 index.html。
# dev 模式（dist 不存在或未开 PUBLIC_MODE 且无 dist）自动跳过，vite 继续走 5173。
if PUBLIC_MODE and FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="spa")
    logger.info("前端托管已开启：%s（单端口模式）", FRONTEND_DIST)
elif PUBLIC_MODE:
    logger.warning("PUBLIC_MODE 已开启但找不到前端构建产物 %s —— 请先在 frontend/ 跑构建", FRONTEND_DIST)


if __name__ == "__main__":
    import uvicorn

    # reload 会起一个文件监视子进程，且每次 reload 都会重跑一遍
    # 模块级 init_db()。生产路径不该默认开着，用 DEV=1 显式打开。
    dev = str(os.getenv("DEV", "")).strip().lower() in ("1", "true", "yes", "on")
    uvicorn.run(
        "main:app",
        host=BACKEND_HOST,          # 默认 127.0.0.1，不再监听全网卡
        port=BACKEND_PORT,
        reload=dev,
    )
