"""服务层 · LLM 调用 —— 带工具调用、JSON 修复、预算可见

重写要点
--------
1. **不再手搓 JSON 让模型"只返回JSON"**（旧 `choose_template` 用字符串切割兜镖，
   遇到 Markdown 围栏、多输出、前言后语就崩）。改用 OpenAI 原生 tools，
   结构化由协议保证。

2. **保留 `reasoning_effort=none` 的秘书通道**（实测 reasoning_tokens=0），
   以及前缀缓存带来的省钱效果。缓存命中的前提是 **system 前缀稳定**，
   这正是 engine/prompts.py 要把 System Prompt 分层且稳定的理由。

3. **失败要带上下文**：只抛裸异常时无法回答"是网络还是鉴权还是超长"。
   现在统一包成 LLMError，带上 model 与原始错误类型。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, NamedTuple

from openai import OpenAI

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (                                    # noqa: E402
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    # 通道容错开关：主通道挂掉时能不能换条路走
    LLM_FALLBACK_DOWNGRADE,
    LLM_FALLBACK_TO_PREMIUM,
    MAX_AGENT_STEPS,
    REQUEST_TIMEOUT_SEC,
    assert_ready_for_llm,

    PREMIUM_API_KEY,
    PREMIUM_BASE_URL,
    PREMIUM_MODEL,

    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
)
from infra.logging import logger                        # noqa: E402

# ── 遗留别名 ──────────────────────────────────────────────
# 本模块内部已改用「通道 + 缓存客户端」（见 _client_for），
# 这两个对象只为向后兼容外部脚本保留；新的调用请走 chat() / vision()。
#
# ★ 为什么占位 key 而不是直接构造（2026-10-05 开源整理时实测）：
#   OpenAI 客户端在 api_key 为空时会在**构造那一刻**抛 OpenAIError，
#   于是"还没配 .env 的新用户"在 import 阶段就崩在一句 SDK 异常上，
#   连"你还没配密钥"这种能照做的提示都看不到。
#   现在给一个显眼的占位 key：导入照常成功，真正调用时仍由
#   _usable() / chat() / vision() 抛出「没有可用的模型通道（请检查 DEEPSEEK_* 配置）」。
_UNSET_KEY = "sk-not-configured"
client = OpenAI(
    base_url=DEEPSEEK_BASE_URL,
    api_key=DEEPSEEK_API_KEY or _UNSET_KEY,
    timeout=REQUEST_TIMEOUT_SEC,
)

# 关键任务通道 client（GPT-Plus / gpt-5.5）：工坊合成/编译/自修/迭代走这里
premium_client = OpenAI(
    base_url=PREMIUM_BASE_URL,
    api_key=PREMIUM_API_KEY or _UNSET_KEY,
    timeout=REQUEST_TIMEOUT_SEC,
    max_retries=0,
)

if not (DEEPSEEK_API_KEY or PREMIUM_API_KEY):
    logger.warning(
        "未检测到 DEEPSEEK_API_KEY / PREMIUM_API_KEY —— "
        "应用可以启动，但对话与工坊不可用；请在项目根的 .env 里配置（可参考 .env.example）。"
    )


class LLMError(Exception):
    """LLM 调用失败 —— 携带原始错误类型，便于区分网络 / 鉴权 / 超长"""


def _close_truncated(raw: str) -> str:
    """把「被 max_tokens 截断」的 JSON 尽量补成合法的

    ★ 来历（2026-10-03 实测）：VLM 提炼 card 时 max_tokens=500 不够用，
      模型刚写完 anchors 就被掐断 —— 整段 JSON 少了结尾的括号。
      旧行为是 `extract_json` 返回 None → VLM 档被整段丢掉（**这次调用的钱白花了**），
      card 退回只有色板的本地档，秋毫必现的「反推 forbid」再次失效。
      与其重来一次（再花钱、再等两分钟），不如把已经到手的完整片段救回来。

    三步：① 结尾落在字符串里 → 补个引号；② 去掉悬空的逗号/冒号；
          ③ 按未闭合的括号栈反向补齐。
    """
    s = raw.rstrip()

    # ⓪ 先砍掉末尾"刚开了头"的结构：`...,{` 这种半截片段修补出来只会是一个空对象。
    #    注意**不**砍引号 —— `{"a":"未写完` 结尾的引号是有信息量的，砍了反而救不回来。
    while s and s[-1] in ",{[:":
        s = s[:-1].rstrip()

    # ① 扫描一遍看结尾是否在字符串内部（不能用 count('"') % 2 —— 遇转义引号就错）
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'

    # ② 悬空的分隔符：`{"a":1,` 或 `{"a":` 后面的东西是残缺的
    while s and s[-1] in ",:":
        s = s[:-1].rstrip()

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """从模型输出里稳健地取出 JSON

    模型偶尔会：加 ```json 围栏 / 前后加寒暄 / 输出对象后还附带解释。
    所以按「围栏 → 最外层花括号配对扫描」两级尝试，而不是简单 json.loads。
    """
    if not text:
        return None
    raw = text.strip()

    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _close_truncated(raw: str) -> str:
    """把「被 max_tokens 截断」的 JSON 尽量补成合法的

    ★ 来历（2026-10-03 实测）：VLM 提炼 card 时 max_tokens=500 不够用，
      模型刚写完 anchors 就被掐断 —— 整段 JSON 少了结尾的括号。
      旧行为是 `extract_json` 返回 None → VLM 档被整段丢掉
      （**这次调用的钱白花了**），card 退回只有色板的本地档，
      秋毫必现的「反推 forbid」再次失效。
      与其重来一次（再花钱、再等两分钟），不如把已经到手的完整片段救回来。

    三步：⓪ 砍掉末尾刚开了头的片段；① 结尾落在字符串里就补个引号；
          ② 去掉悬空的逗号/冒号；③ 按未闭合的括号栈反向补齐。
    """
    s = raw.rstrip()

    # ⓪ 先砍掉末尾"刚开了头"的结构：`...,{` 这种半截片段修补出来只会是一个空对象。
    #    注意**不**砍引号 —— `{"a":"未写完` 结尾的引号是有信息量的，砍了反而救不回来。
    while s and s[-1] in ",{[:":
        s = s[:-1].rstrip()

    # ① 扫描一遍看结尾是否在字符串内部（不能用 count('"') % 2 —— 遇转义引号就错）
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'

    # ② 悬空的分隔符：`{"a":1,` 或 `{"a":` 后面的东西是残缺的
    while s and s[-1] in ",:":
        s = s[:-1].rstrip()

    # ③ 补齐容器
    for opener in reversed(stack):
        s += "]" if opener == "[" else "}"
    return s


def extract_json_lenient(text: str) -> dict | None:
    """比 extract_json 多一层：允许 JSON 被截断

    只在**明确预期会截断**的场合使用（目前是 VLM 提炼 card）；
    其它地方（工具 arguments、样式提炼）坚持用严格的 extract_json ——
    那里宁可判为「解析不了」，也不要悄悄接受一个残缺的参数表。

    策略：先严格解析；不行就逐步砍掉最后一个"写到一半"的片段再补齐括号。
    每一步都要求产出**合法的 dict**，绝不含糊地返回半截对象。
    """
    strict = extract_json(text)
    if strict is not None:
        return strict

    raw = (text or "").strip()
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    if start < 0:
        return None
    candidate = raw[start:]

    for _ in range(40):
        try:
            obj = json.loads(_close_truncated(candidate))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        # 再退一步：砍掉最后一个未写完的元素，重试
        cut = max(candidate.rfind(","), candidate.rfind("{"), candidate.rfind("["))
        if cut <= start:
            break
        candidate = candidate[:cut]
    return None


# ════════════ 通道（Channel）════════════
#
# ★ 为什么要显式建模「通道」（2026-10-03 线上事故）
# ------------------------------------------------
# 中转站对本机与线上的 deepseek-v4.1-flash 请求返回 **HTTP 200 +
# content-type: text/event-stream**，帧里却是 `choices: []`、
# `completion_tokens: 0` —— 它**假装成功**却一个字都不给。
# 后果不是报错，而是更糟的东西：Agent 第一步 LLM 调用拿到空内容，
# 后面的 extract_card / 出图根本到不了，用户界面只表现为
# 「上传了图片，一直生成不了」。
# 同一时刻 premium（gpt-5.5）通道完全正常 —— 说明只是**单条上游通道抽风**。
#
# 这件事给出三条硬规则：
#   ① 空响应必须被识别成「可重试的瞬时故障」，而不是含糊一句
#      「非预期格式」之后就放弃；
#   ② 重试必须能**换通道** —— 同一条坏通道重试一百次还是空的；
#   ③ 绝不能为了「让它有输出」去编造内容 —— 那才是真正的降质。
#      宁可如实告诉用户「上游临时不可用」，也不给一张凭空来的图。


class _Channel(NamedTuple):
    """一条可用的模型通道 = (名字, 接入点, 密钥, 模型)"""
    name: str
    base_url: str
    api_key: str
    model: str


_DEEPSEEK_CH = _Channel("deepseek", DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL)
_PREMIUM_CH = _Channel("premium", PREMIUM_BASE_URL, PREMIUM_API_KEY, PREMIUM_MODEL)
_VISION_CH = _Channel("vision", VISION_BASE_URL, VISION_API_KEY, VISION_MODEL)


def _usable(ch: _Channel) -> bool:
    """这条通道有没有最低限度的配置（缺 model 或缺 key 一律不可用）"""
    return bool(ch.model and ch.api_key and ch.base_url)


def _distinct(a: _Channel, b: _Channel) -> bool:
    """两条通道是否真的不一样 —— 换过去要有意义

    model 与 base_url 都相同却换了条通道，等于「换了等于没换，
    还多花一次调用的钱」。这条判断是防那种自欺欺人的兜底。
    """
    if not _usable(a) or not _usable(b):
        return False
    return a.model != b.model or a.base_url != b.base_url


def _text_channels(premium: bool) -> list[_Channel]:
    """文本通道顺序：主通道在前，备通道在后

    主 = deepseek  → 备 = premium（**更强**，不是降级），默认开启。
    主 = premium   → 备 = deepseek 属**降质**，默认关闭，
                    只有 LLM_FALLBACK_DOWNGRADE=1 时才启用。
    """
    main = _PREMIUM_CH if premium else _DEEPSEEK_CH
    alt = _DEEPSEEK_CH if premium else _PREMIUM_CH
    out = [main]
    if premium:
        if LLM_FALLBACK_DOWNGRADE and _distinct(alt, main):
            out.append(alt)
    elif LLM_FALLBACK_TO_PREMIUM and _distinct(alt, main):
        out.append(alt)
    return [c for c in out if _usable(c)]


def _vision_channels() -> list[_Channel]:
    """视觉通道顺序

    ★ 刻意**不**回落到 deepseek：把图片喂给一个不支持视觉的模型，
      得到的不是「降级结果」而是**幻觉**（实测教训：把天津大学校门
      认成了清华大学，这种错误会直接写进提示词并毁掉出图）。
      看不了图就该如实说看不了，而不是编一份。
    """
    out = [_VISION_CH]
    if _distinct(_PREMIUM_CH, _VISION_CH):
        out.append(_PREMIUM_CH)
    return [c for c in out if _usable(c)]


# ── 客户端缓存 / 测试注入点 ────────────────────────────────
# 每个 (接入点, 密钥, 超时, 重试) 组合复用同一个 client。
# 组合数量有界（本项目只有几条通道 × 几个超时档），不会无限增长。
_CLIENT_CACHE: dict[tuple, Any] = {}

# ★ 测试注入点：None = 用真实 OpenAI；否则 (通道, 超时, 重试) -> 客户端类对象。
#   有了它，下面所有重试 / 换通道的逻辑都能离线被单测覆盖，
#   不必真的去打上游、也不必真的 sleep。
_client_factory = None
_sleep = time.sleep


def _client_for(ch: _Channel, timeout: float, max_retries: int):
    if _client_factory is not None:
        return _client_factory(ch, timeout, max_retries)
    key = (ch.base_url, ch.api_key, timeout, max_retries)
    hit = _CLIENT_CACHE.get(key)
    if hit is None:
        hit = OpenAI(base_url=ch.base_url, api_key=ch.api_key,
                     timeout=timeout, max_retries=max_retries)
        _CLIENT_CACHE[key] = hit
    return hit


# ════════════ 空响应 / 伪流式：识别与抢救 ════════════

EMPTY_UPSTREAM = "EmptyUpstreamResponse"

_SSE_DONE = "[DONE]"


def content_from_sse(raw: str) -> str | None:
    """把「上游误标为 SSE」的报文聚合成纯文本

    兼容三种真实形态（中转站实现各不相同，都在这台机器上出现过）：
      · `data: {"choices":[{"delta":{"content":"你"}}]}`      多帧增量
      · `data: {"choices":[{"message":{"content":"你好"}}]}`  整条消息
      · `data: [DONE]`                                        结束标记
    还有不走 `data:` 前缀、直接一行 JSON 的野路子。

    拼不出任何字符 → 返回 **None**。调用方据此判定「上游空响应」，
    而不是当成「成功但说了句空话」——后者会让用户看到一片空白，
    比看到报错更难排查。
    """
    if not raw:
        return None
    buf: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):        # 空行 / SSE 心跳注释
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
        elif line.startswith(("{", "[")):           # 没有 data: 前缀的裸 JSON
            payload = line
        else:
            continue
        if _SSE_DONE in payload:
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            for choice in (obj.get("choices") or []):
                if not isinstance(choice, dict):
                    continue
                for holder in ("message", "delta"):
                    piece = choice.get(holder)
                    if isinstance(piece, dict) and isinstance(piece.get("content"), str):
                        buf.append(piece["content"])
    return "".join(buf) or None


def _recover_from_exception(e: Exception) -> str | None:
    """异常对象里可能躺着完整内容 —— 值得救一次

    上游把 JSON 谎报成 `text/event-stream` 时，SDK 在**解析阶段**就抛异常，
    但内容其实好好地在异常里。直接抛给用户 = 白跑一次调用 + 白等几十秒。
    """
    candidates: list[str] = []
    body = getattr(e, "body", None)
    if isinstance(body, bytes):
        candidates.append(body.decode("utf-8", "replace"))
    elif isinstance(body, str):
        candidates.append(body)
    elif isinstance(body, (dict, list)):
        candidates.append(json.dumps(body, ensure_ascii=False))
    resp = getattr(e, "response", None)
    txt = getattr(resp, "text", None)
    if isinstance(txt, str) and txt:
        candidates.append(txt)
    for raw in candidates:
        text = content_from_sse(raw)
        if text:
            return text
    return None


def _empty_error(resp: Any) -> LLMError:
    """统一的「上游空响应」异常 —— 前缀 EMPTY_UPSTREAM 让重试逻辑认得出"""
    detail = ""
    if isinstance(resp, str):
        detail = f"，原始报文前 120 字：{resp[:120]!r}"
    return LLMError(
        f"{EMPTY_UPSTREAM}: 上游返回了空的 choices"
        f"（{type(resp).__name__}{detail}，可能是临时故障）")


class _TextMessage:
    """垫片：把「从原始报文里扒出来的文本」包装成一个 message 对象

    存在的唯一理由 —— 让下游（chat / chat_with_tools / vision）
    都能用同一套 `.content` 访问，不必各自再写一遍解析。
    """

    def __init__(self, content: str):
        self.role = "assistant"
        self.content = content
        self.tool_calls = None


def _stringify_content(raw: Any) -> str:
    """content 可能是 None / str / 多模态片段列表 —— 统一取文本"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):                      # [{"type":"text","text":"..."}]
        parts = []
        for piece in raw:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict) and isinstance(piece.get("text"), str):
                parts.append(piece["text"])
        return "".join(parts)
    return str(raw)


def _message_from(resp: Any):
    """从响应对象取第一条 message —— 带全类型守卫

    三道防线，对应三种真实故障形态：
      ① resp 压根不是响应对象（str / dict / None）→ 试着从原始报文里救内容；
      ② 响应对象在，但 choices 为空 → 空响应；
      ③ choice 里有 message/delta，但既无 content 也无 tool_calls → 空响应。
    全部统一抛 LLMError(EMPTY_UPSTREAM…)，由上层按瞬时故障处理。
    """
    choices = resp.get("choices") if isinstance(resp, dict) else getattr(resp, "choices", None)
    if choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message") or first.get("delta")
        else:
            msg = getattr(first, "message", None) or getattr(first, "delta", None)
        if msg is not None:
            return msg
        raise _empty_error(resp)

    # 非标准形态：最后一次机会 —— 从原始报文里抢救内容
    raw = None
    if isinstance(resp, str):
        raw = resp
    elif isinstance(resp, (dict, list)):
        raw = json.dumps(resp, ensure_ascii=False)
    if raw:
        text = content_from_sse(raw)
        if text:
            logger.warning("上游返回非标准格式，已从原始报文里还原 %d 字符内容", len(text))
            return _TextMessage(text)
    raise _empty_error(resp)


def _first_message(resp: Any):
    """向后兼容别名 —— 历史调用点 import 的是这个名字"""
    return _message_from(resp)


def _content_of(msg: Any) -> str:
    if isinstance(msg, dict):
        return _stringify_content(msg.get("content"))
    return _stringify_content(getattr(msg, "content", None))


def _tool_calls_of(msg: Any) -> list:
    if msg is None:
        return []
    if isinstance(msg, dict):
        return msg.get("tool_calls") or []
    return getattr(msg, "tool_calls", None) or []


def _call_parts(tc: Any) -> tuple[str | None, str | None, str]:
    """拆一个 tool_call —— 兼容 dict 与 SDK 对象两种形态"""
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        args_s = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
        return tc.get("id"), fn.get("name"), args_s
    fn = tc.function
    return tc.id, fn.name, fn.arguments


# ★ 瞬时性错误判定（与出图重试同源思路）：中转站上游池波动产生的
#   502/503/504/429 与「上游断流 / no available」都是**立即返回**的错误，
#   退避几秒重试通常就能过。超时（APITimeoutError）不在此列——
#   分析类调用超时重试会把等待拖长三倍，保持快速失败哲学。
_TRANSIENT_HINTS = (
    "502", "503", "504", "429",
    "no available", "upstream stream", "bad gateway",
    "service unavailable", "overloaded",
    # ★ 超时纳入重试（实测 2026-10-03：迭代撞上上游拥堵 60s 超时直接报废，
    #   用户手动重试要重填上下文）。timeout 类的重试次数另有限额，
    #   避免拥堵时用户干等 3×60s。
    "timed out", "apitimeouterror", "request timed out",
    # ★ 空响应（2026-10-03 线上事故）：上游 HTTP 200 但 choices 为空，
    #   这是单条通道抽风的典型症状 —— 换条通道往往立刻就好。
    "emptyupstreamresponse", "空的 choices", "空响应",
)
_TRANSIENT_STATUS = frozenset({429, 502, 503, 504})

# ★ 中转站的「假 400」——必须重试，不能当确定性失败（2026-10-04 用户实测）
#
# 现象：每次会话的**第一次**生成必弹 `BadRequestError: Error code: 400`，
#      原文是中文「您的请求无法……」，用户手动再点一次就好 —— 连续两次，
#       reproducible。随后第二轮请求 26s 一次通过，而**冷请求那一轮要用 65.8s
#       重试 4 次**（前三次分别是 502 / 502 / 503 No available compatible account）。
#
# 结论：这家网关在「还没拉起通道／上游拥堵」时，并不是老老实实返回 5xx，
#      而是甩一个 400 + 中文文案。我们把 400 一律当"请求写错了"就错了：
#      结果是用户的第一次操作永远失败，还会把原始异常文案直接弹到界面上。
#
# 所以这里按**文案**而不是状态码来分诊：命中下列措辞 = 网关忙 = 重试。
_BUSY_400_HINTS = (
    "您的请求无法", "无法完成", "无法处理", "暂时无法",
    "请稍后重试", "稍后再试", "请重试", "请稍后再试",
    "服务繁忙", "系统繁忙", "当前请求过多", "并发过多",
    "upstream", "bad gateway", "gateway", "proxy",
    "try again", "please retry", "later", "temporarily",
    "currently unavailable", "no available", "rate limit",
)

# ★ 内容策略类：这类 400 重试一万次也是同样结果，只会白烧钱。
#   必须单独认出来，给用户一句能改的话说（「换个说法再试」），
#   而不是拿 BugRequestError 原文糊他一脸。
_POLICY_HINTS = (
    "content polic", "moderation", "safety", "unsafe", "blocked by",
    "violat", "sensitive", "nsfw",
    "内容政策", "内容策略", "违规", "违规内容", "敏感内容",
    "安全审核", "审核不通过", "不符合",
)


def looks_like_policy_error(e: Exception) -> bool:
    """命中内容策略？——区别于「重试」，这类要立刻给用户可执行的反馈"""
    s = str(e).lower()
    return any(h in s for h in _POLICY_HINTS)


def is_gateway_busy(e: Exception) -> bool:
    """看起来是 4xx，其实是网关在喊忙 —— 应当重试

    ★ 为什么不干脆「所有 400 都重试」：
      `invalid size value`、`unsupported parameter` 这类是真写错了，
      重试是纯浪费，还会把"改对了参数"的错误诊断拖成"上游抽风"。
      所以只放行明确喊忙/喊上游的措辞。
    """
    if getattr(e, "status_code", None) not in (None, 400, 404, 409, 425, 499):
        return False
    s = str(e).lower()
    return any(h in s for h in _BUSY_400_HINTS)


def _is_transient(e: Exception) -> bool:
    s = str(e).lower()
    if any(h in s for h in _TRANSIENT_HINTS):
        return True
    if is_gateway_busy(e):
        return True
    # ★ 上游返回非预期格式（实测 2026-10-03：生成图片整步报
    #   AttributeError: 'str' object has no attribute 'choices'）——
    #   中转站 5xx/维护时会把错误体原样反序列化成 str。此时必须按
    #   **瞬时错误**重试，而不是把 AttributeError 直接抛给用户。
    if "unexpectedresponse" in s or "非预期格式" in s:
        return True
    return getattr(e, "status_code", None) in _TRANSIENT_STATUS


def _looks_like_timeout(e: Exception) -> bool:
    s = str(e).lower()
    return "timeout" in s or "timed out" in s


# ════════════ 重试计划 ════════════

_PRIMARY_WAITS = (0.0, 2.0, 4.0)
_FALLBACK_WAITS = (0.0, 2.0)


def build_plan(channels: list[_Channel]) -> list[tuple[_Channel, float]]:
    """把「通道列表」展开成 [(通道, 调用前等待秒数), …]

    主通道 3 次（0/2/4s），备通道 2 次（0/2s）——
    备通道本来就是兜底，再排长队会把用户的等待拖成两倍。
    """
    plan: list[tuple[_Channel, float]] = []
    for i, ch in enumerate(channels):
        waits = _PRIMARY_WAITS if i == 0 else _FALLBACK_WAITS
        plan.extend((ch, w) for w in waits)
    return plan


def _run_resilient(attempt, plan, *, label: str, timeout_budget: int = 2):
    """按计划执行；任何一次成功就返回

    ★ 为什么要有这层（统一而非分散）：
      chat / chat_with_tools / vision 三条路径此前各写各的容错，
      结果是一处有重试、另一处裸奔 —— 「工具轮」这一步一次抖动就报废整轮对话。
      统一之后，「什么时候该重试、什么时候必须立刻失败」只有一处判断。

    attempt(channel) -> 结果；出错抛 LLMError。
    timeout_budget：允许几次超时（超时是"最贵"的失败，重试要克制）。

    ★ 连续两次空响应 = 这条通道对本次请求已死，跳过它剩余的排期（2026-10-06 实测）：
      build_plan 会给主通道排 3 次（0/2/4s）。实测小助手那次调用里，
      deepseek 连返 3 次「HTTP 200 但 choices 为空」——烧掉约 12 秒全部落空，
      才轮到备通道，用户干等 30.8 秒。
      注释里早就写着「在同一条坏通道上重试多少次都是空的」，可计划仍排三次。

      ★ 为什么是「两次」而不是「一次就判死」：
        单次空响应确实可能只是抖动，原地重试一次常常就能拿到内容 ——
        这是 `chat_with_tools` 那条既有用例断言过的行为，不能推翻。
        但**连续两次都空**，就不是抖动了，第三次基本注定白等。
        所以：允许原地重试 1 次，第二次仍空即判死，跳过剩余排期直接换通道。
      超时不算（上游忙 ≠ 通道坏，那类重试是有意义的，走 timeout_budget 另算）。
    """
    last: Exception | None = None
    timeout_seen = 0
    dead_channels: set[str] = set()
    empty_streak: dict[str, int] = {}
    for ch, wait in plan:
        # 这条通道已被判死（连续两次空响应）：跳过它剩下的所有排期
        if ch.name in dead_channels:
            continue
        if wait:
            _sleep(wait)
        try:
            return attempt(ch)
        except Exception as e:                        # noqa: PERF203
            last = e
            if _looks_like_timeout(e):
                timeout_seen += 1
                if timeout_seen >= timeout_budget:
                    raise LLMError(f"{type(e).__name__}: {e}") from e
                logger.warning("%s 超时（第 %d 次，重试 1 次）：%s",
                               label, timeout_seen, str(e)[:160])
                continue
            if not _is_transient(e):
                # 鉴权失败 / 请求体非法这类**确定性**错误：重试没有意义，立刻失败。
                # 快速失败也能避免把「key 配错了」包装成「上游抽风」，掩盖真问题。
                raise LLMError(f"{type(e).__name__}: {e}") from e
            if _is_empty_response(e):
                empty_streak[ch.name] = empty_streak.get(ch.name, 0) + 1
                if empty_streak[ch.name] >= 2:
                    dead_channels.add(ch.name)
                    logger.warning("%s 通道 %s 连续 %d 次空响应（判定本次不可用，"
                                   "跳过其剩余排期，直接换通道）：%s",
                                   label, ch.name, empty_streak[ch.name],
                                   str(e)[:120])
                else:
                    logger.warning("%s 通道 %s 空响应（第 1 次，原地重试一次）：%s",
                                   label, ch.name, str(e)[:120])
            else:
                logger.warning("%s 瞬时错误（通道 %s，将重试）：%s",
                               label, ch.name, str(e)[:160])
    raise LLMError(f"{type(last).__name__}: {last}") from last


def _is_empty_response(e: Exception) -> bool:
    """是不是「上游假装成功」——HTTP 200 但内容为空

    这类失败与超时/502 性质完全不同：它不是忙，而是**这条通道此刻给不出内容**，
    在同一条通道上再试多少次都是空的（2026-10-03 已记录过这个教训）。
    判据用类型名 + 文案双保险：异常类型可能来自第三方 SDK，名字不保证统一。
    """
    name = type(e).__name__.lower()
    if "empty" in name:
        return True
    msg = str(e).lower()
    return ("空的 choices" in msg or "empty" in msg and "choice" in msg)


def _chat_once(
    ch: _Channel,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    response_format: dict | None,
    timeout: float | None,
    max_retries: int,
) -> str:
    """走指定通道发一次纯文本请求 —— 失败抛 LLMError（由上层决定重试与否）"""
    assert_ready_for_llm()
    use_timeout = float(timeout or REQUEST_TIMEOUT_SEC)
    try:
        kwargs: dict[str, Any] = {
            "model": ch.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        cli = _client_for(ch, use_timeout, max_retries)
        resp = cli.chat.completions.create(**kwargs)
    except Exception as e:
        # ★ 异常里可能带着完整内容（上游把 JSON 谎报成 SSE，SDK 在解析阶段就抛了）
        saved = _recover_from_exception(e)
        if saved:
            logger.warning("已从上游异常报文中还原 %d 字符内容（通道 %s）",
                           len(saved), ch.name)
            return saved
        raise LLMError(f"{type(e).__name__}: {e}") from e

    text = _content_of(_message_from(resp)).strip()
    if not text:
        raise _empty_error(resp)
    return text


def chat(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 800,
    response_format: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 0,
    premium: bool = False,
) -> str:
    """纯文本对话（不带工具）

    timeout：单次调用超时秒数，默认用全局 REQUEST_TIMEOUT_SEC。
    max_retries：客户端级重试次数，默认 **0**。
    ★ 两个参数的来历（实测教训，见 2026-09-28 日志）：
      模板工坊一次提炼要发多次调用（逐图解构 + 角色 + 合成 + 编译 + 自修轮）。
      之前统一 60s 超时 + SDK 默认 max_retries=2 —— 一次 30s 超时会被 SDK 内部重试两次，
      实际变成 ~90s。实测 6 张图逐图解构耗时 138.8s 且 5 次全失败。
      现在分析类调用单独收紧超时并把重试关掉：出不来就**快速失败走降级**，而不是干等。

    ★ 瞬时错误重试（2026-10-01 实测教训）：工坊一次提炼 8–9 次调用、累计数分钟，
      最后一步撞上中转站 502 就**整轮报废**——5 张图的解构全部白跑。
      现在只对「立即返回」的瞬时错误（502/503/504/429/上游断流）做退避重试。

    ★ 换通道兜底（2026-10-03 实测教训）：deepseek 通道曾返回 HTTP 200 但
      choices 为空（"假装成功"），同站 premium 通道正常。此时在同一条坏通道上
      重试多少次都是空的 —— 所以计划里包含备用通道（见 _text_channels）。
    """
    channels = _text_channels(premium)
    if not channels:
        raise LLMError("没有可用的文本模型通道（请检查 DEEPSEEK_* 配置）")

    def attempt(ch: _Channel) -> str:
        return _chat_once(ch, messages, temperature, max_tokens,
                          response_format, timeout, max_retries)

    return _run_resilient(attempt, build_plan(channels), label="LLM")


# ── 交互式问答（小助手）的等待预算 ───────────────────────────
#   与收尾文案同源思路：用户在屏幕前等着，档位必须按「人等得住」来定，
#   不能套批量流水线的容错。
#   ★ 两个数字必须满足：3 × 单次 + 1s 退避 ≤ 总预算
#     （最坏情况 = 主通道 2 次 + 兜底 1 次）。默认 3×18+1 = 55s ≤ 60s。
#     改任一参数都要保证这个式子成立 —— test_llm_resilience 里有对应断言。
HELPER_ATTEMPT_TIMEOUT_SEC = int(os.getenv("HELPER_ATTEMPT_TIMEOUT_SEC", "18"))
HELPER_TOTAL_BUDGET_SEC = int(os.getenv("HELPER_TOTAL_BUDGET_SEC", "60"))
# 小助手看图：带图比纯文本慢，给纯文本的近两倍；但同样不套全局 180s
HELPER_VISION_TIMEOUT_SEC = int(os.getenv("HELPER_VISION_TIMEOUT_SEC", "30"))


def chat_interactive(
    messages: list[dict],
    *,
    temperature: float = 0.4,
    max_tokens: int = 500,
    response_format: dict | None = None,
) -> str:
    """**交互式问答专用**（小助手）：总预算封顶，且**空响应立刻换通道**。

    ★ 为什么不能直接用 chat()（2026-10-06 实测，用户反馈「小助手回复慢」）：
      chat() 走 build_plan 完整档位，而全局 REQUEST_TIMEOUT_SEC=180s ——
      主通道 3 次（0/2/4s）+ 备通道 2 次（0/2s），单次可等 180s，
      最坏理论值超过 9 分钟。那是给「模板工坊跑 8–9 次调用、失败就整轮报废」
      设计的，**用在问答上就是把批量的容错套到等人身上**。

      实测那一次 30.8s 的构成：deepseek 连返 3 次空 choices（17→22→29s），
      三次全落空后才轮到 gpt-5.5 —— 光重试就白烧 12 秒。

    现在：
      · 单次 18s（问答不需要 180s 的耐心），总预算 60s 硬闸
      · 保留备通道兜底 —— deepseek 空响应是常态，没有兜底小助手会直接不可用
      · 配合 _run_resilient 的「连续空响应即换通道」，不再在同一条死通道上排队
    失败由调用方给出「稍等再问」这类可执行的回话，不让用户干等。
    """
    channels = _text_channels(False)
    if not channels:
        raise LLMError("没有可用的文本模型通道（请检查 DEEPSEEK_* 配置）")

    def attempt(ch: _Channel) -> str:
        return _chat_once(ch, messages, temperature, max_tokens,
                          response_format, HELPER_ATTEMPT_TIMEOUT_SEC, 0)

    plan = _interactive_plan(channels)
    # 计划本身已按预算排好，超时不需要额外的「克制」闸 —— 让它排完即可
    return _run_resilient(attempt, plan, label="小助手问答",
                          timeout_budget=len(plan))


def _interactive_plan(channels: list[_Channel]) -> list[tuple[_Channel, float]]:
    """交互式问答的排期：主通道尽量多试，**但兜底必须留一次**

    ★ 这里有个必须想清楚的取舍（第一版就在这里翻过车）：
      按预算从前往后排，主通道试 2 次就把 60s 吃满了，兜底通道**一次都排不进去** ——
      等于「保留了备通道」是句空话，主通道一死小助手就整个不可用。

    所以：**先给兜底预留一次**（reserve），剩下的预算才归主通道。
      有兜底 → 主 2 次（0/1s）+ 兜底 1 次，最坏 3×18+1 = 55s
      无兜底 → 预算全归主通道，可排满 3 次（0/1/2s），最坏 3×18+3 = 57s
    """
    primary, *fallbacks = channels
    reserve = HELPER_ATTEMPT_TIMEOUT_SEC if fallbacks else 0
    plan: list[tuple[_Channel, float]] = []
    elapsed = 0.0
    for w in (0.0, 1.0, 2.0):
        if elapsed + w + HELPER_ATTEMPT_TIMEOUT_SEC + reserve > HELPER_TOTAL_BUDGET_SEC:
            break
        plan.append((primary, w))
        elapsed += w + HELPER_ATTEMPT_TIMEOUT_SEC
    for ch in fallbacks:
        plan.append((ch, 0.0))       # 兜底不再受预算砍：它就是底线
    if not plan:                     # 预算小到一次都排不下 —— 那也要试一次
        plan = [(primary, 0.0)]
    return plan


def vision(
    messages: list[dict],
    *,
    max_tokens: int = 800,
    temperature: float = 0.3,
    response_format: dict | None = None,
    timeout: float | None = None,
) -> str:
    """视觉模型调用（看图）—— 走与文本通道同一套容错

    ★ 为什么要统一到这里（审查 + 事故双重理由）：
      此前 card_extractor / style_forge / routers.helper 各自 `new OpenAI(...)`，
      三份独立拼装的客户端 = 三种各不相同的失败模样，而且**一处都没有重试**。
      抽到这里之后，「看图失败了怎么办」只有一处说法、一处可以改。

    失败一律抛 LLMError；调用方（都已经有 try/except）据此走各自的降级路径。
    """
    channels = _vision_channels()
    if not channels:
        raise LLMError("没有可用的视觉模型通道（请检查 VISION_MODEL 配置）")
    use_timeout = float(timeout or REQUEST_TIMEOUT_SEC)

    def attempt(ch: _Channel) -> str:
        try:
            cli = _client_for(ch, use_timeout, 0)
            kwargs: dict[str, Any] = {
                "model": ch.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format
            resp = cli.chat.completions.create(**kwargs)
        except Exception as e:
            saved = _recover_from_exception(e)
            if saved:
                logger.warning("已从视觉模型异常报文中还原 %d 字符内容", len(saved))
                return saved
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = _content_of(_message_from(resp)).strip()
        if not text:
            raise _empty_error(resp)
        return text

    return _run_resilient(attempt, build_plan(channels), label="视觉模型")


def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    tool_choice: str = "auto",
    temperature: float = 0.2,
) -> dict:
    """带工具的一轮对话

    返回 {"content", "tool_calls": [{"id","name","arguments"}], "usage", "finish_reason"}

    ★ 重试是 2026-10-03 补上的 —— 这是整个故障里最要命的一处：
      Agent 主循环每一步都走这里，而它此前**完全没有任何容错**：
      上游一次抖动（502 / 空 choices）就在第 1 步把整轮对话打成 llm_error，
      用户看到的是「上传图片后什么都没发生」。
      现在它和 chat() 共用同一套「重试 + 换通道」计划。
    """
    channels = _text_channels(False)
    if not channels:
        raise LLMError("没有可用的文本模型通道（请检查 DEEPSEEK_* 配置）")

    def attempt(ch: _Channel) -> dict:
        try:
            cli = _client_for(ch, REQUEST_TIMEOUT_SEC, 0)
            resp = cli.chat.completions.create(
                model=ch.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
            )
        except Exception as e:
            saved = _recover_from_exception(e)
            if saved:
                logger.warning("工具轮已从异常报文还原 %d 字符内容（通道 %s）",
                               len(saved), ch.name)
                return {"content": saved, "tool_calls": [], "usage": {},
                        "finish_reason": "stop", "_recovered": True}
            raise LLMError(f"{type(e).__name__}: {e}") from e

        msg = _message_from(resp)
        content = _content_of(msg)
        raw_calls = _tool_calls_of(msg)
        # ★ 既没文字又没工具调用 = 这一轮白跑了。当成空响应重试，
        #   而不是让上层拿到一个空 dict 去继续（那样 Agent 会卡在
        #   「模型沉默」的状态里，前端无从判断是慢还是坏了）。
        if not content.strip() and not raw_calls:
            raise _empty_error(resp)

        calls: list[dict] = []
        for tc in raw_calls:
            _id, name, args = _call_parts(tc)
            parsed = extract_json(args) if (args or "").strip() else None
            if parsed is None:
                parsed = {}          # 模型输出了无法解析的 arguments
            calls.append({"id": _id, "name": name, "arguments": parsed})

        usage: dict[str, Any] = {}
        u = getattr(resp, "usage", None) or (resp.get("usage") if isinstance(resp, dict) else None)
        if u:
            usage = {
                "prompt_tokens": u.get("prompt_tokens") if isinstance(u, dict)
                else getattr(u, "prompt_tokens", None),
                "completion_tokens": u.get("completion_tokens") if isinstance(u, dict)
                else getattr(u, "completion_tokens", None),
                "total_tokens": u.get("total_tokens") if isinstance(u, dict)
                else getattr(u, "total_tokens", None),
                "cached_tokens": None,
            }
            details = (u.get("prompt_tokens_details") if isinstance(u, dict)
                       else getattr(u, "prompt_tokens_details", None))
            if isinstance(details, dict):
                usage["cached_tokens"] = details.get("cached_tokens")
            elif details is not None:
                usage["cached_tokens"] = getattr(details, "cached_tokens", None)

        finish = None
        choices = resp.get("choices") if isinstance(resp, dict) else getattr(resp, "choices", None)
        if choices:
            first = choices[0]
            finish = (first.get("finish_reason") if isinstance(first, dict)
                      else getattr(first, "finish_reason", None))

        return {
            "content": content,
            "tool_calls": calls,
            "usage": usage,
            "finish_reason": finish,
        }

    return _run_resilient(attempt, build_plan(channels), label="工具轮 LLM")


# ── 收尾文案的「等待预算」三件套（2026-10-06）────────────────────────
# 用户口径：**文案必须拿得到**，但**全程不许超过 1 分钟**。
#   三个参数缺一不可 ——
#     ATTEMPT  单次请求上限：比主链路 60s 短，超时早退才能留出重试余地
#     TOTAL    总预算硬闸：不论重试几次，超过就立刻放弃（这是 1 分钟红线的执行者）
#     PLAN     尝试次数 × 退避：给上游真实抖动留机会，而不是一撞就放弃
FINAL_SUMMARY_ATTEMPT_TIMEOUT_SEC = int(os.getenv("FINAL_SUMMARY_ATTEMPT_TIMEOUT_SEC", "25"))
FINAL_SUMMARY_TOTAL_BUDGET_SEC = int(os.getenv("FINAL_SUMMARY_TOTAL_BUDGET_SEC", "55"))
_FINAL_SUMMARY_WAITS = (0.0, 1.5, 3.0)      # 3 次：0s / 1.5s / 3s（合计退避 4.5s）


def summarize_for_final(messages: list[dict]) -> dict:
    """**收尾总结专用**：不换通道、总预算封顶，但**给足 3 次机会**。

    ★ 为什么要单独开一条（2026-10-06，用户实测「收尾文案 2 分钟」）：
      收尾（出图成功后模型写一段说明）此前直接复用 `chat_with_tools`，
      于是继承了 Agent 主循环的**完整容错档位**：

        主通道 60s × timeout_budget=2 + 备通道(premium) 60s + 退避 6s ≈ 2 分 12 秒

      但这一步的产物只是**一段说明文字** —— 图早就生成好了，用户已拿到成品。
      为一段锦上添花的文案付两分钟，是把「主链路的容错」用错了地方。

      对照 `secretary()` 早就想明白了（「秘书不该让人等」）——收尾是同一类，漏了。

    ★ 参数怎么定的（用户明确要求：保证拿到文案，且总时长 < 1 分钟）：
      不是简单砍到「一撞就放弃」—— 那样上游轻微抖动时文案直接没了，体验更差。
      而是**在 1 分钟预算内尽量多给机会**：
        3 次尝试（0s / 1.5s / 3s 退避），单次 25s，
        总预算 55s 硬闸 —— 最坏 25+1.5+25 = 51.5s，留几秒余量。
      典型情况（上游正常）首次即中，约 3–8s，与之前无感。

    ★ 不换通道：备通道是 premium（更贵更强），收尾文案用不上；
      出图成功那一刻用户要的是「马上看到说明」，不是「更强的一版说明」。

    ★ 失败一律不抛给用户：调用方 loop.py 有兜底文案（图已生成，不该因文案失败而报整轮失败）。
    """
    ch = _DEEPSEEK_CH
    if not _usable(ch):
        raise LLMError("没有可用的文本模型通道（请检查 DEEPSEEK_* 配置）")

    def attempt(c: _Channel) -> dict:
        try:
            cli = _client_for(c, FINAL_SUMMARY_ATTEMPT_TIMEOUT_SEC, 0)
            resp = cli.chat.completions.create(
                model=c.model,
                messages=messages,
                tools=[],
                tool_choice="none",
                temperature=0.2,
            )
        except Exception as e:
            saved = _recover_from_exception(e)
            if saved:
                return {"content": saved, "tool_calls": [], "usage": {},
                        "finish_reason": "stop", "_recovered": True}
            raise LLMError(f"{type(e).__name__}: {e}") from e

        msg = _message_from(resp)
        content = _content_of(msg)
        if not content.strip():
            raise _empty_error(resp)
        return {"content": content, "tool_calls": [], "usage": {},
                "finish_reason": "stop"}

    # 计划按总预算截断：万一配置被人调大，也不会突破 1 分钟红线
    plan: list[tuple[_Channel, float]] = []
    elapsed = 0.0
    for w in _FINAL_SUMMARY_WAITS:
        if elapsed + FINAL_SUMMARY_ATTEMPT_TIMEOUT_SEC > FINAL_SUMMARY_TOTAL_BUDGET_SEC:
            logger.warning("收尾总结预算已用尽（%ds/%ds），剩余机会跳过",
                           int(elapsed), FINAL_SUMMARY_TOTAL_BUDGET_SEC)
            break
        plan.append((ch, w))
        elapsed += w + FINAL_SUMMARY_ATTEMPT_TIMEOUT_SEC
    if not plan:                      # 预算被配得极小：至少给一次机会
        plan = [(ch, 0.0)]

    # timeout_budget 宽松（等于尝试次数）：超时也允许再试 ——
    #   与主链路「超时不重试」相反，因为这里有总预算硬闸兜着，
    #   多试一次的代价可控，而放弃的代价是用户拿不到文案。
    return _run_resilient(attempt, plan, label="收尾总结",
                          timeout_budget=len(plan))
    return _run_resilient(attempt, plan, label="收尾总结", timeout_budget=1)


def secretary(messages: list[dict], max_tokens: int = 300) -> str:
    """小模型秘书：复用主接入点 + 关掉思考

    实测 reasoning_effort=none 时 reasoning_tokens=0，响应快成本低。
    用于摘要压缩、偏好提取这类不需要推理的活（方案 §7）。

    ★ 重试但**不换通道**：秘书干的是「锦上添花」的记忆压缩，
      为了它去调更贵的 premium 是拿高射炮打蚊子。失败了跳过即可
      （调用方 engine/loop.py 已经把它包在 try 里）。
    """
    assert_ready_for_llm()

    def attempt(ch: _Channel) -> str:
        try:
            cli = _client_for(ch, REQUEST_TIMEOUT_SEC, 0)
            resp = cli.chat.completions.create(
                model=ch.model,
                messages=messages,
                temperature=0.2,
                max_tokens=max_tokens,
                extra_body={"reasoning_effort": "none"},
            )
        except Exception as e:
            saved = _recover_from_exception(e)
            if saved:
                return saved
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = _content_of(_message_from(resp)).strip()
        if not text:
            raise _empty_error(resp)
        return text

    # 只有主通道、只退避一次、超时不重试（秘书不该让人等）
    plan = [(_DEEPSEEK_CH, 0.0), (_DEEPSEEK_CH, 2.0)]
    return _run_resilient(attempt, plan, label="秘书模型", timeout_budget=1)


# 【预留】向后兼容别名 —— 真正的来源是 config.MAX_AGENT_STEPS。
# 留着只是防止外部脚本 import 时报错。
MAX_STEPS = MAX_AGENT_STEPS
