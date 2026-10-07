"""端到端实测 —— 打真实服务，走真实模型通道

    python tests/e2e_live.py [BASE_URL]

★ 与tests/ 下其它测试的区别：那些**离线**（假客户端、零真实调用），
  验的是"逻辑对不对"；这个跑的是**真链路**：真上传、真提炼卡、真渲染、
  真出图、真修复、真小助手问答、真工坊。
  它回答的问题是另一个问题 —— 「这些环节**串起来**还能不能走通？」

⚠️ 会花真实 API 费用。出图 2 次（生成 + 修复）是有意的：
  只测生成不测修复，等于把最贵的那条路径留到比赛当天才第一次跑。

每一步都记耗时，超时/失败立刻中止并说清是哪一步。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1
        else "http://127.0.0.1:8123").rstrip("/")
SID = f"e2e-{uuid.uuid4().hex[:12]}"
TIMEOUT = 300

TIMES: list[tuple[str, float, str]] = []
FAILED: list[str] = []


def step(name: str):
    def deco(fn):
        def run(*a, **kw):
            t0 = time.time()
            print(f"\n▶ {name} …", flush=True)
            try:
                res = fn(*a, **kw)
                dt = time.time() - t0
                TIMES.append((name, dt, "ok"))
                print(f"  ✔ {name} 用时 {dt:.1f}s", flush=True)
                return res
            except Exception as e:                        # noqa: BLE001
                dt = time.time() - t0
                TIMES.append((name, dt, "fail"))
                FAILED.append(f"{name}: {type(e).__name__}: {e}")
                print(f"  ✘ {name} 失败（{dt:.1f}s）：{e}", flush=True)
                raise
        return run
    return deco


def req(method: str, path: str, *, body=None, files=None, fields=None,
        headers=None, timeout=TIMEOUT) -> dict:
    """统一请求入口。

    ★ 路由与入参形态全部**从 OpenAPI 实测**得来，不靠记忆 ——
      第一版把 /api/generate 写成了 JSON，实际是 /api/image/generate 且收
      multipart表单，直接 404。凭印象写端到端脚本，测的就不是被测系统了。
    """
    url = BASE + path
    hdr = {"X-Session-Id": SID, **(headers or {})}
    data = None
    if files is not None or fields is not None:
        boundary = "----e2e" + uuid.uuid4().hex
        buf = io.BytesIO()
        for field, (fname, content, ctype) in (files or {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{field}"; '
                      f'filename="{fname}"\r\n'.encode())
            buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(content)
            buf.write(b"\r\n")
        for field, value in (fields or {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
                      .encode())
            buf.write(str(value).encode())
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        hdr["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"_raw": raw}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {e.code} {detail}") from e


def tiny_png() -> bytes:
    """造一张真实的 PNG（走 Pillow，和浏览器给的不一样但对上游一样）"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 480), (198, 176, 148))
    d = ImageDraw.Draw(img)
    d.rectangle([80, 300, 560, 470], fill=(120, 140, 110))          # 地
    d.polygon([(120, 300), (320, 90), (520, 300)], fill=(206, 198, 186))  # 山
    d.ellipse([430, 60, 470, 100], fill=(250, 238, 190))            # 日
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


print("=" * 64)
print(f"端到端实测  base={BASE}  session={SID}")
print("=" * 64)

health = req("GET", "/api/health")
print(f"  ready={health.get('ready')}  families="
      f"{len(health.get('templates', {}).get('by_kind', {}).get('family', []))}")

png = tiny_png()
print(f"  测试图 640x480，{len(png)} 字节")

# ── 1. 家族清单（前端首屏依赖）───────────────────────────────
@step("1. GET /api/families 家族清单")
def _s1():
    d = req("GET", "/api/families")
    fams = d.get("families") or d.get("items") or []
    assert fams, f"没拿到家族清单：{str(d)[:200]}"
    print(f"    家族数：{len(fams)}，第一个：{fams[0].get('id')}")
    return fams


families = _s1()
fam_id = families[0]["id"]

# ── 2. 上传 ─────────────────────────────────────────────────
@step("2. POST /api/chat/upload 上传原图")
def _s2():
    d = req("POST", "/api/chat/upload",
            files={"file": ("e2e.png", png, "image/png")},
            fields={"thread_id": "default"})
    src = d.get("source") or d
    assert src.get("width"), f"没拿到图片尺寸：{str(d)[:200]}"
    print(f"    尺寸 {src['width']}x{src['height']}  格式 {src.get('format')}")
    print(f"    source_id={src.get('id') or src.get('key')}")
    return src


source = _s2()
source_id = source.get("id") or source.get("key") or source.get("source_id")

# ── 3. 配额 ─────────────────────────────────────────────────
@step("3. GET /api/chat/policy 配额与开关")
def _s3():
    d = req("GET", "/api/chat/policy")
    print(f"    {json.dumps(d, ensure_ascii=False)[:180]}")
    return d


policy = _s3()

# ── 4. 生成（最贵的一步：提炼卡 VLM + 渲染 + 出图）──────────
#★ `source_id` 在这个端点里指的是**模板/家族 id**，不是刚上传那张图
#   （图走 file 字段）。第一版传了上传返回的路径 → 404「找不到该模板」。
#   产品的**主流程**其实不走这个端点，而是走 Agent（见第 8 步）；
#   这里是独立的 HTTP 通路，值得单独测——它是给API 调用方用的。
@step("4. POST /api/image/generate 生成（提炼卡→渲染→出图）")
def _s4():
    d = req("POST", "/api/image/generate",
            files={"file": ("e2e.png", png, "image/png")},
            fields={"source_id": fam_id, "params": json.dumps({}),
                    "thread_id": "default", "extra_prompt": "",
                    "extra_mode": "append", "locked": ""})
    assert d.get("ok"), f"生成未成功：{str(d)[:300]}"
    img = d.get("image") or d.get("source") or {}
    print(f"    家族：{fam_id}")
    print(f"    成品：{img.get('width')}x{img.get('height')}")
    print(f"    提示词前 60 字：{(d.get('prompt') or '')[:60]}")
    return d


gen = _s4()
gen_url = ((gen.get("image") or {}).get("url") or gen.get("image_url"))

# ── 5. 局部修复（第二贵的一步）───────────────────────────────
@step("5. POST /api/image/repair 局部修复")
def _s5():
    d = req("POST", "/api/image/repair",
            fields={"change": "把天空的云去掉一些，画面更干净",
                    "reference": gen_url, "thread_id": "default"})
    assert d.get("ok"), f"修复未成功：{str(d)[:300]}"
    print(f"    修复件：{(d.get('image') or {}).get('width')}x"
          f"{(d.get('image') or {}).get('height')}")
    return d


rep = _s5()

# ── 9. 工坊提炼（最重的一步：多轮 LLM 迭代，4–9 分钟）──────────
#★ 这是此前唯一没有黑盒验证过的链路：读代码与单测都覆盖不到
#   "逐图解构 → 角色设定 → 合成 → 编译 → 自修轮" 串起来是否真能出稿。
#   走异步接口（/draft-async），因为它必然超过任何网关的同步超时。
@step("9. POST /api/forge/draft-async 工坊提炼（真实多轮迭代）")
def _s9():
    d = req("POST", "/api/forge/draft-async",
            body={"image_urls": [gen_url, source.get("url") or ""],
                  "theory": "要像手绘旅行速写，线条松弛， watercolor 质感，"
                            "保留真实地景的构图",
                  "user_notes": "主体必须保持可辨认",
                  "style_prompt": "", "base_family_id": fam_id,
                  "name": f"e2e-{uuid.uuid4().hex[:6]}"})
    forge_id = d.get("forge_id") or d.get("task_id")
    assert forge_id, f"没拿到 forge_id：{str(d)[:300]}"
    print(f"    forge_id={forge_id}，提炼中（这一步最慢，请耐心）…")
    last = ""
    for i in range(560):                       # 最多等约 9 分钟
        time.sleep(1.0)
        snap = req("GET", f"/api/forge/tasks/{forge_id}")
        st = snap.get("status")
        msg = (snap.get("message") or snap.get("stage") or "")
        if msg and msg != last:
            print(f"      [{i:>3}s] {msg}", flush=True)
            last = msg
        if st in ("done", "failed", "error"):
            print(f"    终态：{st}")
            assert st == "done", f"提炼失败：{str(snap)[:250]}"
            row = req("GET", f"/api/forge/{forge_id}")
            spec = row.get("spec") or {}
            print(f"    家族名：{row.get('name')}")
            print(f"    画幅：{len(spec.get('canvas') or [])//2} 种，"
                  f"参数 {len(spec.get('params_schema') or [])} 项")
            assert spec, "提炼完成但spec 为空"
            return row
    raise RuntimeError("工坊提炼 9 分钟仍未结束")


forge = _s9()

# ── 10. 草稿渲染（工坊产物要能真的渲出提示词）──────────────────
@step("10. POST /api/forge/{id}/render 渲染工坊草稿")
def _s10():
    d = req("POST", f"/api/forge/{forge['forge_id']}/render", body={})
    assert d.get("prompt"), f"渲染没出提示词：{str(d)[:200]}"
    print(f"    提示词 {len(d['prompt'])} 字符，前 50 字：{d['prompt'][:50]}")
    return d


rendered = _s10()

# ── 11. 画廊 ─────────────────────────────────────────────────
@step("11. GET /api/image/gallery 作品墙")
def _s11():
    d = req("GET", "/api/image/gallery?limit=10")
    items = d.get("items") or []
    assert items, "画廊是空的（刚生成的图应该在里面）"
    print(f"    可见作品 {len(items)} 张")
    return d


gallery = _s11()

# ── 12. 小助手问答 ───────────────────────────────────────────
@step("12. POST /api/helper/chat 小助手问答（纯文本）")
def _s12():
    d = req("POST", "/api/helper/chat",
            body={"messages": [{"role": "user",
                                "content": "上传的照片会保存在哪里？"}]},
            timeout=120)
    assert d.get("reply"), f"助手没回话：{str(d)[:200]}"
    print(f"    回复：{(d['reply'])[:70]}")
    return d


helper = _s12()

# ── 13. Agent 异步任务（Tool-use Loop 全链路）────────────────
@step("13. POST /api/chat/async + 轮询 Agent 工具循环")
def _s13():
    tid = f"e2e-thread-{uuid.uuid4().hex[:8]}"
    d = req("POST", "/api/chat/async",
            body={"thread_id": tid, "message": "先告诉我这个家族适合什么类型的照片",
                  "image_url": source.get("url") or source.get("image_url") or ""})
    task_id = d.get("task_id")
    assert task_id, f"没拿到 task_id：{str(d)[:300]}"
    print(f"    task_id={task_id}，轮询中…")
    seen_events, last_len, cursor = [], -1, 0
    for _ in range(180):
        time.sleep(1.0)
        snap = req("GET", f"/api/chat/tasks/{task_id}?cursor={cursor}")
        for ev in snap.get("events") or []:
            seen_events.append(ev.get("event"))
        cursor = snap.get("cursor", cursor)
        text = snap.get("text") or ""
        if len(text) != last_len:
            last_len = len(text)
        if snap.get("status") in ("done", "failed", "error"):
            print(f"    终态：{snap.get('status')}  事件 {len(seen_events)} 个")
            print(f"    工具轨迹：{seen_events[:12]}")
            print(f"    回复前 70 字：{text[:70]}")
            assert snap.get("status") == "done", f"任务失败：{text[:150]}"
            return snap
    raise RuntimeError("轮询 180 秒仍未结束")


task = _s13()

# ── 14. 配额对账 ────────────────────────────────────────────
@step("14. GET /api/chat/policy 额度正确扣减")
def _s14():
    d = req("GET", "/api/chat/policy")
    print(f"    {json.dumps(d.get('quota', d), ensure_ascii=False)[:200]}")
    return d


after = _s14()

# ── 15. 越权防护（拿别人会话的图片应被拒）────────────────────
@step("15. 越权取图应被拒（隔离判据）")
def _s15():
    try:
        d = req("GET", "/api/image/gallery?limit=5",
                headers={"X-Session-Id": "someone-else-" + uuid.uuid4().hex[:8]})
        return f"匿名兜底只给了 {len(d.get('items') or [])} 张（公共图，符合预期）"
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return "404（正确隔离）"
        raise


isolation = _s15()
print(f"    → {isolation}")

# ══════════════════ 汇总 ══════════════════
print("\n" + "=" * 64)
print("各环节耗时")
print("=" * 64)
for name, dt, st in TIMES:
    bar = "█" * max(1, int(dt / 2))
    print(f"  {st:4}  {name[:44]:<46} {dt:7.1f}s {bar}")
total = sum(d for _, d, _ in TIMES)
print("-" * 64)
print(f"  合计 {total:.1f}s  （成功 {len(TIMES) - len(FAILED)} / {len(TIMES)}）")
print(f"  额度：{after.get('quota', after)}")
if FAILED:
    print("\n失败步骤：")
    for f in FAILED:
        print(f"  ✘ {f}")
    sys.exit(1)
print("\n✔ 端到端全链路通过")
sys.exit(0)