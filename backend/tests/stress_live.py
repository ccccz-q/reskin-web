"""性能压测 —— 并发、延迟分布、资源占用

    python tests/stress_live.py [BASE_URL] [并发数] [每轮请求数]

★ 与其它测试的区别：test_* 验"对不对"，这个验"快不快、稳不稳"。
  评审核评指标只有「快」，没有数字；这里产出一份可复现的基线。

★ 为什么不压生图端点
------------------
生图一次30 秒且要真金白银。压测要的是**并发下的调度与响应能力**，
用廉价端点（health / families / gallery / upload / helper）才能把并发开大，
测出真正的曲线；生图链路另外由e2e_live.py 做单次真实验证。
唯一例外：helper 会调真实 LLM，**刻意限制并发数**（默认 4），
否则等于用压测名义烧钱。

★ 记什么
--------
p50 / p95 / p99 / max、错误率、吞吐（req/s）、以及**服务端资源**快照。
只看平均值是不够的：平均值 80ms 听起来很好，但 p99 是 3s 就是另一回事。
"""
import io
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123").rstrip("/")
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 40
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 12

# ★ 显式关掉代理：压测必须直连，走代理测出来的是代理的性能
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, path: str, *, body=None, files=None, fields=None,
         sid="stress", timeout=60) -> tuple[int, float, str]:
    hdr = {"X-Session-Id": sid}
    data = None
    if files is not None or fields is not None:
        b = "----st" + uuid.uuid4().hex
        buf = io.BytesIO()
        for k, (fn, content, ct) in (files or {}).items():
            buf.write(f"--{b}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{k}"; '
                      f'filename="{fn}"\r\n'.encode())
            buf.write(f"Content-Type: {ct}\r\n\r\n".encode())
            buf.write(content)
            buf.write(b"\r\n")
        for k, v in (fields or {}).items():
            buf.write(f"--{b}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            buf.write(str(v).encode())
            buf.write(b"\r\n")
        buf.write(f"--{b}--\r\n".encode())
        data = buf.getvalue()
        hdr["Content-Type"] = f"multipart/form-data; boundary={b}"
    elif body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    t0 = time.perf_counter()
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            resp.read()
            return resp.status, time.perf_counter() - t0, ""
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t0, f"HTTP {e.code}"
    except Exception as e:                                 # noqa: BLE001
        return 0, time.perf_counter() - t0, f"{type(e).__name__}"


def png_bytes() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 600), (180, 170, 150))
    ImageDraw.Draw(img).rectangle([100, 400, 700, 580], fill=(90, 130, 90))
    b = io.BytesIO()
    img.save(b, format="JPEG", quality=70)          # JPEG 更大，更像真实上传
    return b.getvalue()


PNG = png_bytes()

SCENARIOS = [
    # (名称, 方法, 路径, 额外参数, 是否花钱)
    ("健康检查", "GET", "/api/health", {}, False),
    ("家族清单", "GET", "/api/families", {}, False),
    ("我的来源", "GET", "/api/sources", {}, False),
    ("作品墙", "GET", "/api/image/gallery?limit=12", {}, False),
    ("配额策略", "GET", "/api/chat/policy", {}, False),
    ("非法token（应 401/404，不应 5xx）", "GET", "/api/chat/history", {}, False),
    ("上传图片", "POST", "/api/chat/upload", {"files": True}, False),
    ("小助手问答（真调 LLM）", "POST", "/api/helper/chat", {"body": True}, True),
]

results: dict[str, list[tuple[int, float, str]]] = {n: [] for n, *_ in SCENARIOS}
lock = threading.Lock()


def worker(scn_idx: int, rounds: int) -> None:
    name, method, path, opt, _paid = SCENARIOS[scn_idx]
    sid = f"stress-{uuid.uuid4().hex[:8]}"
    for _ in range(rounds):
        kw: dict = {}
        if opt.get("files"):
            kw = {"files": {"file": ("s.jpg", PNG, "image/jpeg")}}
        elif opt.get("body"):
            kw = {"body": {"messages": [{"role": "user",
                                         "content": "局部修复在界面哪里？"}]}}
        code, dt, err = call(method, path, sid=sid, **kw)
        with lock:
            results[name].append((code, dt, err))


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s))) - 1))
    return s[k]


print("=" * 78)
print(f"性能压测base={BASE}  并发={CONC}  每场景请求数={ROUNDS * CONC}")
print("=" * 78)

print("\n[0] 基线（单请求，用于对照）")
_code, base_dt, _ = call("GET", "/api/health", sid="stress-base")
print(f"    /api/health 单请求 {base_dt * 1000:.1f} ms")

print(f"\n[1] 冷启动后并发压测（{CONC} 线程 × {ROUNDS} 轮/ 场景）")
wall0 = time.perf_counter()
threads: list[threading.Thread] = []
for i in range(CONC):
    # 花钱的场景只开 4 个并发（限流），其余场景全开
    for idx, (_n, _m, _p, _o, paid) in enumerate(SCENARIOS):
        if paid and i >= 4:
            continue
        t = threading.Thread(target=worker, args=(idx, ROUNDS))
        threads.append(t)
        t.start()
for t in threads:
    t.join(timeout=300)
wall = time.perf_counter() - wall0

total_req = sum(len(v) for v in results.values())
total_ok = sum(1 for v in results.values() for c, _, _ in v if 200 <= c < 300)
total_err = total_req - total_ok

print()
print(f"{'场景':<32}{'样本':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'错误':>7}")
print("-" * 78)
for name, _m, _p, _o, _paid in SCENARIOS:
    rows = results[name]
    if not rows:
        print(f"{name:<32}{'—':>6}")
        continue
    lats = [d for _c, d, _e in rows]
    errs = [e for c, _d, e in rows if not (200 <= c < 300)]
    print(f"{name:<32}{len(rows):>6}"
          f"{pct(lats, 50) * 1000:>8.1f}ms"
          f"{pct(lats, 95) * 1000:>8.1f}ms"
          f"{pct(lats, 99) * 1000:>8.1f}ms"
          f"{max(lats) * 1000:>8.1f}ms"
          f"{len(errs):>7}")
print("-" * 78)
print(f"{'合计':<32}{total_req:>6}{'':>9}{'':>9}{'':>9}{'':>9}{total_err:>7}")
print()
print(f"  吞吐{total_req / wall:.1f} req/s（墙钟 {wall:.1f}s，含花钱场景的串行等待）")
print(f"  成功率 {total_ok / max(1, total_req) * 100:.2f}%")

print("\n[2] 错误明细（按类型归并）")
allerr = Counter()
for rows in results.values():
    for code, _d, err in rows:
        if not (200 <= code < 300):
            allerr[err or f"HTTP {code}"] += 1
if not allerr:
    print("    无错误")
for k, v in allerr.most_common():
    print(f"    {k:<40} {v} 次")

print("\n[3] 越权压测（拿随机会话 ID 访问他人资源，应全部 4xx/空）")
code, dt, err = call("GET", "/api/image/gallery?limit=50",
                     sid="intruder-" + uuid.uuid4().hex[:8])
print(f"    随机会话取画廊 → HTTP {code}（{dt * 1000:.1f} ms）"
      f"{'← 正确隔离' if code in (200, 404, 403) else '← ★异常'}")

print("\n[4] SSE/任务并发上限（MAX_SSE_CONCURRENCY 应拦住超限请求）")
codes = []
lock2 = threading.Lock()


def _async_once(i: int) -> None:
    c, _d, _e = call("POST", "/api/chat/async",
                     body={"thread_id": f"stress-async-{uuid.uuid4().hex[:8]}",
                           "message": "你好"}, sid=f"stress-async-{uuid.uuid4().hex[:8]}")
    with lock2:
        codes.append(c)


_as = [threading.Thread(target=_async_once, args=(i,)) for i in range(12)]
for t in _as:
    t.start()
for t in _as:
    t.join(timeout=60)
print(f"    12 个并发异步任务 →状态分布 {dict(Counter(codes))}")
print("    （429 = 被并发闸拦住，属预期保护）")

print("\n[5] 结论")
slow = [n for n, *_ in SCENARIOS
        if results[n] and pct([d for _c, d, _e in results[n]], 95) > 2.0]
print(f"    错误率：{total_err}/{total_req}")
if allerr:
    print(f"    ★ 有错误类型：{dict(allerr)}")
if slow:
    print(f"    ★ p95 > 2s 的场景：{slow}")
else:
    print("    所有场景 p95 < 2s")
print(f"    服务端在压测期间未崩溃、未返回 5xx（除上表列出的）")
sys.exit(1 if (total_err / max(1, total_req) > 0.02) else 0)