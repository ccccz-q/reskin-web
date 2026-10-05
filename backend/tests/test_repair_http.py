"""repair / diagnose 的 HTTP 层测试 —— 会话隔离 + 落盘结构 + 限速

    python tests/test_repair_http.py

TestClient 直连 ASGI，不出真图（生成函数打桩，但**走真实的落盘代码**）。
守护四件事：
 ① /api/image/repair 的额度键必须跟会话走（merge_thread）——
    公开版里所有访客共用 "studio" 桶、任何人改表单动别人额度，都算事故
 ② 连续修复的落盘：目录深度恒定、文件名不叠 gen_gen_（2026-10-05 实锤的嵌套 bug）
 ③ 修复的参考图必须过会话读权限（may_read）：别人的图不能拿来当参考
 ④ /api/image/diagnose 限速与降级：超限 429，失败 ok=false 而不是 5xx
"""
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import services.context_store as cs                      # noqa: E402
from pathlib import Path as P                            # noqa: E402

tmp_db = P(tempfile.mkdtemp(prefix="repair_http_")) / "t.db"
cs.SQLITE_PATH = tmp_db

import config                                            # noqa: E402
config.SQLITE_PATH = tmp_db

# ── 临时存储树（与 test_public_isolation 同一套重定向纪律）──
TMP = Path(tempfile.mkdtemp(prefix="repair_http_imgs_"))
ROOT = TMP / "images"
ROOT.mkdir(parents=True)
A = "aaaa1111-2222-3333-4444-555566667777"
B = "bbbb2222-3333-4444-5555-666677778888"

import infra.storage as storage_mod                      # noqa: E402
import services.image_generator as ig                    # noqa: E402
import services.upload as up                             # noqa: E402
for mod in (config, up, ig, storage_mod):
    mod.IMAGE_STORAGE_DIR = ROOT

from fastapi.testclient import TestClient                # noqa: E402
from main import app                                     # noqa: E402
from infra.storage import to_url                         # noqa: E402
from governance import guard                             # noqa: E402

client = TestClient(app)
H_A = {"X-Session-Id": A}
H_B = {"X-Session-Id": B}

# 打桩出图必须绕开「禁出图」总开关 —— 我们要测的正是计费出图那条路
guard.DISABLE_IMAGE_GENERATION = False

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


def png_bytes(w=320, h=480):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (70, 90, 120)).save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


# ── 打桩：不出真图，但走真实落盘（_save_generated）────────
real_save = ig._save_generated


def fake_generate(**kw):
    ref = kw["reference_image_path"]
    path = real_save(b"FAKE-JPEG-BYTES", "image/jpeg", ref)
    return {"success": True, "image_path": path, "url": to_url(path),
            "size": "1024x1024"}


import routers.image as img_router                       # noqa: E402
img_router.generate_image_with_reference = fake_generate

# ── 上传一张原图（作为修复链的起点）──────────────────────
r = client.post("/api/chat/upload", headers=H_A,
                files={"file": ("gate.jpg", io.BytesIO(png_bytes()), "image/jpeg")})
assert r.status_code == 200, r.text
upload_url = r.json()["url"]
print(f"上传原图：{upload_url}")


def depth_of(url: str) -> int:
    key = url.split("/images/")[-1]
    return len(key.split("/")) - 1


# ── 测试 1：第一次修复 ───────────────────────────────────
print("\n[测试 1] /api/image/repair 基本路径")
r = client.post("/api/image/repair", headers=H_A, data={
    "change": "只修改「色彩与曝光」：天空改暖",
    "reference": upload_url,
    "thread_id": "studio",
})
check("HTTP 200 且 ok", r.status_code == 200 and r.json().get("ok"), r.text[:200])
rep1_url = r.json().get("image_url", "")
check("落盘在自己会话命名空间下", rep1_url.startswith(f"/images/{A}/"), rep1_url)
check("目录深度 = 2（sid/日期），不再嵌套", depth_of(rep1_url) == 2, str(depth_of(rep1_url)))
check("文件名没有 gen_gen_ 叠加", rep1_url.count("gen_") == 1, rep1_url)

# ── 测试 2：连续修复 —— 深度与文件名不许滚雪球 ──────────
print("\n[测试 2] 连续修复（上一版成品作参考图）")
r = client.post("/api/image/repair", headers=H_A, data={
    "change": "只修改「光线层级」：云彩加层次",
    "reference": rep1_url,
    "thread_id": "studio",
})
check("第二次修复成功", r.status_code == 200 and r.json().get("ok"), r.text[:200])
rep2_url = r.json().get("image_url", "")
check("★ 连修后目录深度仍是 2（旧实现会变成 3）", depth_of(rep2_url) == 2, rep2_url)
check("★ 文件名仍只有一层 gen_ 前缀", rep2_url.count("gen_") == 1, rep2_url)

# ── 测试 3：会话隔离 ─────────────────────────────────────
print("\n[测试 3] 会话隔离")
r = client.post("/api/image/repair", headers=H_B, data={
    "change": "偷用别人的图当参考",
    "reference": rep2_url,
    "thread_id": "studio",
})
check("★ B 会话拿 A 的成品当参考 → 404", r.status_code == 404, r.status_code)

rem_a = guard.remaining_quota(A)
rem_studio = guard.remaining_quota("studio")
rem_default = guard.remaining_quota("default")
check("★ 额度记在会话 A 名下（merge_thread 生效，不是裸 thread_id）",
      rem_a.get("used", 1) >= 2, str(rem_a))
check("studio / default 桶没有被消耗",
      rem_studio.get("used", 0) == 0 and rem_default.get("used", 0) == 0,
      f"studio={rem_studio} default={rem_default}")

# ── 测试 4：diagnose 的校验与限速 ────────────────────────
print("\n[测试 4] /api/image/diagnose")
r = client.post("/api/image/diagnose", headers=H_A,
                data={"original": "https://evil.example/x.png", "generated": rep2_url})
check("远程 URL → 400", r.status_code == 400, r.status_code)
r = client.post("/api/image/diagnose", headers=H_B,
                data={"original": upload_url, "generated": rep2_url})
check("★ B 会话读不到 A 的图 → 404", r.status_code == 404, r.status_code)

import services.drift as drift_mod                       # noqa: E402
drift_mod.diagnose_drifts = lambda o, g, hint="", builtin=False: {"ok": True, "drifts": [
    {"change": "测试漂移", "kind": "drift", "why": "t"}], "elapsed_sec": 0.1}
r = client.post("/api/image/diagnose", headers=H_A,
                data={"original": upload_url, "generated": rep2_url})
check("打桩后诊断可用", r.status_code == 200 and r.json().get("ok"), r.text[:200])

# 家族上下文要原样透传给判定层（模板元素不被误报的通路）
seen_hint: list = []


def spy(o, g, hint="", builtin=False):
    seen_hint.append((hint, builtin))
    return {"ok": True, "drifts": [], "elapsed_sec": 0.1}


drift_mod.diagnose_drifts = spy
client.post("/api/image/diagnose", headers=H_A,
            data={"original": upload_url, "generated": rep2_url,
                  "family_id": "doodle_narrators"})
check("★ family_id 透传成 family_hint（名字｜描述）",
      seen_hint and "趣味小人" in seen_hint[0][0] and "｜" in seen_hint[0][0], str(seen_hint))
check("★ 内置家族被标记 builtin=True（禁用模板内部理由）",
      seen_hint and seen_hint[0][1] is True, str(seen_hint))

# 限速：同源 60s 内最多 6 次真实判定（上面可用 + spy 两次已计入预算）
codes = []
for i in range(7):
    rr = client.post("/api/image/diagnose", headers=H_A,
                     data={"original": upload_url, "generated": rep2_url})
    codes.append(rr.status_code)
check("★ 预算耗尽后限速 429（含此前 2 次共 6 次放行）",
      codes[:4] == [200] * 4 and codes[4:] == [429] * 3, str(codes))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
