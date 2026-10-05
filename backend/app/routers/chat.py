"""聊天接口 —— Agent 对话 + 上传 + 历史 + SSE

════════ 重写要点（对照审查报告）════════

1. **去掉 LangChain / LangGraph**：`HumanMessage`、`StateGraph`、`{"configurable": ...}`
   全部退役，改成普通 dict —— 这也是权利要求（claims）里最值钱的一条：
   「工具调用循环自建」。
2. **修复 P1-10 的上传路径**：旧版写 `"./storage/images"`，实际落盘位置取决于进程 cwd。
   现在统一走 `services/upload.py` → `config.IMAGE_STORAGE_DIR`。
   前端拿到的是 `/images/YYYY-MM-DD/xxx.png`，可以直接拼 baseURL 用。
3. **统一错误语义**：治理拦截 → 409/429（可重试 / 需用户决策），
   入参错误 → 400，LLM 失败 → 写在 result 里（200，因为对话本身没崩）。
4. **SSE**：把「模型正在调哪个工具」实时推给前端。
   Tool-use Loop 的价值一半在于**可观测** —— 看不见的工具调用等于没有。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
from typing import Any, AsyncIterator, Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.image_agent import (                                         # noqa: E402
    AgentInputError,
    resolve_thread_id,
    run_agent,
)
from config import ConfigMissing                                         # noqa: E402
from governance.guard import GovernanceError, policy_snapshot, reset_quota   # noqa: E402
from infra.logging import logger                                             # noqa: E402
from infra import tasks                                                      # noqa: E402
from services import context_store                                           # noqa: E402
from services.identity import merge_thread, session_dep                      # noqa: E402
from services.upload import validate_and_save                                # noqa: E402

router = APIRouter(prefix="/api/chat", tags=["chat"])

# SSE 并发上限：每条流占一条 OS 线程 + 一次付费 Agent 循环，
# 不设上限的话「多开几个标签页」就是把额度打光的捷径。
MAX_SSE_CONCURRENCY = int(os.getenv("MAX_SSE_CONCURRENCY", "4"))
_SSE_MAX = MAX_SSE_CONCURRENCY

# ★ 用 threading.Semaphore 而不是 asyncio.Semaphore，理由有两条：
#   ① 许可的所有者是 **worker 线程**（它才是真正在花钱的那个），
#      不是生成器 —— 生成器在断连时立刻就退了，worker 还在跑当前那一步。
#   ② asyncio.Semaphore 要求 release 发生在同一个事件循环里。
#      worker 线程归还时只能 `call_soon_threadsafe`，而**事件循环一旦已经关闭，
#      那个调用会抛 RuntimeError 被吞掉 → 许可永久泄漏**，几次之后端点就死锁。
#      （这个失败模式我在测试里真的复现了：请求结束后许可停在 3/4。）
#   用线程信号量 + 非阻塞获取，归还完全不依赖事件循环是否还活着。
_sse_gate = threading.Semaphore(MAX_SSE_CONCURRENCY)


# ─────────────────────────── 数据模型 ───────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    thread_id: str = "default"
    image_url: str = ""            # /images/xxx.png（不是本地绝对路径）
    card: dict = Field(default_factory=dict)
    allow_spend: bool = True       # False = 预览模式，禁止出图
    # 用户自定义提示词。**原样**并入最终 prompt，全程不经模型转述 ——
    # 让模型转述的话，它可能改写、截断、或干脆忘掉。
    # extra_mode:
    #   "append"  追加到 creative 段末尾（默认）
    #   "replace" 用你的文字替换 creative 段
    # 两种模式都保留 preserve 与 forbid 的保真约束 —— 那是本项目存在的理由。
    extra_prompt: str = Field("", max_length=2000)
    extra_mode: str = "append"


class AgentOut(BaseModel):
    ok: bool
    reply: str = ""
    thread_id: str
    stopped_reason: str = "completed"
    steps: int = 0
    tool_events: list[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    family_id: str | None = None
    params: dict = Field(default_factory=dict)
    image_url: str | None = None
    size: str | None = None
    error: str | None = None
    # ★ 这几个字段 run_agent 一直在返回，但 AgentOut 没声明 →
    #   FastAPI 按 response_model 把它们静默过滤掉了（审查发现 P2-17）。
    #   结果是「Agent 提前告诉用户这次能不能出图」的能力在非流式接口上丢失。
    aborted: bool = False
    card_level: str | None = None
    generation_ready: bool | None = None
    governance_code: str | None = None
    governance_message: str | None = None


# ─────────────────────────── 对话 ───────────────────────────

@router.post("", response_model=AgentOut, summary="与 Agent 对话（自建 Tool-use Loop）")
async def chat(req: ChatRequest, sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(req.thread_id, sid)   # ★ 公开版：header 会话优先
    try:
        # ★ run_agent 里是同步的 Agent 循环（最多 8 步 × LLM 调用），
        #   直接放进 async def 会冻结事件循环 —— 必须丢线程池。
        result = await run_in_threadpool(
            run_agent,
            req.message,
            thread_id=tid,
            image_url=req.image_url,
            card=req.card,
            allow_spend=req.allow_spend,
            extra_prompt=req.extra_prompt,
            extra_mode=req.extra_mode,
        )
    except AgentInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except GovernanceError as e:
        raise HTTPException(status_code=409, detail={"code": e.code, "message": str(e)}) from e
    except ConfigMissing as e:
        raise HTTPException(status_code=503, detail={"code": e.code, "message": str(e)}) from e
    return result


@router.post("/stream", summary="对话（SSE 实时推送每一步工具调用）")
async def chat_stream(request: Request, req: ChatRequest,
                      sid: str = Depends(session_dep)) -> StreamingResponse:
    """把 Tool-use Loop 的每一步实时推给前端

    Tool-use Loop 有一半价值在于**过程可观测**：
    用户在等图的时候看着「正在查家族 → 正在渲染提示词 → 正在出图」，
    比一个转圈强得多，也让「这到底是个 Agent 还是一个 API wrapper」一目了然。

    ── 三条资源护栏（审查发现 P2-6）────────────────────────
    旧实现有三个洞，都是「看不见但会真花钱」的那种：

    ① **线程无上限**：每个请求 `threading.Thread(...)` 裸起一条。
       50 个并发就是 50 条 OS 线程。现在用 Semaphore 限流，超了直接 429。

    ② **队列无上限**：`queue.Queue()` 不带 maxsize，没人消费时无界增长。
       而且 worker 往一个没人读的队列里写永远不会阻塞。

    ③ **客户端断连不感知**（最要命的一条）：
       Starlette 取消 anyio 任务组时，**同步生成器线程不会被中断** ——
       用户关掉浏览器，worker 会继续跑完整个 Agent 循环（8 步 × LLM × 生图），
       钱照烧。现在 `generate()` 主动探测 `request.is_disconnected()`，
       一旦断开就置 abort 标志，引擎在每一步之间检查并提前收尾。
    """
    # ① 并发限流 —— 拿不到许可就明确拒绝，而不是把线程数堆上去。
    #
    # ★ 两个坑（我第一版都踩了）：
    #   ⓐ `if _sse_slots.locked(): acquire()` 是非原子的 —— `locked()` 与
    #      `acquire()` 之间会被别的请求插进来，两个请求同时通过检查。
    #      改用 `wait_for(acquire(), timeout=0)` 把「尝试获取」变成一次原子动作。
    #   ⓑ `locked()` 的语义是「计数器 == 0」也就是**已满员**，不是「正被占用」。
    #      第一版写 `finally: if _sse_slots.locked(): release()` ——
    #      只要同时活着的流不到上限（value>0），locked() 就是 False，
    #      于是**永远不释放**，许可泄漏到第四次请求之后整个端点死锁。
    #      正确做法是用一个显式的 acquired 标志，别去猜信号量的内部状态。
    # 非阻塞获取：拿不到就直接 429。
    # 这是**原子**的 —— 不会出现「先检查再获取」那种两个请求同时通过的窗口。
    if not _sse_gate.acquire(blocking=False):
        logger.warning("SSE 并发已满（上限 %d），拒绝新请求", _SSE_MAX)
        raise HTTPException(
            429,
            {"code": "too_many_streams",
             "message": f"同时进行的对话已达上限 {_SSE_MAX} 个，请稍后再试。"},
        )

    # ★ 队列用 asyncio.Queue，投递用 call_soon_threadsafe —— 不用线程池轮询。
    #
    # 第一版是 `asyncio.to_thread(q.get, True, 0.5)` + 外层 `wait_for(..., 1.0)`，
    # 两个毛病（复审发现 P2-8）：
    #   ① 每 0.5 秒起一个新线程去阻塞 get，纯 churn；池饱和后外层 wait_for
    #      会取消 to_thread，而线程可能已经把元素取走 → **事件被静默丢弃**，
    #      比如 done 事件丢了，前端永远拿不到 image_url，还不报错。
    #   ② 队列是同步 queue.Queue(maxsize)，`close` 在满队列时会被丢弃 →
    #      生成器再也收不到结束信号 → 一直占着许可。
    # 换成 asyncio.Queue + put_nowait 之后：没有线程、没有轮询、事件不丢。
    loop = asyncio.get_running_loop()
    aq: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue(maxsize=512)
    abort_flag = threading.Event()
    _tid = merge_thread(req.thread_id, sid)   # ★ 公开版：header 会话优先（worker 闭包用）

    def _put(event: str, data: Any) -> None:
        try:
            aq.put_nowait((event, data))
        except asyncio.QueueFull:
            # 只在真满的时候丢，且必须留痕 —— 静默丢事件是最坑的
            logger.error("SSE 队列已满，丢弃事件 %s（客户端消费过慢）", event)

    def publish(event: str, data: Any = None) -> None:
        """从 worker 线程安全地投递到 asyncio 队列"""
        try:
            loop.call_soon_threadsafe(_put, event, data)
        except RuntimeError:
            pass                    # 事件循环已关闭（请求早结束了），忽略

    def _on_event(event: str, payload: dict) -> None:
        publish(event, payload)

    def worker() -> None:
        try:
            # ★ 与后台任务共用同一段调用（/_async 也走这里），
            #   避免两条路径参数走偏
            _run_agent_once(req, _tid, _on_event, abort_flag.is_set)
        except Exception as e:                    # SSE 里绝不让异常裸奔
            logger.exception("SSE 对话失败")
            publish("error", _friendlied_error(e))
        finally:
            publish("close", {})
            # ★ 许可由 **worker** 归还，不是由生成器归还（复审发现 P1-6）。
            #   生成器在客户端断连时立刻就 return 了，但 worker 还在跑当前那一步
            #   （可能是几十秒的生图）。如果许可跟着生成器走，
            #   反复「连上→断开」就能在限流 4 的情况下叠出任意多个仍在花钱的 worker
            #   —— 那这道限流就白设了。
            # ★ 直接归还，不经事件循环 —— 循环可能已经关了（见 _sse_gate 的注释）
            _sse_gate.release()

    try:
        threading.Thread(target=worker, daemon=True, name="sse-worker").start()
    except Exception:
        # 线程起不来就立刻还许可，否则这个槽永远回不来
        logger.exception("SSE worker 线程启动失败")
        _sse_gate.release()
        raise

    async def generate() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(aq.get(), timeout=0.5)
                except (asyncio.TimeoutError, TimeoutError):
                    # 超时只用来定期检查连接是否还在
                    if await request.is_disconnected():
                        logger.info("SSE 客户端已断开，通知 Agent 停止")
                        abort_flag.set()
                        break
                    continue

                if await request.is_disconnected():
                    logger.info("SSE 客户端已断开，通知 Agent 停止")
                    abort_flag.set()
                    break

                payload = json.dumps(data if data is not None else {},
                                     ensure_ascii=False, default=str)
                yield f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
                if event == "close":
                    break
        finally:
            # 这里只负责通知 worker 停；**不释放许可**（那是 worker 的事）
            abort_flag.set()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",      # 关掉 nginx 缓冲，否则推不动
        },
    )


def _friendlied_error(e: Exception) -> dict:
    """异常 → 给前端的那一份（2026-10-04 用户实测的必修项）

    ★ 之前这里是 `{"error": f"{type(e).__name__}: {e}"}`：
      用户第一次点生成就弹一整屏 `BadRequestError: Error code: 400 - {...}`
      —— 既看不懂，也不知道下一步该干嘛，还把中转站的错误体原样送了出去。
      现在：原文 → 日志（带 trace）；前端 → 中文一句 + trace_id 可对账。
    """
    from infra.logging import new_trace_id
    from services.image_generator import friendly_error

    trace_id = new_trace_id()
    logger.error("对话异常原文 [trace=%s]：%s: %s", trace_id, type(e).__name__, e)
    return {"error": friendly_error(e), "trace_id": trace_id}


def _run_agent_once(req: "ChatRequest", tid: str,
                    on_event, should_abort) -> None:
    """跑一次 Agent 循环，把每一步喂给 on_event —— SSE 与后台任务共用这一段

    ★ 为什么要抽出来：异步版本和 SSE 版本如果各写一份调用参数，
      早晚会出现「本地 SSE 正常、线上异步少传了个字段」这种幽灵 bug。
      现在两者共用同一处真源，差异只在事件往哪儿送。
    """
    result = run_agent(
        req.message,
        thread_id=tid,
        image_url=req.image_url,
        card=req.card,
        allow_spend=req.allow_spend,
        extra_prompt=req.extra_prompt,
        extra_mode=req.extra_mode,
        on_event=on_event,
        should_abort=should_abort,
    )
    on_event("done", result)


# ═════════════════ 异步对话任务（云端 60 秒网关的正确答案）═════════════════
#
# ★ 为什么必须有这一组端点（2026-10-03 线上事故）：
#   托管平台的反向代理对每个 HTTP 请求有 **60 秒硬超时**（实测 60.099s 返回 504），
#   而一次真实出图是「对话 + extract_card 视觉模型 + 生图」，实测 1~7 分钟。
#   只要它撑在一个同步请求里，云端必然掐断 —— 表现就是用户看到的
#   「卡在 extract_card 不动，然后弹错」。
#
#   本机直连没有反代，所以这个问题在本地永远测不出来。
#   解法是把请求和耗时脱钩：提交 → 秒回 task_id → 前端轮询。
#   质量一点不让：视觉模型照跑，只是不再占用那根 HTTP 连接。

@router.post("/async", summary="后台对话（立即返回任务号，不受 60 秒网关限制）")
async def chat_async(req: ChatRequest, sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(req.thread_id, sid)

    def _fn(emit, should_abort):
        try:
            _run_agent_once(req, tid, lambda e, d=None: emit(e, d), should_abort)
        except Exception as e:
            # 异常既要写进任务（前端可读），也要抛给任务中心去登记 failed
            # ★ 原始报文只进日志 + 任务详情（本地排障），给前端的那份必须是中文
            emit("error", _friendlied_error(e))
            raise

    try:
        task_id = tasks.submit(name="对话出图", kind="chat", owner=sid, fn=_fn,
                               meta={"thread_id": tid})
    except tasks.TaskBusy as e:
        raise HTTPException(429, {"code": "task_busy", "message": str(e)})
    return {"ok": True, "task_id": task_id, "status": "running"}


@router.get("/tasks/active", summary="我还没结束的后台对话")
async def chat_tasks_active(sid: str = Depends(session_dep)) -> dict:
    return {"ok": True, "items": tasks.active(sid)}


@router.get("/tasks/{task_id}", summary="查一个后台对话任务（带增量事件）")
async def chat_task(task_id: str, cursor: int = 0,
                    sid: str = Depends(session_dep)) -> dict:
    """轮询用：返回 task 快照 + cursor 之后的增量事件 + 累积文本

    cursor 是下一次要传回来的下标；text 是截至当前的完整流式文本，
    前端按自己已渲染的长度切片取增量即可（避免逐 token 存事件导致内存膨胀）。
    """
    try:
        snap = tasks.snapshot(task_id, sid, max(0, cursor))
    except KeyError:
        # 不存在 / 不是本人的 → 统一 404，不泄露「这个 ID 存不存在」
        raise HTTPException(404, {"code": "task_not_found",
                                  "message": "没有找到这个任务，可能已经完成并被清理了。"})
    snap["ok"] = True
    return snap


@router.post("/tasks/{task_id}/cancel", summary="中止一个进行中的后台对话")
async def chat_task_cancel(task_id: str, sid: str = Depends(session_dep)) -> dict:
    try:
        ok = tasks.cancel(task_id, sid)
    except KeyError:
        raise HTTPException(404, {"code": "task_not_found",
                                  "message": "没有找到这个任务，可能已经完成并被清理了。"})
    return {"ok": True, "cancelled": ok}


@router.post("/upload", summary="上传参考图")
async def upload_image(file: UploadFile = File(...),
                       sid: str = Depends(session_dep)) -> dict:
    """上传 → 解码校验 → 按真实格式落盘，返回可直接使用的 URL

    `validate_and_save` 内部已经把「空文件 / 超限 / 不是图片 / 格式不支持」
    翻译成带中文说明的 HTTPException，这里不需要再包一层。
    """
    saved = await run_in_threadpool(validate_and_save, file, "chat", sid)
    return {
        "url": saved["url"],
        "key": saved["key"],
        "filename": saved["filename"],
        "width": saved.get("width"),
        "height": saved.get("height"),
        "bytes": saved.get("bytes"),
        "format": saved.get("format"),
        "orientation": (
            "portrait" if (saved.get("height") or 0) > (saved.get("width") or 0)
            else "landscape"
        ),
    }


@router.get("/history", summary="查看某个会话的对话历史")
async def get_history(thread_id: str = "default", limit: int = 40,
                      sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(thread_id, sid)
    msgs = context_store.history(tid, limit=max(1, min(limit, 200)))
    return {"thread_id": tid, "count": len(msgs), "messages": msgs}


@router.delete("/history", summary="清空某个会话")
async def clear_history(thread_id: str = "default",
                        sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(thread_id, sid)
    n = context_store.clear_thread(tid)
    return {"thread_id": tid, "deleted": n}


@router.get("/search", summary="在会话历史里做全文检索（SQLite FTS5）")
async def search_history(q: str, thread_id: str = "default", limit: int = 5,
                         sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(thread_id, sid)
    hits = context_store.search(tid, q, limit=max(1, min(limit, 20)))
    return {"thread_id": tid, "query": q, "count": len(hits), "hits": hits}


@router.get("/policy", summary="查看治理策略与剩余配额")
async def get_policy(thread_id: str = "default",
                     sid: str = Depends(session_dep)) -> dict:
    return policy_snapshot(merge_thread(thread_id, sid))


@router.post("/reset-quota", summary="重置某个会话的生成额度")
async def reset(thread_id: str = "default",
                sid: str = Depends(session_dep)) -> dict:
    tid = merge_thread(thread_id, sid)
    return {"thread_id": tid, "quota": reset_quota(tid)}
