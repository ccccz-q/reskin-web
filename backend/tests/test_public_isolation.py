"""公开版多用户隔离 · 画廊列举 + 写入 + 取图

背景（2026-10-03 线上实测，不是假想用例）：
同一份部署，不带 X-Session-Id 打一次 gallery，返回 31 张 —— 其中 13 张
分属 7 个 uuid 会话（别人的上传原图与生成图），连 URL 都给出了。根因是
"default 会话 = 扫全目录"这条本地开发的合理规则，被直接带进了公开版。

这套测试锁死四件事：
    1. 公开版里「身份未知」的请求，只拿得到公共展示图；
    2. 真实会话只见自己的图 + 公共图，会话之间不相交；
    3. 公开版不许往共享空间写 —— 否则会产出「生成成功但画廊里没有」的幽灵结果；
    4. 取图要校验归属（含 <img> 只能靠 Cookie 认证的那条通道），
       不能出现「列表挡住了、直连锁门却开着」。

★ 全程离线：不打网络、不花 API 调用，目录与数据库都是临时造的。
★ 会话名必须是**合法会话格式**（hex + 连字符）——白名单外的会被打回 default，
  那正是被「关系到 leak 本身」的行为，测试里借用真名模拟真访客。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

_root_app = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_root_app))

import config                                                         # noqa: E402
import services.identity as ident                                     # noqa: E402
import services.upload as up                                          # noqa: E402
import services.image_access as ia                                    # noqa: E402
import routers.image as img                                           # noqa: E402
import infra.storage as storage                                       # noqa: E402
from fastapi import HTTPException                                     # noqa: E402
from PIL import Image                                                 # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def tiny_png() -> bytes:
    """2×2 真 PNG —— 够 Pillow 解码，也够 stat 出体积"""
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ── 造一棵 storage 树 ────────────────────────────────────────────
# 会话目录名必须长得像真实 UUID（白名单才会放行），否则会被当成路径注入串。
A = "aaaa1111-2222-3333-4444-555566667777"
B = "bbbb2222-3333-4444-5555-666677778888"

_TMP = Path(tempfile.mkdtemp(prefix="wb_public_iso_"))
ROOT = _TMP / "images"
(ROOT / "_seed").mkdir(parents=True)
(ROOT / A / "2026-10-03").mkdir(parents=True)
(ROOT / B).mkdir(parents=True)
(ROOT / ".thumbs").mkdir()

_PNG = tiny_png()
(ROOT / "_seed" / "s1.png").write_bytes(_PNG)
(ROOT / "_seed" / "s2.png").write_bytes(_PNG)
(ROOT / "examples").mkdir(exist_ok=True)          # 家族示例图（公共素材）
(ROOT / "examples" / "split_poster.png").write_bytes(_PNG)
MINE = ROOT / A / "2026-10-03" / "gen_a.png"
MINE.write_bytes(_PNG)
THEIRS = ROOT / B / "gen_b.png"
THEIRS.write_bytes(_PNG)
(ROOT / "legacy_root.png").write_bytes(_PNG)        # 旧布局：根目录平铺
(ROOT / ".thumbs" / "thumb.jpg").write_bytes(_PNG)  # 派生缓存，不该露面

# ★ 各处 import 了各自的模块级常量，必须统一指向临时树。
#   漏掉 config 那份，image_access 的 relative_to 就会算出「越界」→ owner_of 空。
for mod in (config, up, img, storage):
    mod.IMAGE_STORAGE_DIR = ROOT

SEEDS = 2
# 2 seed + a + b + root + 1 张家族示例图（缩略图目录不算）
TOTAL = 6
URL_MINE = f"/images/{A}/2026-10-03/gen_a.png"
URL_THEIRS = f"/images/{B}/gen_b.png"
URL_SEED = "/images/_seed/s1.png"


def urls(items) -> set[str]:
    return {i["url"] for i in items}


def subs(items) -> set[str]:
    return {i["subdir"] for i in items}


print("\n── 1. 本地/内网（PUBLIC_MODE=0）：保留旧语义，default 扫全目录 ──")
config.PUBLIC_MODE = False
r = img._gallery_sync(500, "default")
check("default 见全部 6 张", r["count"] == TOTAL, f"实际 {r['count']}")
check("含根目录旧图", any(i["subdir"] == "." for i in r["items"]))

print("\n── 2. 公开版（PUBLIC_MODE=1）：身份未知 → 只给公共种子图 ──")
config.PUBLIC_MODE = True
r = img._gallery_sync(500, "default")
check("只返回种子图", r["count"] == SEEDS, f"实际 {r['count']}")
check("不出现别人会话", not any(f"/{A}" in u or f"/{B}" in u for u in urls(r["items"])),
      str(subs(r["items"])))
check("不出现根目录旧图", not any(i["subdir"] == "." for i in r["items"]))
check("缩略图目录不露面", not any(".thumbs" in u for u in urls(r["items"])))

print("\n── 3. 真实会话：自己 + 种子，两条会话互不相交 ──")
ra = img._gallery_sync(500, A)
rb = img._gallery_sync(500, B)
check("A 见自己的图", any("gen_a" in u for u in urls(ra["items"])))
check("A 也见种子图", any(i["kind"] == "seed" for i in ra["items"]))
check("A 不见 B 的图", not any("gen_b" in u for u in urls(ra["items"])))
check("B 不见 A 的图", not any("gen_a" in u for u in urls(rb["items"])))
check("B 不见根目录旧图", not any(i["subdir"] == "." for i in rb["items"]))
check("A/B 交集只有种子图",
      urls(ra["items"]) & urls(rb["items"]) ==
      {i["url"] for i in ra["items"] if i["kind"] == "seed"})

print("\n── 4. 读取 + 写入：公开版都不放行共享身份 ──")
check("is_anonymous_fallback(公开+default)", ident.is_anonymous_fallback("default") is True)
check("is_anonymous_fallback(公开+真实会话)", ident.is_anonymous_fallback(A) is False)
config.PUBLIC_MODE = False
check("is_anonymous_fallback(内网+default)", ident.is_anonymous_fallback("default") is False)
config.PUBLIC_MODE = True


class _FakeUpload:
    """够 validate_and_save 用的最小 UploadFile 替身"""

    class _F:
        def __init__(self, data): self._d = io.BytesIO(data)
        def read(self, n=-1): return self._d.read(n)

    def __init__(self, data):
        self.file = _FakeUpload._F(data)
        self.filename = "x.png"
        self.size = len(data)
        self.content_type = "image/png"


try:
    up.validate_and_save(_FakeUpload(_PNG), "upload", "default")
    msg = ""
except HTTPException as e:
    msg = str(e.detail)
check("公开版拒绝无身份写入", "X-Session-Id" in msg, msg[:40])

config.PUBLIC_MODE = False
try:
    saved_default = up.validate_and_save(_FakeUpload(_PNG), "upload", "default")
    ok = Path(saved_default["path"]).exists()
except Exception as e:                                    # noqa: BLE001
    ok = False
    print(f"       （内网写入抛 {type(e).__name__}: {e}）")
check("内网行为不变（放行写入）", ok)
config.PUBLIC_MODE = True
saved_a = up.validate_and_save(_FakeUpload(_PNG), "upload", A)
check("真实会话写入自己的命名空间", f"/{A}/" in saved_a["url"].replace("\\", "/"),
      saved_a["url"])

print("\n── 5. 会话白名单仍是唯一入口（恶意值照样回 default）──")
check("路径穿越串 -> default", ident._clean_session("../../etc") == "default")
check("合法 UUID 保留", ident._clean_session("DEAD-BEEF-1234") == "dead-beef-1234")

print("\n── 6. 取图授权矩阵（/images 不再是裸静态挂载）──")
check("种子图：人人可读", ia.may_read(ROOT / "_seed" / "s1.png", A)
      and ia.may_read(ROOT / "_seed" / "s1.png", "default"))
check("家族示例图：人人可读（否则线上卡片集体裂图）",
      ia.may_read(ROOT / "examples" / "split_poster.png", A)
      and ia.may_read(ROOT / "examples" / "split_poster.png", "default"))
check("自己的图：本人可读", ia.may_read(MINE, A))
check("别人的图：不可读", not ia.may_read(THEIRS, A))
check("身份未知 + 公开版：别人的图不可读", not ia.may_read(THEIRS, "default"))
check("派生缓存 .thumbs 不外卖", not ia.may_read(ROOT / ".thumbs" / "thumb.jpg", A))
check("不存在的文件一律不可读", not ia.may_read(ROOT / A / "nope.png", A))
check("owner_of 认得命名空间", ia.owner_of(MINE) == A, ia.owner_of(MINE))

config.PUBLIC_MODE = False     # 内网：唯一用户保留旧权限
check("内网 default 仍可读全部", ia.may_read(THEIRS, "default"))
config.PUBLIC_MODE = True

print("\n── 7. 端到端 HTTP：包括 <img> 那条只能靠 Cookie 的通道 ──")
from services import context_store as _cs                             # noqa: E402

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")
_cs.SQLITE_PATH = Path(tempfile.mkdtemp(prefix="pub_iso_db_")) / "t.db"
_cs._initialized_for = None

from fastapi.testclient import TestClient                             # noqa: E402
import main as _main                                                  # noqa: E402

_client = TestClient(_main.app)

r_a = _client.get(URL_MINE, headers={"X-Session-Id": A})
check("本会话取自己的图 -> 200", r_a.status_code == 200, str(r_a.status_code))
ck_a = {"tv_session": r_a.cookies.get("tv_session", "")} if r_a.cookies else {}
check("响应种下了会话 Cookie", bool(ck_a.get("tv_session")), str(list(r_a.cookies)))
r_ck = _client.get(URL_MINE, cookies=ck_a)
check("不带头、只带 Cookie -> 200（<img> 的真实形态）", r_ck.status_code == 200, str(r_ck.status_code))
check("别人的图 -> 404", _client.get(URL_THEIRS, headers={"X-Session-Id": A}).status_code == 404)
ck_b = {"tv_session": _client.get(URL_THEIRS, headers={"X-Session-Id": B}).cookies.get("tv_session", "")}
check("B 凭自己的 Cookie 取不到 A 的图 -> 404",
      _client.get(URL_MINE, cookies=ck_b).status_code == 404)
check("种子图：无身份也可读", _client.get(URL_SEED).status_code == 200)
check("示例图：无身份也可读（端到端）",
      _client.get("/images/examples/split_poster.png").status_code == 200)
check("穿越串 -> 404 而非 400（不喂 oracle）",
      _client.get("/images/..%2F..%2Fetc%2Fpasswd").status_code == 404)
check("缩略图也按归属校验 -> 404",
      _client.get("/api/image/thumb", params={"u": URL_THEIRS, "w": 100},
                  headers={"X-Session-Id": A}).status_code == 404)
check("自己的图经 thumb -> 200",
      _client.get("/api/image/thumb", params={"u": URL_MINE, "w": 100},
                  headers={"X-Session-Id": A}).status_code == 200)

# ── 退役种子图（2026-10-05 用户要求：首屏不出现两张一样的图）────────
# 平台是"上传覆盖"语义，包里删文件线上不会消失 → 退役图仍躺在磁盘上，
# 必须靠名单在**列表层与缩略图层**一起挡掉，否则只是"看不见列表、直连还能看"。
print("\n── 5. 退役种子图：列表与缩略图都不认它 ──")
(ROOT / "_seed" / "retired.txt").write_text(
    "# 注释行\ns2.png\n\n", encoding="utf-8")
_retired_url = "/images/_seed/s2.png"
check("退役图不再出现在画廊里",
      not any(_retired_url in u for u in urls(img._gallery_sync(500, A)["items"])),
      str(urls(img._gallery_sync(500, A)["items"])))
check("退役图经 thumb -> 404",
      _client.get("/api/image/thumb", params={"u": _retired_url, "w": 100}).status_code == 404)
check("没退役的种子图不受影响",
      any(URL_SEED in u for u in urls(img._gallery_sync(500, A)["items"])))
(ROOT / "_seed" / "retired.txt").unlink()      # 还原，避免影响后续用例

print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
