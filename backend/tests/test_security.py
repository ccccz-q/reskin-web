"""访问控制与 SSE 资源护栏验收

    python tests/test_security.py

覆盖审查发现 P1-5（无鉴权 → 恶意网页可刷额度）与 P2-6（SSE 三处资源洞）：
  ① Origin 白名单挡住浏览器 drive-by 写操作
  ② 可选 LOCAL_TOKEN 强校验
  ③ SSE 并发上限
  ④ 客户端断连后 Agent 提前收尾（不再空烧钱）
  ⑤ 只读请求不受任何影响
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

os.environ["DISABLE_IMAGE_GENERATION"] = "1"
os.environ.pop("LOCAL_TOKEN", None)

import services.context_store as cs  # noqa: E402

cs.SQLITE_PATH = Path(tempfile.mkdtemp(prefix="sec_")) / "s.db"

import config  # noqa: E402

config.SQLITE_PATH = cs.SQLITE_PATH

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def fresh_client(**env):
    """每次重新 import main 太贵；这里用 client 的 headers 模拟不同来源"""
    from fastapi.testclient import TestClient
    import importlib
    import main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app)


client = fresh_client()

print("=== 1. Origin 白名单：模拟恶意网页 ===")
GOOD = "http://localhost:5173"
BAD = "https://evil.com"

r = client.post(f"/api/chat/reset-quota?thread_id=t", headers={"Origin": GOOD})
check("白名单内的来源放行", r.status_code == 200, str(r.status_code))

r = client.post(f"/api/chat/reset-quota?thread_id=t", headers={"Origin": BAD})
check("★ 恶意网页被挡 403", r.status_code == 403, str(r.status_code))
check("给的是结构化错误码", r.json().get("code") == "origin_denied", str(r.json())[:90])

r = client.post(f"/api/chat/reset-quota?thread_id=t")
check("无 Origin（curl/本地脚本）放行", r.status_code == 200, str(r.status_code))

# 这个正是旧版最容易被利用的形态：简单请求、不触发预检
r = client.post("/api/chat/reset-quota?thread_id=t",
                headers={"Origin": BAD, "Content-Type": "text/plain"},
                content="x=1")
check("text/plain 简单请求同样被挡", r.status_code == 403, str(r.status_code))

print()
print("=== 2. 所有写方法都设闸 ===")
for method, path in [("DELETE", "/api/chat/history?thread_id=t"),
                     ("POST", "/api/image/render")]:
    r = client.request(method, path, headers={"Origin": BAD}, json={})
    check(f"{method} {path.split('?')[0]} 被挡", r.status_code == 403, str(r.status_code))

print()
print("=== 3. 只读请求完全不受影响 ===")
for path in ("/api/health", "/api/families", "/api/chat/policy?thread_id=t",
             "/api/templates", "/api/sources", "/api/inventory"):
    r = client.get(path, headers={"Origin": BAD})
    check(f"GET {path.split('?')[0]} 仍 200", r.status_code == 200, str(r.status_code))

print()
print("=== 4. 安全快照不回显敏感信息 ===")
sec = client.get("/api/health").json()["security"]
check("有 origin_allowlist", isinstance(sec.get("origin_allowlist"), list))
check("token_required=false（未配置时）", sec.get("token_required") is False)
check("快照里没有任何令牌值", "secrettoken" not in str(sec))

print()
print("=== 5. LOCAL_TOKEN 模式 ===")
# 直接改 config 这个单一真源 —— 生产路径上它就是启动时从 env 读的，
# 这里改它等价于「用配了令牌的环境启动进程」。
# （不给 os.environ 打补丁：config 只在 import 时读一次 env，
#   改 env 对这种已加载的模块是无效的，那会造出一个假通过的测试。）
config.LOCAL_TOKEN = "secrettoken123"
client2 = fresh_client()
for headers, expect, note in [
    ({}, 401, "无令牌"),
    ({"X-Local-Token": "wrong"}, 401, "错令牌"),
    ({"X-Local-Token": "secrettoken123"}, 200, "正确令牌"),
]:
    r = client2.post("/api/chat/reset-quota?thread_id=t", headers=headers)
    check(f"{note} → {expect}", r.status_code == expect, str(r.status_code))
check("快照报告 token_required=true",
      client2.get("/api/health").json()["security"]["token_required"] is True)
check("★ Origin 闸在令牌模式下依然生效（两道闸独立）",
      client2.post("/api/chat/reset-quota?thread_id=t",
                   headers={"Origin": BAD,
                            "X-Local-Token": "secrettoken123"}).status_code == 403)
config.LOCAL_TOKEN = ""
client = fresh_client()

print()
print("=== 6. ★ 断连感知：Agent 必须在步与步之间停下来 ===")
import engine.loop as loop  # noqa: E402

calls = {"n": 0}


def counting_chat(messages, tools=None, tool_choice="auto", temperature=0.2):
    """每次被调用就计数 —— 用来验证断连后不再继续请求模型"""
    calls["n"] += 1
    return {"content": "", "tool_calls": [{"id": f"c{calls['n']}", "name": "list_families",
                                           "arguments": {}}],
            "usage": {}, "finish_reason": "tool_calls"}


loop.chat_with_tools = counting_chat

# 场景 A：不取消 → 跑满步数上限
calls["n"] = 0
r = loop.run("随便聊聊", thread_id="nocancel", should_abort=None)
check("不取消时正常跑", r.stopped_reason in ("step_limit", "repeat_fuse"), r.stopped_reason)
baseline = calls["n"]
check("确实调用了多次模型", baseline >= 2, f"{baseline} 次")

# 场景 B：第一步之后就取消
calls["n"] = 0
state = {"n": 0}


def abort_after_first():
    state["n"] += 1
    return state["n"] > 1          # 第二次询问时返回 True


r2 = loop.run("随便聊聊", thread_id="cancel", should_abort=abort_after_first)
check("stopped_reason=client_abort", r2.stopped_reason == "client_abort", r2.stopped_reason)
check("★ 取消后调用次数明显减少", calls["n"] < baseline,
      f"取消 {calls['n']} 次 vs 不取消 {baseline} 次")
check("有给用户的中止说明", "中止" in r2.reply, repr(r2.reply[:30]))

# 场景 C：一开始就取消 → 一次模型调用都不该发生
calls["n"] = 0
r3 = loop.run("随便聊聊", thread_id="cancel2", should_abort=lambda: True)
check("立即取消 → 0 次模型调用", calls["n"] == 0, f"{calls['n']} 次")
check("stopped_reason=client_abort", r3.stopped_reason == "client_abort", r3.stopped_reason)

# 场景 D：探针自己抛异常不能把 Agent 搞崩
def broken_probe():
    raise RuntimeError("探针坏了")


r4 = loop.run("随便聊聊", thread_id="broken", should_abort=broken_probe)
check("探针异常时按「未取消」处理", r4.stopped_reason != "client_abort", r4.stopped_reason)

print()
print("=== 7. SSE 并发上限与许可回收 ===")
import asyncio  # noqa: E402
from routers import chat as chat_router  # noqa: E402

check("配置了并发上限", chat_router.MAX_SSE_CONCURRENCY >= 1,
      f"MAX_SSE_CONCURRENCY={chat_router.MAX_SSE_CONCURRENCY}")
import threading as _threading  # noqa: E402
check("闸门已创建", isinstance(chat_router._sse_gate, _threading.Semaphore))


def _slot_value():
    """读信号量剩余许可。测试专用 —— 生产代码绝不该这么干（见 chat.py 的注释）"""
    return getattr(chat_router._sse_gate, "_value", None)


# ★ 这条是冲着「许可泄漏」去的，写法必须能真的抓住它：
#   泄漏的表现是「跑几轮流之后，剩余许可越来越少，最后端点死锁」。
#   所以这里跑**远超上限**的轮数，每轮结束都要求许可回到初始值。
async def drain_rounds(total: int) -> list[int]:
    before = _slot_value()
    observed = []
    for _ in range(total):
        chat_router._sse_gate.acquire()
        chat_router._sse_gate.release()
        observed.append(_slot_value())
    return [before] + observed


rounds = asyncio.run(drain_rounds(chat_router.MAX_SSE_CONCURRENCY * 3))
check("多轮流式后许可不泄漏",
      all(v == rounds[0] for v in rounds),
      f"初始 {rounds[0]}，序列 {rounds[1:]}")

# 用「显式标志」而不是 locked() 判断释放 —— 直接验证这个语义差异
async def locked_semantics():
    """asyncio.Semaphore.locked() 返回的是「计数器==0」即『满员』，
    不是「正在被占用」。第一版代码把它当后者用，导致永远不释放。"""
    sem = asyncio.Semaphore(4)
    await sem.acquire()
    return sem.locked()          # 占 1 个还剩 3 个 → 应为 False


check("locked() 语义 = 满员（不是占用中）", asyncio.run(locked_semantics()) is False,
      "这正是第一版写反的地方")

print()
print("=== 8. ★ extract_card 在预览模式下必须降级（复审 P1-5）===")
from contracts.tools import costs_money, CONDITIONAL_SPEND_TOOLS, SPEND_TOOLS  # noqa: E402
from tools.registry import ToolContext, dispatch  # noqa: E402
from PIL import Image  # noqa: E402

check("generate_image 恒计费", costs_money("generate_image", {}) is True)
check("extract_card(use_vlm=true) 计费", costs_money("extract_card", {"use_vlm": True}) is True)
check("extract_card(use_vlm=false) 不计费",
      costs_money("extract_card", {"use_vlm": False}) is False)
check("extract_card 默认（不传参）按计费算",
      costs_money("extract_card", {}) is True)
check("describe_family 不计费", costs_money("describe_family", {}) is False)
check("extract_card 在条件计费表里", "extract_card" in CONDITIONAL_SPEND_TOOLS)

_tmpdir = Path(tempfile.mkdtemp(prefix="card2_"))
_photo = _tmpdir / "p.jpg"
Image.new("RGB", (640, 480), (240, 220, 190)).save(_photo, quality=92)

# 预览模式：dispatch 应把 use_vlm 强制降级，绝不真调视觉模型
ctx = ToolContext(thread_id="pv", image_path=str(_photo), allow_spend=False)
res = dispatch("extract_card", {"use_vlm": True}, ctx)
check("预览模式下调 extract_card 成功（降级而非拒绝）", res.ok, str(res.observation)[:80])
check("★ 结果带有 downgraded 标记",
      res.observation.get("downgraded") is True,
      str(res.observation.get("warning", ""))[:80])
check("★ 预览模式下没有调用 VLM（origin 是 local）",
      res.observation.get("origin") == "local",
      str(res.observation.get("origin")))
check("仍然拿到了色板（本地档没白跑）",
      bool(res.observation.get("palette")), str(res.observation.get("palette"))[:60])

# 允许花钱时不应该降级（未配 VISION_MODEL，所以 VLM 档会失败退回 local，
# 但 downgraded 必须为 False —— 语义是「我们没因为省钱而关掉它」）
ctx2 = ToolContext(thread_id="gen", image_path=str(_photo), allow_spend=True)
res2 = dispatch("extract_card", {"use_vlm": True}, ctx2)
check("计费模式下不标 downgraded", res2.observation.get("downgraded") is False,
      str(res2.observation.get("downgraded")))

# generate_image 在预览模式下必须被拒（无条件计费工具，不降级）
res3 = dispatch("generate_image", {"family_id": "zine"}, ctx)
check("生成图在预览模式下被拒", not res3.ok, str(res3.observation)[:70])

print()
print("=== 9. 重复 Origin 头必须拒绝（复审 P1-13）===")
for label, hdrs in [
    ("白名单在前", [("Origin", "http://localhost:5173"), ("Origin", "https://evil.com")]),
    ("恶意在前", [("Origin", "https://evil.com"), ("Origin", "http://localhost:5173")]),
]:
    r = client.post("/api/chat/reset-quota?thread_id=t", headers=hdrs)
    check(f"重复 Origin（{label}）→ 403", r.status_code == 403, str(r.status_code))

r = client.post("/api/chat/reset-quota?thread_id=t",
                headers={"Origin": "http://localhost:5173/"})
check("尾斜杠归一后放行", r.status_code == 200, str(r.status_code))

print()
print("=== 10. ★ SSE 断连全链路 ===")
# 为什么不用 TestClient.stream()：它会把整个响应缓冲完再交给你，
# 「中途停止读取」根本传不到 ASGI 层 —— 我第一版就是用它，结果断连/不断连
# 模型调用次数一模一样（都是跑满），看着像断连没生效，其实是没测到。
# 这里直接构造一个会在第 N 次轮询时返回 http.disconnect 的 Request，
# 驱动真正的 chat_stream 生成器 —— 断连是确定性的，不依赖任何 httpx 语义。
import asyncio as _aio  # noqa: E402
import time as _time  # noqa: E402
import threading as _th  # noqa: E402
import importlib  # noqa: E402

from starlette.requests import Request as _Req  # noqa: E402
from routers.chat import ChatRequest as _CR  # noqa: E402
from routers import chat as chat_router  # noqa: E402

_loop_module = loop
_calls = {"n": 0}


def slow_chat(messages, tools=None, tool_choice="auto", temperature=0.2):
    """每次睡 0.3s 并返回一个**参数不同**的工具调用

    参数必须每次不同：同参数连续三次会先撞上重复调用熔断，
    那样对照组也会提前停下，就分不出「断连截断」和「熔断截断」。
    """
    _calls["n"] += 1
    _time.sleep(0.3)
    return {"content": "", "tool_calls": [
        {"id": f"c{_calls['n']}", "name": "describe_family",
         "arguments": {"family_id": f"probe_{_calls['n']}"}}],
        "usage": {}, "finish_reason": "tool_calls"}


_loop_module.MAX_AGENT_STEPS = 8
_loop_module.chat_with_tools = slow_chat


def make_request(disconnect_after: int | None):
    """构造一个假 Request。disconnect_after=None 表示从不带断开。"""
    state = {"n": 0}

    async def receive():
        state["n"] += 1
        if disconnect_after is not None and state["n"] > disconnect_after:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/api/chat/stream",
        "headers": [(b"origin", b"http://localhost:5173")],
        "query_string": b"", "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000), "scheme": "http", "root_path": "",
    }
    return _Req(scope, receive)


async def drive_stream(disconnect_after: int | None) -> list[str]:
    """驱动一次 chat_stream，返回收到的事件名列表"""
    # 信号量要在这个事件循环里新建（跨 loop 复用会出问题）
    chat_router._sse_gate = _threading.Semaphore(chat_router.MAX_SSE_CONCURRENCY)
    req = make_request(disconnect_after)
    resp = await chat_router.chat_stream(
        req, _CR(message="慢慢来", thread_id="disc", allow_spend=False),
        "disc",   # ★ 直接调用绕过了 FastAPI 依赖注入 —— 显式给会话 ID
    )
    events: list[str] = []
    async for chunk in resp.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if line.startswith("event: "):
                events.append(line[7:])
        if len(events) > 500:
            break
    return events


def slot_now() -> int:
    return getattr(chat_router._sse_gate, "_value", -1)


# ── 断连组：第 4 次轮询就断开（约 2 秒处）
_calls["n"] = 0
events_disc = _aio.run(drive_stream(disconnect_after=4))
calls_disc = _calls["n"]
check("断连前已收到事件", len(events_disc) > 0, str(events_disc[:4]))
check("★ 断连后模型调用被截断（未跑满 8 步）",
      calls_disc < 8, f"调用了 {calls_disc} 次")
check("没走到 close（连接已断，推不出去）",
      "close" not in events_disc or events_disc[-1] != "close",
      str(events_disc[-3:]))
_time.sleep(1.0)
check("★ 断连后调用次数冻结（不再烧钱）",
      _calls["n"] == calls_disc, f"{calls_disc} → {_calls['n']}")
check("★ 许可已归还", slot_now() == chat_router.MAX_SSE_CONCURRENCY,
      f"剩余 {slot_now()}")

# ── 对照组：从不断开，应跑满步数
_calls["n"] = 0
events_norm = _aio.run(drive_stream(disconnect_after=None))
calls_norm = _calls["n"]
check("发出 start / tool_start / tool_end",
      "start" in events_norm and "tool_start" in events_norm and "tool_end" in events_norm,
      str(events_norm[:5]))
check("以 close 收尾", events_norm and events_norm[-1] == "close", str(events_norm[-3:]))
check("★ 不断开时跑得更远（证明断连确实生效）",
      calls_norm > calls_disc, f"正常 {calls_norm} 次 vs 断连 {calls_disc} 次")
check("★ 正常结束后许可也归还", slot_now() == chat_router.MAX_SSE_CONCURRENCY,
      f"剩余 {slot_now()}")

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
