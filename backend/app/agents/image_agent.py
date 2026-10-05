"""图片生成 Agent —— 门面层（facade）

════════ 这个文件曾经是整个项目最重的地方，现在它是整层最薄的 ════════

LangGraph 版本在这里放了 130 行：StateGraph、4 个节点、checkpointer、
`Annotated[list[AnyMessage], operator.add]`……
但那 130 行实际做的只是：一条不分支、不回溯、不自省的单行流水线。

现在的分工（方案 §0 决策 5 的工程落地）：

    engine/prompts.py   System Prompt 六层装配
    engine/loop.py      循环本体 + 三条护栏
    tools/registry.py   五个工具的实现
    governance/guard.py 预算与开关
    contracts/tools.py  工具契约

本文件只剩一件事：**把一次 HTTP 请求映射到 `engine.loop.run()`**，
并把结果整理成 API 层好用的形状。

留着它的理由不是技术需要，而是**可读性**：
读代码的人问「Agent 在哪」，答案应该是一处，而不是散在 engine/tools/governance 四处。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.loop import AgentRunResult, run as _run            # noqa: E402
from governance.guard import GovernanceError, check_generation_allowed  # noqa: E402
from infra.logging import logger                              # noqa: E402
from infra.storage import from_url, to_url                    # noqa: E402
from services.card_extractor import build_card                # noqa: E402
from services.upload import load_upload_as_card_hint          # noqa: E402


class AgentInputError(Exception):
    """入参不合法 —— 应由 API 转成 400"""


def resolve_thread_id(thread_id: str | None) -> str:
    """规范化 thread_id

    旧版直接用用户传来的字符串做 DB key 和文件路径片段，
    一旦包含 `..` 或路径分隔符就会被写进磁盘 path。这里做白名单清洗。
    """
    tid = (thread_id or "").strip()
    if not tid:
        return "default"
    cleaned = "".join(ch for ch in tid if ch.isalnum() or ch in "-_")
    return cleaned or "default"


def build_image_info(image_path: str) -> dict:
    """从参考图读出客观信息 —— 交给 read_image_info 工具"""
    info = load_upload_as_card_hint(image_path)
    if not info:
        return {}
    try:
        info["bytes"] = os.path.getsize(image_path)
    except OSError:
        pass
    info.setdefault("filename", os.path.basename(image_path))
    return info


def normalize_reference(image_url_or_path: str | None) -> str:
    """接受 `/images/xxx.png` 这种 URL，也接受绝对路径 —— 统一换成绝对路径

    前端发过来的一直是 `/images/...`，后端到处在手写 `"./" + url` 的拼接，
    拼出来的路径取决于进程 cwd（见审查报告 P1-10）。现在统一走 infra.storage。
    """
    raw = (image_url_or_path or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        raise AgentInputError(f"图片地址应为站内路径，收到：{raw[:60]}")
    # 不论 `/images/xxx`、`images/xxx` 还是绝对路径，from_url 都能归一；
    # 越界（穿越到存储目录之外）会在 from_url 里被 ensure_within 拦下。
    # （这里曾有一段 if/else 两个分支返回完全相同的值——纯粹的死代码。）
    try:
        return str(from_url(raw))
    except ValueError as e:
        # ★ 越界 / 非法 key 是**用户输入问题**，必须翻译成 400，
        #   否则会冒到全局异常处理器变成 500 —— 既误导用户，
        #   也在日志里淹掉真正该关注的内部错误。
        raise AgentInputError(f"非法的图片地址：{e}") from e


def run_agent(
    message: str,
    *,
    thread_id: str = "default",
    image_url: str = "",
    card: dict | None = None,
    allow_spend: bool = True,
    memory_update: bool = True,
    on_event: Callable[[str, dict], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    extra_prompt: str = "",
    extra_mode: str = "append",
) -> dict:
    """跑一轮 Agent —— 返回可直接 JSON 序列化的 dict

    刻意不抛业务异常：
    - 治理拦截 → 放进 result["error"] + code
    - LLM 失败 → stopped_reason="llm_error"
    Agent 失败也是产品的一部分，不该把 HTTP 请求炸成 500。
    """
    message = (message or "").strip()
    if not message:
        raise AgentInputError("消息内容不能为空")

    tid = resolve_thread_id(thread_id)
    image_path = normalize_reference(image_url) if image_url else ""

    image_info = build_image_info(image_path) if image_path else {}

    # ★ 自动补一张本地档的创作卡（0 成本）。
    #   为什么要自动：card 是渲染「反推 forbid」的唯一数据源，没有它
    #   渲染器只能产出通用约束。本地档只做 Pillow 取色，不调任何模型，
    #   所以对「预览 0 成本」的承诺没有影响。
    #   主体 / 锚点需要视觉模型 —— 那由 Agent 显式调用 extract_card(use_vlm=true)
    #   或走 /api/image/generate 时补上。
    card = dict(card or {})
    if not card and image_path:
        try:
            card = build_card(image_path, use_vlm=False)
        except Exception as e:                   # 提炼失败不该挡住对话
            logger.warning("本地 card 预提取失败：%s", e)

    # 出图之前先过一遍治理检查：让用户尽早知道「这次能不能出图」，
    # 而不是陪模型走完 6 步工具调用之后才被告知额度已用完。
    preflight: dict[str, Any] = {}
    if allow_spend and image_path:
        try:
            check_generation_allowed(tid, reference_image=image_path)
            preflight["generation_ready"] = True
        except GovernanceError as e:
            preflight["generation_ready"] = False
            preflight["governance_code"] = e.code
            preflight["governance_message"] = str(e)
    elif not image_path:
        preflight["generation_ready"] = False
        preflight["governance_message"] = "尚未上传原图"

    try:
        result: AgentRunResult = _run(
            message,
            thread_id=tid,
            image_path=image_path,
            image_info=image_info,
            card=card,
            allow_spend=allow_spend,
            memory_update=memory_update,
            on_event=on_event,
            should_abort=should_abort,
            extra_prompt=extra_prompt,
            extra_mode=extra_mode,
        )
    except Exception as e:                        # 引擎承诺不抛，这里是最后一道保险
        logger.exception("Agent 运行异常")
        # ★ 原文严禁直出界面（2026-10-04 用户实测：弹出「BadRequestError:
        #   Error code: 400 - {...}」，既看不懂也不知道怎么办，还等于把
        #   上游中转站的地址/ policies 细节泄露给访客）。
        #   排障靠日志和 trace_id，界面只留一句中文 + 下一步。
        from infra.logging import new_trace_id
        from services.image_generator import friendly_error

        trace_id = new_trace_id()
        logger.error("Agent 异常原文 [trace=%s]：%s: %s", trace_id, type(e).__name__, e)
        return {
            "ok": False,
            "reply": "",
            "error": friendly_error(e),
            "error_raw": f"{type(e).__name__}: {str(e)[:300]}",   # 仅供本地排障
            "trace_id": trace_id,
            "stopped_reason": "internal_error",
            "thread_id": tid,
            **preflight,
        }

    artifacts = dict(result.artifacts)
    out: dict[str, Any] = {
        "ok": result.stopped_reason in ("completed",),
        "aborted": result.stopped_reason == "client_abort",
        "reply": result.reply,
        "thread_id": tid,
        "stopped_reason": result.stopped_reason,
        "steps": result.steps,
        "tool_events": result.tool_events,
        "usage": result.usage,
        "family_id": artifacts.get("family_id") or artifacts.get("last_family"),
        "params": artifacts.get("params") or artifacts.get("last_params") or {},
        "image_url": artifacts.get("image_url"),
        "size": artifacts.get("size"),
        "aspect_warning": artifacts.get("aspect_warning") or "",
        "error": None if result.stopped_reason == "completed" else result.reply,
    }

    if not out["ok"] and not out["error"]:
        out["error"] = f"Agent 以 {result.stopped_reason} 结束"
    out.update(preflight)
    return out


__all__ = [
    "AgentInputError",
    "build_image_info",
    "normalize_reference",
    "resolve_thread_id",
    "run_agent",
]
