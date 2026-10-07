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
print("=== 9. 长期记忆：摘要 + 结构化画像 ===")
# ★ 这一节专治「prefs 永远是空 dict」那个陈年空壳（2026-10-06 修复）：
#   旧代码 `prefs = old.get("prefs")` 读出来又写回去，等于什么都没做，
#   L5 长期记忆层实际上只吃到那一段自然语言摘要。

xj = loop._extract_json_object
check("裸 JSON 能解析", xj('{"summary":"a","prefs":{}}') == {"summary": "a", "prefs": {}})
check("代码块包裹也能解析", xj('```json\n{"summary":"a"}\n```').get("summary") == "a")
check("前后有废话也能抠出来", xj('这是摘要：{"summary":"a"} 完毕').get("summary") == "a")
check("非法 JSON 返回 None", xj('我就不按格式来') is None)
check("空输入返回 None", xj("") is None)
check("JSON 数组不是对象", xj('[1,2,3]') is None)

np_ = loop._normalize_prefs
clean = np_({"喜欢的家族": ["小人国", "zine"], "常用画幅": "3:4、9:16",
             "偏好的氛围": ["暖色"], "回避的元素": [], "乱加的键": ["x"]})
check("键收敛到白名单", set(clean) <= set(loop._PREF_KEYS), str(sorted(clean)))
check("字符串按顿号切开", clean.get("常用画幅") == ["3:4", "9:16"], str(clean.get("常用画幅")))
check("空数组不进库", "回避的元素" not in clean)
check("脏类型整体丢弃", np_([1, 2]) == {} and np_("hi") == {})
check("重复项去重", np_({"喜欢的家族": ["小人国", "小人国"]})["喜欢的家族"] == ["小人国"])
big = np_({"喜欢的家族": [f"风格{i}" for i in range(20)]})
check("列表有上限", len(big["喜欢的家族"]) == loop._PREF_LIST_CAP,
      f"{len(big['喜欢的家族'])} 条")

mp = loop._merge_prefs
merged = mp({"喜欢的家族": ["旧的"], "历史遗留键": ["保留我"]}, {"喜欢的家族": ["新的"]})
check("新偏好排在前", merged["喜欢的家族"][0] == "新的", str(merged["喜欢的家族"]))
check("旧偏好没被丢", "旧的" in merged["喜欢的家族"])
check("★ 白名单外的旧键保留", merged.get("历史遗留键") == ["保留我"])

# ── 端到端 ①：秘书按格式返回 JSON → 画像真的写进去了
tid = "mem_json"
cs.clear_thread(tid)
check("初始画像为空", cs.load_long_term(tid)["prefs"] == {})

calls = []


def _sec_json(messages, max_tokens=300):
    calls.append(messages[-1]["content"][:200])
    return ('{"summary":"用户偏好暖色插画，反复用小人国家族",'
            '"prefs":{"喜欢的家族":["小人国"],"偏好的氛围":["暖色"]}}')


llm_mod.secretary = _sec_json
loop._update_long_term(tid)
lt = cs.load_long_term(tid)
check("摘要入库", "小人国" in lt["summary"], repr(lt["summary"][:30]))
check("★ 画像不再是空壳", lt["prefs"] != {}, str(lt["prefs"]))
check("画像内容正确", lt["prefs"].get("喜欢的家族") == ["小人国"], str(lt["prefs"]))
check("只调了一次秘书（摘要与画像合并）", len(calls) == 1, f"{len(calls)} 次")

# ── 端到端 ②：秘书不按格式返回 → 降级为摘要，且**已有画像不能被抹掉**
def _sec_plain(messages, max_tokens=300):
    return "这是一段没有 JSON 的纯文本摘要"


llm_mod.secretary = _sec_plain
loop._update_long_term(tid)
lt2 = cs.load_long_term(tid)
check("纯文本降级为摘要", lt2["summary"] == "这是一段没有 JSON 的纯文本摘要",
      repr(lt2["summary"]))
check("★ 解析失败不清空已有画像", lt2["prefs"].get("喜欢的家族") == ["小人国"],
      str(lt2["prefs"]))

# ── 端到端 ③：秘书抛异常也不能拖垮调用方
def _sec_boom(messages, max_tokens=300):
    raise RuntimeError("上游炸了")


llm_mod.secretary = _sec_boom
try:
    loop._update_long_term(tid)
    check("秘书异常被吞掉，不向上抛", True)
except Exception as e:                                              # noqa: BLE001
    check("秘书异常被吞掉，不向上抛", False, repr(e))
lt3 = cs.load_long_term(tid)
check("异常后数据仍是上一版", lt3["summary"] == "这是一段没有 JSON 的纯文本摘要")

print()
print("=== 14. 收尾失败的话术按「手上有没有东西」分档 ===")
# ★ 评审自查发现：repeat_fuse / step_limit 两处收尾原本都写死「（模型调用失败）…」。
#   可这三种情形的用户处境完全不同 —— 图已落盘时用户手上就是成品，
#   告诉他"调用失败"会让他以为白跑甚至重做（forced_final 早就修过这个坑）。
#   所以话术必须看 ctx.artifacts 说话，不能一句模板走天下。

class _Ctx:
    def __init__(self, arts):
        self.artifacts = arts


_reply_img = loop._final_fallback_reply(_Ctx({"image_path": "/x/y.png"}))
check("有图：明确告诉用户图已生成好", "图已经生成好了" in _reply_img, repr(_reply_img[:28]))
check("有图：不再出现「模型调用失败」字样", "模型调用失败" not in _reply_img)

_reply_prompt = loop._final_fallback_reply(_Ctx({"last_prompt": "a prompt"}))
check("只有提示词：给出可执行的下一步", "继续" in _reply_prompt, repr(_reply_prompt[:28]))

_reply_none = loop._final_fallback_reply(_Ctx({}))
check("什么都没有：给具体化建议", "具体一点" in _reply_none, repr(_reply_none[:28]))
check("三档话术互不相同",
      len({_reply_img, _reply_prompt, _reply_none}) == 3)
check("没有产物时也不会出现「模型调用失败」", "模型调用失败" not in _reply_none)
check("artifacts 缺失（None）也不炸",
      bool(loop._final_fallback_reply(_Ctx(None))))

print()
print("=== 15. step_limit 到达时 tool 协议必然完整（防御性补齐的正当性依据）===")
# 这条断言的作用不是"证明曾经有 bug"，而是**钉住这个前提**：
# 若将来有人在工具循环里加了新的 break 路径，这条会先红，
# 提醒他补_fill_missing_tool_replies（或者确认新路径也需要补）。
_seen = {"messages": None}


def _capture_summary(messages):
    _seen["messages"] = [dict(m) for m in messages]
    return {"content": "收尾结论", "tool_calls": [], "usage": {},
            "finish_reason": "stop"}


_prev_summary = loop.summarize_for_final
loop.summarize_for_final = _capture_summary
try:
    class FakeMulti:
        def __init__(self):
            self.i = 0

        def __call__(self, messages, tools=None, tool_choice="auto", temperature=0.2):
            self.i += 1
            return {"content": "", "finish_reason": "tool_calls",
                    "usage": {"total_tokens": 5},
                    "tool_calls": [
                        {"id": f"m{self.i}a", "name": "list_families",
                         "arguments": {"tag": str(self.i)}},
                        {"id": f"m{self.i}b", "name": "describe_family",
                         "arguments": {"family_id": "zine"}},
                    ]}

    loop.chat_with_tools = FakeMulti()
    _prev_steps = loop.MAX_AGENT_STEPS
    loop.MAX_AGENT_STEPS = 2
    r15 = loop.run("协议完整性探针", thread_id="t15", image_path=img,
                   image_info=info)
    loop.MAX_AGENT_STEPS = _prev_steps
    check("确实走到 step_limit", r15.stopped_reason == "step_limit",
          r15.stopped_reason)

    _declared, _answered = [], set()
    for _m in _seen["messages"] or []:
        for _tc in (_m.get("tool_calls") or []):
            _declared.append(_tc["id"])
        if _m.get("role") == "tool":
            _answered.add(_m.get("tool_call_id"))
    check("每个 tool_call 都有对应回复（协议完整）",
          all(i in _answered for i in _declared),
          f"声明 {len(_declared)} / 回复 {len(_answered)}")
    check("确实存在多个 tool_call（这条断言才有意义）",
          len(_declared) >= 2, str(len(_declared)))
finally:
    loop.summarize_for_final = _prev_summary

print()
print("=== 16. 上下文 token 预算：按条数裁剪不等于安全 ===")
#★ 评审自查发现：HISTORY_LIMIT 只管「条数」，20 条长消息的 token 量
#   可能顶得上 200 条短消息 —— 条数达标但 token 爆掉，上游直接拒绝整轮。
check("估算器：中文按 1 字 1 token（保守）",
      loop._est_tokens("汉" * 100) >= 100, str(loop._est_tokens("汉" * 100)))
check("估算器：ASCII 按 4 字符 1 token",
      loop._est_tokens("a" * 400) <= 110, str(loop._est_tokens("a" * 400)))
check("估算器：空串不算token", loop._est_tokens("") == 0)
check("估算器：单字符不会被估成 0（+1 保底）",
      loop._est_tokens("x") >= 1, str(loop._est_tokens("x")))

# 造一条超预算的长历史，验证真的被裁掉，且至少留最后一条
tid_budget = "t_budget"
for i in range(12):
    cs.append_message(tid_budget, "user", f"第{i}轮提问 " + "内容" * 400)
_saved_budget = config.CONTEXT_TOKEN_BUDGET
try:
    config.CONTEXT_TOKEN_BUDGET = 500          # 故意给得很小
    trimmed = loop._history_within_budget(tid_budget, 20)
    check("超预算时真的裁剪了（条数变少）", len(trimmed) < 12,
          f"{len(trimmed)}/ 12")
    check("★ 至少保留一条（不能裁成空对话）", len(trimmed) >= 1,
          str(len(trimmed)))
    check("★ 保留的是**最新**的那些（丢最旧）",
          bool(trimmed) and "第11轮" in str(trimmed[-1]),
          str(trimmed[-1])[:60] if trimmed else "")
    # 预算放大到极大时不该裁
    config.CONTEXT_TOKEN_BUDGET = 10_000_000
    full = loop._history_within_budget(tid_budget, 20)
    check("预算充足时一条都不裁", len(full) == 12, f"{len(full)}/ 12")
    # 预算<=0 时退化为「只按条数」（关掉这道闸）
    config.CONTEXT_TOKEN_BUDGET = 0
    off = loop._history_within_budget(tid_budget, 5)
    check("预算设为 0 = 关闭这道闸（退回纯条数限制）",
          len(off) == 5, str(len(off)))
finally:
    config.CONTEXT_TOKEN_BUDGET = _saved_budget

print()
print("=== 17. 护栏常量口径统一（都能从 config 走）===")
check("HISTORY_LIMIT 来自 config", loop.HISTORY_LIMIT == config.HISTORY_LIMIT,
      f"{loop.HISTORY_LIMIT} vs {config.HISTORY_LIMIT}")
check("REPEAT_FUSE 来自 config", loop.REPEAT_FUSE == config.REPEAT_FUSE,
      f"{loop.REPEAT_FUSE} vs {config.REPEAT_FUSE}")
check("MEMORY_TRIGGER 来自 config",
      loop.MEMORY_TRIGGER == config.MEMORY_TRIGGER,
      f"{loop.MEMORY_TRIGGER} vs {config.MEMORY_TRIGGER}")
_src = (_root / "engine" / "loop.py").read_text(encoding="utf-8")
check("loop.py 里不再硬编码这三个数字",
      "HISTORY_LIMIT = 20" not in _src and "REPEAT_FUSE = 2" not in _src
      and "MEMORY_TRIGGER = 12" not in _src)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")

# ★ 必须用退出码报告失败 —— 只 print 不 exit 的话，
#   run_all.py 永远看到 returncode=0，整套自测就会假绿。
sys.exit(1 if FAIL else 0)
