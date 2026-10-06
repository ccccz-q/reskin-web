"""LLM 通道容错验收 —— 钉住 2026-10-03 线上事故的解法

    python tests/test_llm_resilience.py

★★ 这个文件为什么存在
----------------------
线上事故：用户上传图片后**什么都生成不出来**。追下去是两层故障叠加：

  第一层  中转站对 deepseek 通道返回 **HTTP 200 + 空的 choices**
          （content-type: text/event-stream，completion_tokens=0）——
          它假装成功，却一个字都不给。Agent 第一步 LLM 调用就落空，
          后面的 extract_card / 出图根本到不了。同一时刻 premium 通道正常。
  第二层  云端反向代理对每个 HTTP 请求有 60 秒硬超时（见 test_async_chat.py）。

本文件覆盖**第一层**的解法（services/llm.py 的通道容错），全部离线：
用假的客户端替换真实 OpenAI，绝不打上游、绝不花钱、绝不 sleep。

钉住的行为：
  1. SSE 聚合：能把「谎报成流式的报文」还原成文本；拼不出内容必须报 None
  2. 空响应识别：choices=[] / 救不回内容的非对象形态 → 统一 EMPTY_UPSTREAM 标记
  3. 异常报文抢救：SDK 解析失败但内容在异常里 → 直接拿到内容，不浪费一次调用
  4. 换通道：同一条坏通道重试到底也没用，必须走到备用通道
  5. 不换通道的场合：确定性错误（401/400）立刻失败，不重试也不兜底
  6. 工具轮 / 秘书 / 视觉三条路径都有重试（此前一处都没有）
  7. 空响应不能再含糊地描述成「非预期格式」就丢给用户 ——
     它必须带 EMPTY_UPSTREAM 标记并被 _is_transient 认出来，才会触发重试换通道
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# ★ 必须在 import services.llm 之前定死通道配置（2026-10-05 开源整理时实测）：
#   本文件全部离线、用假客户端替换真实 OpenAI，通道有没有配 key 不该影响结果。
#   此前它直接读环境：作者机器上 .env 有 key → 绿；开源用户没配 key → 整份红。
#   这类"红灯与被测代码无关"的失败最消耗人，必须在测试里自己钉死。
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-dummy")
os.environ.setdefault("DEEPSEEK_BASE_URL", "https://example.invalid/v1")
os.environ.setdefault("DEEPSEEK_MODEL", "test-model")
os.environ.setdefault("PREMIUM_API_KEY", "sk-test-dummy")
os.environ.setdefault("PREMIUM_BASE_URL", "https://example.invalid/v1")
os.environ.setdefault("PREMIUM_MODEL", "test-model")
os.environ.setdefault("VISION_API_KEY", "sk-test-dummy")
os.environ.setdefault("VISION_BASE_URL", "https://example.invalid/v1")
os.environ.setdefault("VISION_MODEL", "test-model")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import llm  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


# ══════════ 测试替身 ══════════

class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.role = "assistant"


class FakeChoice:
    def __init__(self, message=None, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, cid, name, arguments):
        self.id = cid
        self.function = FakeFunction(name, arguments)


class Boom(Exception):
    """模拟 SDK 抛出的上游错误 —— 可携带原始报文，便于测「抢救」路径"""

    def __init__(self, msg, body=None, status_code=None):
        super().__init__(msg)
        self.body = body
        self.status_code = status_code


class Recorder:
    """记录每次调用走了哪条通道，便于断言「是否换过通道」"""

    def __init__(self, script: dict):
        self.script = script          # {channel_name: [结果或异常, ...]}
        self.seen: list[str] = []
        self.waits: list[float] = []

    def handler(self, ch, _timeout, _max_retries):
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.seen.append(ch.name)
                queue = outer.script.get(ch.name) or []
                item = queue.pop(0) if queue else None
                if isinstance(item, BaseException):
                    raise item
                return item

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


def install(script: dict) -> Recorder:
    """把假客户端装进 llm 模块；返回 Recorder 以便断言"""
    rec = Recorder(script)
    llm._client_factory = rec.handler
    llm._sleep = lambda s: rec.waits.append(s)
    return rec


def uninstall():
    llm._client_factory = None
    llm._sleep = lambda s: None


CH_DS = llm._Channel("deepseek", "http://x", "k", "ds-model")
CH_PM = llm._Channel("premium", "http://x", "k", "gpt-5.5")
CH_VS = llm._Channel("vision", "http://x", "k", "vision-model")


def _patch_channels(text_dir=None, vision_dir=None):
    """替换模块里的通道常量，并返回一个还原函数"""
    saved = (llm._DEEPSEEK_CH, llm._PREMIUM_CH, llm._VISION_CH,
             llm.LLM_FALLBACK_TO_PREMIUM, llm.LLM_FALLBACK_DOWNGRADE)
    llm._DEEPSEEK_CH, llm._PREMIUM_CH = CH_DS, CH_PM
    if vision_dir:
        llm._VISION_CH = CH_VS
    llm.LLM_FALLBACK_TO_PREMIUM = True
    llm.LLM_FALLBACK_DOWNGRADE = False

    def restore():
        (llm._DEEPSEEK_CH, llm._PREMIUM_CH, llm._VISION_CH,
         llm.LLM_FALLBACK_TO_PREMIUM, llm.LLM_FALLBACK_DOWNGRADE) = saved
    return restore


print("=== 1. SSE 报文聚合（上游谎报流式）===")
# 这是事故现场的真实报文形状：HTTP 200、content-type 为 event-stream，
# 但帧里 choices 是空的 —— 「假装成功」。
empty_stream = (
    'data: {"id":"","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4.1-flash","choices":[],"completion_tokens":0}\n\n'
    'data: [DONE]\n\n'
)
check("空 choices → None（不能被当成成功）", llm.content_from_sse(empty_stream) is None)

multi = '\n\n'.join([
    'data: {"choices":[{"delta":{"content":"你"}}]}',
    'data: {"choices":[{"delta":{"content":"好"}}]}',
    'data: {"choices":[{"delta":{"content":"！"}}]}',
    'data: [DONE]',
])
check("多帧增量能拼回来", llm.content_from_sse(multi) == "你好！")

whole = 'data: {"choices":[{"message":{"content":"整条消息"}}]}\n\ndata: [DONE]\n\n'
check("整条 message 形态也能取", llm.content_from_sse(whole) == "整条消息")

check("[DONE] 之后的帧不拼接",
      llm.content_from_sse('data: {"choices":[{"delta":{"content":"前"}}]}\n\n'
                           'data: [DONE]\n\n'
                           'data: {"choices":[{"delta":{"content":"后"}}]}\n\n') == "前")
check("SSE 心跳注释被跳过",
      llm.content_from_sse(': ping\n\ndata: {"choices":[{"delta":{"content":"p"}}]}\n\n') == "p")
check("没有 data: 前缀的裸 JSON 行也能取",
      llm.content_from_sse('{"choices":[{"message":{"content":"裸"}}]}') == "裸")
check("坏 JSON 行不炸", llm.content_from_sse('data: {broken json') is None)
check("空串 → None", llm.content_from_sse("") is None)


print()
print("=== 2. 空响应识别（不再用「非预期格式」含糊过去）===")
try:
    llm._message_from(FakeResponse(choices=[]))
    check("空 choices 抛异常", False)
except llm.LLMError as e:
    check("空 choices 抛 LLMError", True)
    check("错误带 EMPTY_UPSTREAM 标记", llm.EMPTY_UPSTREAM in str(e), str(e)[:60])
    check("空响应被判为瞬时错误（才会重试）", llm._is_transient(e))

# message 存在时 _message_from 只负责「取」，内容是否为空由调用层判定
# （因为工具轮的 message 本来就可能 content 为空、只有 tool_calls）
_msg = llm._message_from(FakeResponse(choices=[FakeChoice(FakeMessage(content="   "))]))
check("message 存在时正常返回", isinstance(_msg, FakeMessage))
check("内容为空由调用层判定", llm._content_of(_msg).strip() == "")

# str / dict / None 三种非对象形态：能救就救，救不了报空
check("str 形态还原成功",
      llm._content_of(llm._message_from(multi)) == "你好！")
check("dict 形态（choices 为 dict 列表）也能取",
      llm._content_of(llm._message_from(
          {"choices": [{"message": {"content": "来自字典"}}]})) == "来自字典")
try:
    llm._message_from("totally-not-a-response")
    check("救不回来时抛异常", False)
except llm.LLMError as e:
    check("救不回来抛 LLMError", True)
    check("原文被截断附在错误里便于排查", "totally-not-a-response" in str(e))

try:
    llm._message_from(None)
    check("None 抛异常", False)
except llm.LLMError:
    check("None 抛 LLMError", True)

try:
    llm._first_message(FakeResponse(choices=[]))
    check("旧别名 _first_message 仍然可用", False)
except llm.LLMError:
    check("旧别名 _first_message 仍然可用（历史调用点不破）", True)


print()
print("=== 3. 从异常报文里抢救内容（不浪费一次已经付费的调用）===")
saved = llm._recover_from_exception(Boom("JSON decode failed", body=multi))
check("body 里的流式内容被还原", saved == "你好！", repr(saved))
check("body 是 dict 时也不炸", llm._recover_from_exception(Boom("x", body={"a": 1})) is None)
check("没有可用报文时返回 None", llm._recover_from_exception(Boom("x")) is None)
check("body 是 bytes 时能解码",
      llm._recover_from_exception(Boom("x", body=multi.encode("utf-8"))) == "你好！")


print()
print("=== 4. chat()：坏通道耗尽后自动换到 premium ===")
restore = _patch_channels()
try:
    good = FakeResponse(choices=[FakeChoice(FakeMessage(content="来自 premium"))])
    rec = install({
        "deepseek": [llm.LLMError(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices"),
                     llm.LLMError(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices"),
                     llm.LLMError(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices")],
        "premium": [good],
    })
    out = llm.chat([{"role": "user", "content": "hi"}])
    check("换通道后成功拿到内容", out == "来自 premium", out)
    # ★ 2026-10-06 行为变更：主通道原本会排满 3 次（0/2/4s），
    #   实测小助手那次调用 deepseek 连返 3 次空 choices，白烧约 12s 才轮到备通道。
    #   现在改成「连续两次空即判该通道本次已死」，跳过第 3 次排期直接换通道。
    check("主通道只试 2 次即判死（不再排满 3 次）",
          rec.seen.count("deepseek") == 2, str(rec.seen))
    check("最后落到备用通道", rec.seen[-1] == "premium", str(rec.seen))
    # 计划：主通道 0/2/4s，备用通道 0/2s。
    #   第 1 次不退避（0.0 不记录）；第 2 次退避 2.0 → 空 → 判死，跳过第 3 次的 4.0；
    #   备通道首次不退避（0.0 不记录）即成功。故只累积 [2.0]。
    check("重试之间有退避等待", rec.waits == [2.0], str(rec.waits))
    uninstall()
finally:
    restore()

print()
print("=== 5. chat()：确定性错误立刻失败，不重试也不兜底 ===")
restore = _patch_channels()
try:
    rec = install({"deepseek": [Boom("401 unauthorized", status_code=401)],
                   "premium": [FakeResponse(choices=[FakeChoice(FakeMessage(content="X"))])]})
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        check("401 应该抛错", False)
    except llm.LLMError as e:
        check("401 抛出 LLMError", True)
        check("401 被识别为非瞬时", "401" in str(e), str(e)[:60])
    check("401 不重试（只调用 1 次）", len(rec.seen) == 1, str(rec.seen))
    check("401 不兜底到 premium", rec.seen == ["deepseek"], str(rec.seen))
    uninstall()
finally:
    restore()

print()
print("=== 6. chat()：超时重试次数受控，不会让用户干等 3×60s ===")
restore = _patch_channels()
try:
    rec = install({"deepseek": [Boom("APITimeoutError: Request timed out.")] * 3,
                   "premium": [FakeResponse(choices=[FakeChoice(FakeMessage(content="迟到的回答"))])]})
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        check("连续超时应最终失败", False)
    except llm.LLMError as e:
        check("连续超时最终抛错", True, str(e)[:50])
    # ★ 超时是"最贵"的失败：每次都可能耗掉整个 60s 超时窗口。
    #   所以策略是主通道最多试 2 次就放弃（约 2 分钟），而不是横跨所有通道试满 5 次。
    check("超时最多在主通道试 2 次", rec.seen.count("deepseek") == 2, str(rec.seen))
    check("超时不转到备用通道（避免等待翻倍）",
          "premium" not in rec.seen, str(rec.seen))
    uninstall()
finally:
    restore()

print()
print("=== 7. chat_with_tools()：工具轮也要有重试（这是事故里最要命的一处）===")
restore = _patch_channels()
try:
    tool_resp = FakeResponse(
        choices=[FakeChoice(FakeMessage(
            content=None,
            tool_calls=[FakeToolCall("c1", "extract_card", '{"a": 1}')]))],
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=2,
                                    total_tokens=12, prompt_tokens_details=None),
    )
    rec = install({
        "deepseek": [llm.LLMError(f"{llm.EMPTY_UPSTREAM}: 空的 choices"), tool_resp],
        "premium": [],
    })
    got = llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    check("第二次尝试成功", got["tool_calls"][0]["name"] == "extract_card", str(got)[:80])
    check("工具参数被解析成 dict", got["tool_calls"][0]["arguments"] == {"a": 1})
    check("usage 透传", got["usage"].get("total_tokens") == 12, str(got["usage"]))
    check("finish_reason 透传", got["finish_reason"] == "stop")
    uninstall()

    # 既没文字又没工具调用 = 这一轮白跑 → 必须重试而不是返回空
    rec = install({
        "deepseek": [FakeResponse(choices=[FakeChoice(FakeMessage(content=""))]),
                     tool_resp],
        "premium": [],
    })
    got = llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    check("空一轮会被重试", len(got["tool_calls"]) == 1, str(rec.seen))
    uninstall()
finally:
    restore()

print()
print("=== 8. 工具轮：换通道兜底 ===")
restore = _patch_channels()
try:
    good_call = FakeResponse(choices=[FakeChoice(FakeMessage(
        content=None, tool_calls=[FakeToolCall("c9", "render_preview", '{"b": 2}')]))])
    rec = install({"deepseek": [Boom("502 Bad Gateway")] * 3,
                   "premium": [good_call]})
    got = llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    check("502 后换到 premium 成功", got["tool_calls"][0]["name"] == "render_preview")
    check("走到了备用通道", "premium" in rec.seen, str(rec.seen))
    uninstall()
finally:
    restore()

print()
print("=== 9. vision()：看图也走同一套容错 ===")
restore = _patch_channels(vision_dir=True)
try:
    rec = install({"vision": [FakeResponse(choices=[FakeChoice(FakeMessage(content="  "))]),
                              FakeResponse(choices=[FakeChoice(FakeMessage(content='{"ok":1}'))])]})
    out = llm.vision([{"role": "user", "content": "看图"}])
    check("空内容被重试而非返回", out == '{"ok":1}', repr(out))
    check("重试次数有上限", len(rec.seen) == 2, str(rec.seen))
    uninstall()

    # 视觉通道刻意不回落到 deepseek（把图喂给不支持视觉的模型 = 幻觉）
    check("视觉通道不含 deepseek",
          all(c.name != "deepseek" for c in llm._vision_channels()),
          str([c.name for c in llm._vision_channels()]))
finally:
    restore()

print()
print("=== 10. secretary()：重试但不换贵通道 ===")
restore = _patch_channels()
try:
    rec = install({"deepseek": [Boom("503 no available channel"),
                                FakeResponse(choices=[FakeChoice(FakeMessage(content="摘要"))])],
                   "premium": []})
    out = llm.secretary([{"role": "user", "content": "压缩"}])
    check("秘书瞬时错误会重试", out == "摘要", out)
    check("秘书不跨到 premium", "premium" not in rec.seen, str(rec.seen))
    uninstall()

    rec = install({"deepseek": [Boom("Request timed out.")] * 2, "premium": []})
    try:
        llm.secretary([{"role": "user", "content": "压缩"}])
        check("秘书超时应该失败", False)
    except llm.LLMError:
        check("秘书超时快速失败（不让人等）", True)
    check("秘书超时只试 1 次", len(rec.seen) == 1, str(rec.seen))
    uninstall()
finally:
    restore()

print()
print("=== 11. 通道去重与开关 ===")
restore = _patch_channels()
try:
    same = llm._Channel("premium-copy", CH_DS.base_url, "k", CH_DS.model)
    check("model 与 base 都相同 → 不算另一条通道", llm._distinct(same, CH_DS) is False)
    check("model 不同 → 算另一条通道", llm._distinct(CH_PM, CH_DS) is True)
    check("缺 model 的通道不可用", llm._usable(llm._Channel("x", "http://x", "k", "")) is False)

    check("主通道优先在计划首位", llm.build_plan([CH_DS, CH_PM])[0][0] is CH_DS)
    plan = llm.build_plan([CH_DS, CH_PM])
    check("主通道 3 次 + 备用 2 次", len(plan) == 5, str(len(plan)))
    check("备用通道首次重试不等待", plan[3][1] == 0.0, str([p[1] for p in plan]))

    # 降质兜底默认关闭
    llm.LLM_FALLBACK_DOWNGRADE = False
    check("premium 主 → 默认不回落到 deepseek",
          [c.name for c in llm._text_channels(True)] == ["premium"])
    llm.LLM_FALLBACK_DOWNGRADE = True
    check("开启 LLM_FALLBACK_DOWNGRADE 后才回落",
          [c.name for c in llm._text_channels(True)] == ["premium", "deepseek"])
    llm.LLM_FALLBACK_TO_PREMIUM = False
    check("关掉开关后不换通道",
          [c.name for c in llm._text_channels(False)] == ["deepseek"])
finally:
    restore()

print()
print("=== 12. 异常形态兜底（历史上踩过的坑）===")
check("content 是多模态片段列表时能取文本",
      llm._stringify_content([{"type": "text", "text": "片段A"}, {"type": "text", "text": "片段B"}])
      == "片段A片段B")
check("片段列表里混非文本节点时跳过",
      llm._stringify_content([{"type": "image_url", "image_url": {"url": "x"}},
                              {"type": "text", "text": "只有我"}]) == "只有我")
check("content 为 None → 空串", llm._stringify_content(None) == "")
cid, name, args = llm._call_parts(FakeToolCall("i", "n", '{"k":1}'))
check("_call_parts 拆 SDK 对象", (cid, name, args) == ("i", "n", '{"k":1}'))
cid, name, args = llm._call_parts({"id": "i2", "function": {"name": "n2", "arguments": '{"z":9}'}})
check("_call_parts 拆 dict 形态", (cid, name, args) == ("i2", "n2", '{"z":9}'))
cid, name, args = llm._call_parts({"id": "i3", "function": {"name": "n3", "arguments": {"a": 1}}})
check("dict 形态的 arguments 被序列化", args == '{"a": 1}', args)

print()
print("=== 13. 截断 JSON 抢救（VLM 提炼 card 的实际翻车现场）===")
truncated = ('{"subject":{"name":"深蓝圆环"},"anchors":[{"desc":"居中的深蓝圆环",'
             '"materializable":true},{"desc":"斜向细密线条","materializable":true},{')
check("严格解析判失败（旧行为：整段丢弃）", llm.extract_json(truncated) is None)
salvaged = llm.extract_json_lenient(truncated)
check("截断被补回来", isinstance(salvaged, dict), str(salvaged)[:80])
check("保住了 subject", (salvaged or {}).get("subject", {}).get("name") == "深蓝圆环")
check("保住了 2 个完整锚点", len((salvaged or {}).get("anchors") or []) == 2,
      str(len((salvaged or {}).get("anchors") or [])))

check("完整 JSON 走严格路径不变",
      llm.extract_json_lenient('{"a":1}') == {"a": 1})
check("截断在 key 上时保住前面部分",
      llm.extract_json_lenient('{"a":1,"b":[],"risk') == {"a": 1, "b": []})
check("截断在字符串中间能闭合",
      llm.extract_json_lenient('{"a":"未写完的') == {"a": "未写完的"})
check("带围栏且被截断也能救",
      llm.extract_json_lenient('```json\n{"a":1,"b":[2,3\n```') == {"a": 1, "b": [2, 3]})
check("完全不像 JSON 时返回 None", llm.extract_json_lenient("随便说几句") is None)
check("空串不炸", llm.extract_json_lenient("") is None)
# ★ 严禁因为「能救」就把非法结构也吞下去 —— 只接受 dict
check("救出来必须是 dict（非 dict 一律 None）",
      llm.extract_json_lenient('[1,2,3') is None)

print()
print("=== 14. 生图重试计划（判定口径必须与文本通道一致）===")
from services import image_generator as ig  # noqa: E402

check("默认只用主模型（不偷偷换模型降质）",
      ig.image_models() == [ig.IMAGE_MODEL], str(ig.image_models()))
plan = ig.image_plan()
check("主模型 5 次尝试", sum(1 for m, _ in plan if m == ig.IMAGE_MODEL) == 5, str(len(plan)))
check("退避 0/5/15/30/45s（容量空窗期实测可达数十秒）",
      [w for m, w in plan if m == ig.IMAGE_MODEL] == [0.0, 5.0, 15.0, 30.0, 45.0],
      str([w for m, w in plan]))
# 显式配了备用模型才扩展（且要去重）—— 默认不动，是刻意的
_saved_fallbacks = ig.IMAGE_FALLBACK_MODELS
ig.IMAGE_FALLBACK_MODELS = ("gpt-image-2.5-flare", ig.IMAGE_MODEL)
plan2 = ig.image_plan()
check("配了备用模型后计划会扩展", len(plan2) == 7, str(len(plan2)))
check("备用模型排在后面", plan2[5][0] == "gpt-image-2.5-flare", str(plan2[5]))
check("备用模型会去重", [m for m, _ in plan2].count(ig.IMAGE_MODEL) == 5)
ig.IMAGE_FALLBACK_MODELS = _saved_fallbacks

# 各种错误的瞬时性判定：口径与 llm 完全一致
class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


for label, exc, expect in [
    ("503 没有可用通道", _Err("Error code: 503 - No available compatible account", 503), True),
    ("502 Bad Gateway", _Err("502 Bad Gateway", 502), True),
    ("429 限流", _Err("429 rate limit", 429), True),
    ("请求超时", _Err("Request timed out."), True),
    ("空 choices", Exception(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices"), True),
    ("401 鉴权失败", _Err("401 unauthorized", 401), False),
    ("参数非法", _Err("400 invalid size value", 400), False),
]:
    check(f"{label} → {'重试' if expect else '立刻失败'}",
          ig._is_transient_image_error(exc) is expect)

# ══════════════════════════════════════════════════════════
# 连续两次空响应 = 该通道本次已死 → 跳过它剩余的排期，直接换通道
# （2026-10-06 实测：deepseek 连返 3 次空 choices，白烧约 12s 才轮到备通道）
# ★ 单次空响应仍要原地重试一次 —— 那可能只是抖动（既有用例已断言此行为）
# ══════════════════════════════════════════════════════════
_empty = Exception(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices")
_ok_after_empty = FakeResponse(choices=[FakeChoice(FakeMessage(content="第一次空后重试成功"))])

# ① 单次空响应：应在同一通道原地重试，不急着换通道
rec = install({"deepseek": [_empty, _ok_after_empty], "premium": []})
restore = _patch_channels()
try:
    out = llm.chat([{"role": "user", "content": "hi"}])
    check("单次空响应：原地重试即成功", out == "第一次空后重试成功", str(out))
    check("单次空响应不会误判通道死（未切 premium）",
          rec.seen.count("premium") == 0, str(rec.seen))
finally:
    restore()
    uninstall()

# ② 连续两次空响应：判死，跳过第 3 次排期，直接换通道
_ok_premium = FakeResponse(choices=[FakeChoice(FakeMessage(content="OK"))])
rec = install({"deepseek": [_empty, _empty, _empty], "premium": [_ok_premium]})
restore = _patch_channels()
try:
    out = llm.chat([{"role": "user", "content": "hi"}])
    check("连续空响应后仍能拿到结果（切到备通道）", out == "OK", str(out))
    n_ds = rec.seen.count("deepseek")
    check("主通道只试 2 次即判死（不再排满 3 次）", n_ds == 2, f"实际 {n_ds} 次")
    check("切到了备通道", rec.seen.count("premium") == 1, str(rec.seen))
finally:
    restore()
    uninstall()

# 空响应判定本身
check("空响应能被识别", llm._is_empty_response(_empty) is True)
check("超时不算空响应（忙 ≠ 通道坏，仍值得重试）",
      llm._is_empty_response(Exception("Request timed out.")) is False)
check("502 不算空响应", llm._is_empty_response(_Err("502 Bad Gateway", 502)) is False)

# 交互式问答：预算必须封顶，且单次超时远小于全局 180s
check("小助手单次超时远小于全局",
      llm.HELPER_ATTEMPT_TIMEOUT_SEC < llm.REQUEST_TIMEOUT_SEC,
      f"{llm.HELPER_ATTEMPT_TIMEOUT_SEC} vs {llm.REQUEST_TIMEOUT_SEC}")

# ★ 排期必须给兜底留位置（第一版翻车：主通道 2 次就把预算吃满，兜底排不进去）
plan_h = llm._interactive_plan([CH_DS, CH_PM])
check("有兜底时：主通道 2 次 + 兜底 1 次",
      [c.name for c, _ in plan_h] == ["deepseek", "deepseek", "premium"],
      str([c.name for c, _ in plan_h]))
worst_h = sum(llm.HELPER_ATTEMPT_TIMEOUT_SEC + w for _, w in plan_h)
check("最坏耗时不超过总预算（参数改坏会被这里抓住）",
      worst_h <= llm.HELPER_TOTAL_BUDGET_SEC,
      f"最坏 {worst_h}s vs 预算 {llm.HELPER_TOTAL_BUDGET_SEC}s")
check("兜底排在最后且不退避", plan_h[-1] == (CH_PM, 0.0), str(plan_h[-1]))

# 只有一个通道时，预算应当全部归它（不留无谓的 reserve）
plan_h1 = llm._interactive_plan([CH_DS])
check("无兜底时主通道可排满 3 次", len(plan_h1) == 3, str(len(plan_h1)))
worst_h1 = sum(llm.HELPER_ATTEMPT_TIMEOUT_SEC + w for _, w in plan_h1)
check("单通道最坏耗时也不超预算",
      worst_h1 <= llm.HELPER_TOTAL_BUDGET_SEC,
      f"最坏 {worst_h1}s vs 预算 {llm.HELPER_TOTAL_BUDGET_SEC}s")

uninstall()
print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
