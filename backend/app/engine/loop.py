"""引擎层 · Tool-use Loop —— 自建，替代 LangGraph

════════ 为什么不用 LangGraph（方案 §0 决策 5 的落地）════════

LangGraph 的价值在于「任意拓扑的状态图 + 持久化 + 分支回溯」。
本项目实际需要的是一条**单张图片的确定性流水线**：
选择家族 → 描述参数 → 渲染预览 → 出图 → 总结。
它不分支、不回溯、不自省 —— 图上只有一个方向。
用 LangGraph 换来的是：
  - 4 个 LangChain 系依赖 + 版本地狱
  - `StateGraph` 的状态合并语义（本项目为此引入 `Annotated[list, operator.add]`）
  - 每轮都要 `{"configurable": {"thread_id": ...}}` 的仪式
而收益是零。这部分连带 enforcing 出的 bug 已在审查报告里列过。

换成自建循环之后：
  - 依赖只剩 `openai`（原生 tools / tool_choice，结构化由协议保证）
  - 对话历史落 SQLite（`services/context_store`），换套 pond 还能 FTS 检索
  - 循环护栏（步数 / 观察长度 / 重复调用熔断）全部显式可见

════════ 三条护栏 ════════

1. **步数**：MAX_AGENT_STEPS。超了就不再给工具，强制做一次总结收尾。
2. **观察长度**：MAX_TOOL_OBSERVATION_CHARS。工具返回的 JSON 可能很长
   （提示词全文上千字符），不截断会把下一轮的输入 token 数悄悄翻倍。
3. **重复熔断**：同一个 (工具名, 参数) 签名出现超过 REPEAT_FUSE 次直接掐断。
   LLM 卡在某个错误上时会在同一个调用上打转 —— 这是烧钱最快的方式。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import MAX_AGENT_STEPS, MAX_TOOL_OBSERVATION_CHARS      # noqa: E402
from contracts.tools import SPEND_TOOLS, TOOL_NAMES, openai_tools    # noqa: E402
from infra.logging import audit, logger, step                        # noqa: E402
from services import context_store                                   # noqa: E402
from services.llm import LLMError, chat_with_tools                   # noqa: E402
from tools.registry import ToolContext, dispatch, missing_implementations  # noqa: E402

from .prompts import build_messages, build_system_prompt, compact_families_for_prompt  # noqa: E402

HISTORY_LIMIT = 20          # 喂给模型的最大历史条数
REPEAT_FUSE = 2             # 同一签名允许的最大重复次数（超过就熔断）
MEMORY_TRIGGER = 12         # 消息数超过多少触发一次长期记忆更新


@dataclass
class AgentRunResult:
    """一次 Agent 运行的完整结果"""
    reply: str = ""
    steps: int = 0
    stopped_reason: str = "completed"      # completed / step_limit / repeat_fuse / llm_error
    tool_events: list[dict] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)   # 完整消息轨迹，给 SSE / 调试用


def _clip_observation(obs: Any, limit: int) -> str:
    try:
        text = json.dumps(obs, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obs)
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.8)]
    return head + f"…[已截断，原始 {len(text)} 字符]"


def _observation_summary(obs: Any) -> str:
    """给 SSE / 日志用的一句话摘要 —— 不要把整个观察结果推给外部"""
    if not isinstance(obs, dict):
        return str(obs)[:80]
    if obs.get("error"):
        return f"失败：{str(obs['error'])[:80]}"
    if obs.get("success"):
        return f"成功：{obs.get('family_id')} · {obs.get('size')}"
    keys = [k for k in ("id", "name", "prompt_chars", "orientation", "quota") if k in obs]
    if keys:
        return "、".join(f"{k}={obs[k]}" for k in keys)[:120]
    return f"{len(obs)} 个字段"


def _signature(name: str, args: dict) -> str:
    try:
        return name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return name + "|" + str(args)


def run(
    user_message: str,
    *,
    thread_id: str = "default",
    image_path: str = "",
    image_info: dict | None = None,
    card: dict | None = None,
    allow_spend: bool = True,
    memory_update: bool = True,
    history_limit: int = HISTORY_LIMIT,
    on_event: Callable[[str, dict], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    extra_prompt: str = "",
    extra_mode: str = "append",
) -> AgentRunResult:
    """跑一轮 Tool-use Loop

    返回 AgentRunResult。这个函数**不抛业务异常**：
    LLM 断网、工具崩溃都会在 result.stopped_reason 里体现，
    因为 Agent 失败也是产品的一部分，前端要能优雅呈现。

    on_event：可选的进度回调，用于 SSE 实时推送。
      刻意做成显式传参而不是「外部猴子补丁 dispatch」——
      后者是模块级可变全局状态，并发两个请求时会互相串台。

    should_abort：可选的取消探针，**在每一步之间检查**。
      SSE 场景下客户端一关浏览器，连接就没了，但同步的 worker 还在跑。
      没有这个探针的话，用户关掉页面后 Agent 会继续走完剩余步骤 ——
      该花的钱已经花了，但不该再花后面的。这是"关掉浏览器还在烧钱"的解药。
    """

    def emit(event: str, **payload: Any) -> None:
        if on_event:
            try:
                on_event(event, payload)
            except Exception as e:                 # 推送失败绝不能拖垮 Agent
                logger.warning("事件回调失败(%s)：%s", event, e)

    def aborted() -> bool:
        if should_abort is None:
            return False
        try:
            return bool(should_abort())
        except Exception:                          # 探针自己出错就当没取消
            return False

    emit("start", thread_id=thread_id, allow_spend=allow_spend)
    result = AgentRunResult()
    # 已经推给前端的图片 URL —— 用于「同一张图只推一次」
    emitted_image_url: str | None = None

    # 契约里声明了但没实现的工具 —— 启动即暴露，不要等到第 8 步才炸
    missing = missing_implementations()
    if missing:
        logger.error("工具契约与实现不一致：%s", missing)
        audit("tool_contract_mismatch", missing=missing)

    # ── 能力裁剪：预览模式下计费工具连 Schema 都不给模型看见
    enabled = set(TOOL_NAMES)
    if not allow_spend:
        enabled -= set(SPEND_TOOLS)

    with step("Agent 运行", thread=thread_id, allow_spend=allow_spend) as run_ctx:
        families = compact_families_for_prompt()
        long_term = context_store.load_long_term(thread_id) if memory_update else {}
        # ★ 用 history_for_llm 而不是 history：
        #   后者是「如实呈现库里的内容」，前者额外做 tools 协议配对净化。
        #   直接喂 history 会在两种正常场景下构造出非法请求体（窗口截断 /
        #   上一轮提前中止），服务端一律 400 —— 见 context_store.history_for_llm。
        history = context_store.history_for_llm(thread_id, limit=history_limit)

        system_prompt = build_system_prompt(
            enabled_tools=enabled,
            families=families,
            image_info=image_info,
            card=card,
            long_term=long_term,
        )
        messages = build_messages(user_message, system_prompt=system_prompt, history=history)
        result.trace = [dict(m) for m in messages]

        ctx = ToolContext(
            thread_id=thread_id,
            image_path=image_path,
            card=dict(card or {}),
            image_info=dict(image_info or {}),
            allow_spend=allow_spend,
            # 用户自定义提示词：请求级直传，不经模型转述（见 ToolContext 注释）
            extra_prompt=extra_prompt or "",
            extra_mode=extra_mode or "append",
        )

        # 用户消息入库
        context_store.append_message(thread_id, "user", user_message)

        tools_spec = openai_tools(enabled)
        repeat_counts: dict[str, int] = {}
        forced_final = False          # 出图成功后强制模型写总结，不再给工具

        for step_i in range(1, MAX_AGENT_STEPS + 1):
            result.steps = step_i

            # ★ 断连检查放在「每步之前」——这是最经济的粒度：
            #   已经发出的那次 LLM/生图请求没法收回，但下一步可以不做。
            if aborted():
                logger.info("客户端已断开，Agent 在第 %d 步提前收尾", step_i)
                result.stopped_reason = "client_abort"
                result.reply = "（已中止：客户端断开连接）"
                audit("agent_aborted", thread_id=thread_id, step=step_i)
                break

            emit("step", step=step_i, max_steps=MAX_AGENT_STEPS)

            try:
                resp = chat_with_tools(
                    messages,
                    tools=[] if forced_final else tools_spec,
                    tool_choice="none" if forced_final else "auto",
                )
            except LLMError as e:
                logger.error("LLM 调用失败：%s", e)
                emit("error", stage="llm", message=str(e)[:200])
                result.stopped_reason = "llm_error"
                result.reply = f"（模型调用失败）{e}"
                audit("agent_llm_error", thread_id=thread_id, error=str(e)[:200])
                break

            for key, val in (resp.get("usage") or {}).items():
                if isinstance(val, int):
                    result.usage[key] = result.usage.get(key, 0) + val

            content = resp.get("content") or ""
            calls = resp.get("tool_calls") or []
            if content:
                emit("token", text=content)
            for call in calls:
                emit("tool_call", name=call["name"], arguments=call["arguments"])

            # 助手消息入 trajectory（要带 tool_calls 结构，否则下一轮协议不认）
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            assistant_msg["content"] = content or None
            if calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in calls
                ]
            messages.append(assistant_msg)
            result.trace.append(dict(assistant_msg))
            if content:
                context_store.append_message(thread_id, "assistant", content)
            if calls:
                context_store.append_message(
                    thread_id, "assistant", None, meta={"tool_calls": calls}
                )

            # ── 没有工具调用 = 模型认为可以回答了
            if not calls:
                result.stopped_reason = "completed"
                result.reply = content
                break

            # ── 逐个执行工具
            stop_reason = ""
            for call in calls:
                name = call["name"]
                args = call["arguments"] or {}

                # 工具之间也查一次：一次工具调用可能耗时几十秒（生图），
                # 用户早关页面了就没必要继续下一个
                if aborted():
                    stop_reason = "client_abort"
                    obs = {"error": "客户端已断开，停止执行。",
                           "hint": "这是正常的中止，不是错误。"}
                    logger.info("工具执行中检测到断连，停止 %s", name)
                else:
                    sig = _signature(name, args)
                    repeat_counts[sig] = repeat_counts.get(sig, 0) + 1
                    if repeat_counts[sig] > REPEAT_FUSE:
                        stop_reason = "repeat_fuse"
                        obs = {
                            "error": f"你已经用完全相同的参数调用 {name} "
                                     f"{repeat_counts[sig]} 次，结果不会改变。",
                            "hint": "请换一组参数，或直接给用户一个结论。",
                        }
                        logger.warning("重复调用熔断：%s", sig[:120])
                    else:
                        emit("tool_start", name=name, arguments=args)
                        tool_res = dispatch(name, args, ctx)
                        obs = tool_res.observation
                        if tool_res.terminal:
                            forced_final = True
                        emit(
                            "tool_end",
                            name=name,
                            ok=tool_res.ok,
                            terminal=tool_res.terminal,
                            summary=_observation_summary(obs),
                        )
                        # ★ 图一落盘就先把 URL 推给前端（2026-10-05，用户反馈
                        #   "生成成功后回到画布太慢"）。
                        #   此前只有 emit("done") 才带 image_url，而 done 发生在
                        #   **收尾 LLM 调用之后** —— 图其实早就好了，用户却在空白
                        #   画布上多等一整轮 LLM（实测 20~60s，钱也是这次花的）。
                        #   这里在工具成功的瞬间就送一次，前端可以立刻上画布，
                        #   收尾文案照常生成（不省任何质量），只是不再挡在图前面。
                        #   去重：重试同一张图时不会重复推。
                        _img = obs.get("image_url") if isinstance(obs, dict) else None
                        if _img and _img != emitted_image_url:
                            emitted_image_url = _img
                            emit("image", url=_img,
                                 size=obs.get("size"), family_id=obs.get("family_id"),
                                 aspect_warning=obs.get("aspect_warning") or "")

                obs_text = _clip_observation(obs, MAX_TOOL_OBSERVATION_CHARS)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": obs_text,
                }
                messages.append(tool_msg)
                result.trace.append(dict(tool_msg))
                context_store.append_message(
                    thread_id, "tool", obs_text,
                    tool_call_id=call["id"], tool_name=name,
                )
                result.tool_events.append({
                    "step": step_i,
                    "name": name,
                    "arguments": args,
                    "ok": not bool(obs.get("error")) if isinstance(obs, dict) else True,
                    "observation_chars": len(obs_text),
                })

                if stop_reason:
                    break

            run_ctx["steps"] = step_i
            run_ctx["tools"] = len(result.tool_events)

            # 客户端断连：不再做任何收尾 LLM 调用（那要花钱），直接结束
            if stop_reason == "client_abort":
                result.stopped_reason = "client_abort"
                result.reply = "（已中止：客户端断开连接）"
                break

            if stop_reason == "repeat_fuse":
                result.stopped_reason = "repeat_fuse"
                # 熔断后再给一次「只写结论」的机会，避免把空白留给用户。
                # 但先补齐未执行的 tool 回复 —— 否则这次请求体不合法，必然 400。
                filled = _fill_missing_tool_replies(messages)
                if filled:
                    logger.info("熔断收尾：补了 %d 条未执行的工具回复", filled)
                messages.append({
                    "role": "user",
                    "content": "（系统提示）请停止重复调用，直接用中文给这句任务一个简短结论。",
                })
                try:
                    final = chat_with_tools(messages, tools=[], tool_choice="none")
                    result.reply = (final.get("content") or "").strip() or (
                        "抱歉，这一步我没有得出可用的结论，换个说法或换张图再试试。"
                    )
                except LLMError as e:
                    result.reply = f"（模型调用失败）{e}"
                break
        else:
            # ── 步数耗尽：强制收尾
            result.stopped_reason = "step_limit"
            logger.warning("Agent 达到步数上限 %d，强制总结", MAX_AGENT_STEPS)
            messages.append({
                "role": "user",
                "content": "（系统提示）已达最大步数，请直接用中文给出简短结论，不要再调用工具。",
            })
            try:
                final = chat_with_tools(messages, tools=[], tool_choice="none")
                result.reply = (final.get("content") or "").strip() or (
                    "这一步走了太久，还没得出结果。可以更具体地说说你想要什么效果。"
                )
            except LLMError as e:
                result.reply = f"（模型调用失败）{e}"

        result.artifacts = dict(ctx.artifacts)
        emit("finish", stopped_reason=result.stopped_reason, steps=result.steps)

        if result.reply:
            context_store.append_message(thread_id, "assistant", result.reply)

        if memory_update and context_store.message_count(thread_id) >= MEMORY_TRIGGER:
            _update_long_term(thread_id)

        audit(
            "agent_run",
            thread_id=thread_id,
            steps=result.steps,
            tools=len(result.tool_events),
            stopped_reason=result.stopped_reason,
            generated=bool(result.artifacts.get("image_url")),
        )
        return result


def _fill_missing_tool_replies(messages: list[dict]) -> int:
    """把「声明了但没执行」的 tool_calls 补上占位回复

    ★ 为什么必须补（审查发现 P0-2 的同一根源）
    ----------------------------------------
    熔断 / 中止时我们会提前跳出工具循环，此时 assistant 消息已经声明了
    N 个 tool_calls，但只产生了 M < N 条 tool 回复。
    OpenAI tools 协议要求**每个 tool_call_id 都有且只有一条 tool 消息**，
    不补齐的话下一次请求直接 400（前端只看到「模型调用失败」，极难定位）。

    返回补了几条。
    """
    # 找最后一条带 tool_calls 的 assistant
    pending: list[str] = []
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            break
    if not pending:
        return 0

    answered = {
        m.get("tool_call_id") for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    filled = 0
    for cid in pending:
        if cid in answered:
            continue
        messages.append({
            "role": "tool",
            "tool_call_id": cid,
            "content": json.dumps(
                {"error": "该工具调用未被执行（Agent 提前收尾）",
                 "hint": "这是正常的中止，不是工具错误。"},
                ensure_ascii=False,
            ),
        })
        filled += 1
    return filled


def _update_long_term(thread_id: str) -> None:
    """压缩历史 + 抽取偏好 —— 用小模型秘书（reasoning_effort=none）

    失败不影响主流程：记忆更新是「nice to have」，不该因为摘要失败让整轮对话失败。
    """
    try:
        from services.llm import secretary
        from services.context_store import history, message_count, save_long_term

        msgs = history(thread_id, limit=MEMORY_TRIGGER)
        text = "\n".join(
            f"{m.get('role')}: {(m.get('content') or '')[:200]}" for m in msgs
        )
        summary = secretary([
            {"role": "system", "content":
             "把这段对话压缩成 120 字以内的中文摘要，保留：用户的风格偏好、"
             "已经尝试过的家族、用户的反馈。只输出摘要本身。"},
            {"role": "user", "content": text[:4000]},
        ], max_tokens=220)
        if summary:
            old = context_store.load_long_term(thread_id)
            prefs = old.get("prefs") or {}
            save_long_term(thread_id, summary, prefs)
            logger.info("已更新会话 %s 的长期记忆（%d 字符）", thread_id, len(summary))
    except Exception as e:                       # 记忆失败绝不能拖垮对话
        logger.warning("长期记忆更新跳过：%s", type(e).__name__)


__all__ = ["AgentRunResult", "run"]
