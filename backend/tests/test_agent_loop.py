"""Tool-use Loop 离线验收 —— 用假 LLM 客户端驱动，不花任何真实调用

要验的是引擎的行为，不是 LLM 的能力：
  ① 正常多工具调用路径
  ② 步数护栏
  ③ 重复调用熔断
  ④ Schema 校验把 typo 变成可见观察结果
  ⑤ 观察截断
  ⑥ 出图成功后强制收尾（forced_final）
  ⑦ preview 模式下计费工具不可见
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# 让本文件既能被 pytest 收集，也能 python xxx.py 直接运行：
# 把 backend/app 加进 import root（与 uvicorn main:app 的约定一致）
_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

tmp = Path(tempfile.mkdtemp(prefix="loop_")) / "loop.db"

import services.context_store as cs  # noqa: E402

cs.SQLITE_PATH = tmp
import config  # noqa: E402

config.SQLITE_PATH = tmp
cs.init_db()

import services.llm as llm_mod  # noqa: E402
import engine.loop as loop  # noqa: E402

from tools.registry import missing_implementations  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


class FakeLLM:
    """按剧本返回工具调用序列"""

    def __init__(self, script, record):
        self.script = list(script)
        self.record = record

    def __call__(self, messages, tools=None, tool_choice="auto", temperature=0.2):
        self.record.append({"n_tools": len(tools or []), "tool_choice": tool_choice,
                            "last_role": messages[-1]["role"] if messages else None})
        if not self.script:
            return {"content": "兜底结论", "tool_calls": [], "usage": {}, "finish_reason": "stop"}
        item = self.script.pop(0)
        calls = item.get("calls")
        if calls:
            return {"content": item.get("content", ""),
                    "tool_calls": [{"id": f"c{len(self.record)}_{i}", "name": c[0],
                                    "arguments": c[1]} for i, c in enumerate(calls)],
                    "usage": {"total_tokens": 10}, "finish_reason": "tool_calls"}
        return {"content": item.get("content", ""), "tool_calls": [],
                "usage": {"total_tokens": 10}, "finish_reason": "stop"}


def install(script, record):
    fake = FakeLLM(script, record)
    loop.chat_with_tools = fake
    return fake


def fresh_ctx_info():
    # 造一张真实的 1x1 PNG 作为参考图，让 resolve_size / Pillow 走真实路径
    from PIL import Image
    d = Path(tempfile.mkdtemp(prefix="img_"))
    p = d / "ref.png"
    Image.new("RGB", (320, 480), (70, 90, 120)).save(p)
    info = {"filename": "ref.png", "width": 320, "height": 480,
            "orientation": "portrait", "format": "PNG"}
    return str(p), info


print("=== 0. 契约 vs 实现 ===")
check("契约与实现完全对齐", missing_implementations() == [],
      str(missing_implementations()))

print()
print("=== 1. 正常路径：list → describe → render → 收尾 ===")
img, info = fresh_ctx_info()
rec1 = []
install([
    {"calls": [("list_families", {})]},
    {"calls": [("describe_family", {"family_id": "second_world"})]},
    {"calls": [("render_prompt", {"family_id": "second_world", "params": {}})]},
    {"content": "我选了「第二世界」家族。"},
], rec1)
r1 = loop.run("帮我把这张雪山做成立体纸雕感", thread_id="t1", image_path=img,
              image_info=info, card={"subject": {"name": "雪山垭口"}})
check("最终得到中文回复", "第二世界" in r1.reply, repr(r1.reply[:40]))
check("走了 3 个工具事件", len(r1.tool_events) == 3, str([e["name"] for e in r1.tool_events]))
check("stopped_reason=completed", r1.stopped_reason == "completed", r1.stopped_reason)
check("渲染产物已记录", bool(r1.artifacts.get("last_prompt")))
check("全过程给了工具", all(x["n_tools"] > 0 for x in rec1), str([x["n_tools"] for x in rec1]))

print()
print("=== 2. Schema 校验：模型的 typo 变成可见观察结果 ===")
rec2 = []
install([
    {"calls": [("describe_family", {"family": "zine"})]},      # 参数名写错：family
    {"content": "收到，我修正一下。"},
], rec2)
r2 = loop.run("看看 zine 的参数", thread_id="t2", image_path=img, image_info=info)
ev2 = r2.tool_events[0]
check("工具事件被记录", ev2["name"] == "describe_family")
check("观察里带了 error", any(
    "error" in m.get("content", "") for m in r2.trace if m.get("role") == "tool"))
check("成功收尾", r2.stopped_reason == "completed")

print()
print("=== 3. 重复调用熔断 ===")
rec3 = []
install([
    {"calls": [("render_prompt", {"family_id": "zine", "params": {}})]},
    {"calls": [("render_prompt", {"family_id": "zine", "params": {}})]},
    {"calls": [("render_prompt", {"family_id": "zine", "params": {}})]},
    {"content": "不该走到这一步"},
], rec3)
r3 = loop.run("渲染 zine", thread_id="t3", image_path=img, image_info=info)
check("触发熔断", r3.stopped_reason == "repeat_fuse", r3.stopped_reason)
check("给了用户兜底结论", bool(r3.reply), repr(r3.reply[:30]))

print()
print("=== 4. 步数护栏（每次参数都不同，避免先撞上熔断）===")
loop.MAX_AGENT_STEPS = 3
fam_names = ["zine", "full_restyle", "surreal_collage", "split_poster",
             "portrait_epic", "second_world", "zine_again", "x", "y", "z"]
rec4 = []
install([{"calls": [("describe_family", {"family_id": n})]} for n in fam_names], rec4)
r4 = loop.run("无限循环测试", thread_id="t4", image_path=img, image_info=info)
check("触发 step_limit", r4.stopped_reason == "step_limit", r4.stopped_reason)
check("步数不超上限", r4.steps <= 3, str(r4.steps))
check("仍有回复给用户", bool(r4.reply))
loop.MAX_AGENT_STEPS = 8

print()
print("=== 5. 预览模式：计费工具不可见 ===")
rec5 = []
install([{"content": "好的"}], rec5)
r5 = loop.run("随便聊聊", thread_id="t5", image_path=img, image_info=info,
              allow_spend=False)
from contracts.tools import SPEND_TOOLS, TOOL_NAMES  # noqa: E402

check("工具集按契约裁剪（去掉 SPEND_TOOLS）",
      rec5[0]["n_tools"] == len(TOOL_NAMES) - len(SPEND_TOOLS),
      f"实际 {rec5[0]['n_tools']}，契约 {len(TOOL_NAMES)} 个 - 计费 {len(SPEND_TOOLS)} 个")
sysprompt = r5.trace[0]["content"]
# 真实断言（旧版这里以 `or True` 结尾，恒真，等于没测）：
# 「当前可用工具」那一行的列表里不应出现 generate_image
tool_line = next(
    (ln for ln in sysprompt.splitlines() if ln.startswith("当前可用工具：")), ""
)
check("System Prompt 的工具清单里没有 generate_image",
      "generate_image" not in tool_line, tool_line[:80])

print()
print("=== 6. 观察长度截断 ===")
loop.MAX_TOOL_OBSERVATION_CHARS = 200
rec6 = []
install([
    {"calls": [("render_prompt", {"family_id": "second_world", "params": {}})]},
    {"content": "好"},
], rec6)
r6 = loop.run("渲染", thread_id="t6", image_path=img, image_info=info)
tool_msgs = [m for m in r6.trace if m.get("role") == "tool"]
check("工具消息被截断到 <=200", all(len(m["content"]) <= 230 for m in tool_msgs),
      str([len(m["content"]) for m in tool_msgs]))
loop.MAX_TOOL_OBSERVATION_CHARS = 2000

print()
print("=== 7. 历史落库 + 可检索 ===")
h = cs.history("t1", limit=50)
check("t1 有历史", len(h) > 0, f"{len(h)} 条")
check("含 tool 消息", any(m.get("role") == "tool" for m in h))
check("含 assistant 回复", any(m.get("role") == "assistant" for m in h))
print("   t1 角色序列:", [m["role"] for m in h])

print()
print("=== 8. LLM 异常时优雅降级 ===")


def boom(*a, **k):
    raise llm_mod.LLMError("APITimeoutError: 连接超时")


loop.chat_with_tools = boom
r8 = loop.run("hello", thread_id="t8", image_path=img, image_info=info)
check("LLM 失败不抛出", r8.stopped_reason == "llm_error", r8.stopped_reason)
check("回复里说明了失败", "模型调用失败" in r8.reply, repr(r8.reply[:40]))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")

# ★ 必须用退出码报告失败 —— 只 print 不 exit 的话，
#   run_all.py 永远看到 returncode=0，整套自测就会假绿。
sys.exit(1 if FAIL else 0)
