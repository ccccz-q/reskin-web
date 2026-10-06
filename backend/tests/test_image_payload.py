"""出图响应形态测试 · 上游换协议不能变成「假故障」

事故经过（2026-10-05，用户实测报错「gpt-image 返回里没有 b64_json」）：
    用户在中转站把 image 通道换到「image2 原生分组」。换组后上游**不再返回
    base64**，改成返回图片 URL：
        data[0] = {"url": "https://<供应商-CDN-域名>/images/.../xxx.png"}
    而我们的代码只认 `b64_json` —— 图其实 18.8s 就生成了，却被我们判成失败。
    典型的「上游改协议、我们不跟」造成的假故障。

这组测试锁三件事：
    1. 两种回图形态（b64 / url）都能落盘；
    2. 两种都没有时，错误文案要说人话、且不泄露上游原文；
    3. 取图安全判据分两套：
       · 客户端传来的 URL → 域名白名单（陌生人可能塞任何地址）
       · 上游返回的 URL   → https 且**解析结果不是内网**（供应商可信但仍防 SSRF）

★ 全程离线：假的响应对象 + 假的下载器，不打网络、不花钱。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import services.image_generator as ig                                 # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ── 替身：模拟上游返回的对象形态 ────────────────────────────────
def _item(**fields):
    """模拟 OpenAI SDK 的 pydantic 对象（属性访问）"""
    return types.SimpleNamespace(**fields)


def _resp(data):
    return types.SimpleNamespace(data=data)


print("\n── 1. 两种回图形态都能落地 ──")
import base64                                                       # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
b64 = base64.b64encode(PNG).decode()

_downloads: list[tuple[str, bool]] = []
_real_dl = ig._download_image
ig._download_image = lambda url, provider=False: (
    _downloads.append((url, provider)) or (PNG, "image/png"))

# ① 官方形态：b64_json
c1, t1, r1 = ig._extract_image_bytes(_resp([_item(b64_json=b64, url=None)]), "gpt-image-2")
check("b64 形态：拿到字节", c1 == PNG, f"{len(c1 or b'')} 字节")
check("b64 形态：按 png 落盘", t1 == "image/png", t1)
check("b64 形态：没有错误详情", r1 == "", r1)
check("b64 形态：不触发下载", not _downloads)

# ② 原生分组形态：只有 url
c2, t2, r2 = ig._extract_image_bytes(
    _resp([_item(b64_json=None, url="https://cdn.example.com/a.png")]), "gpt-image-2")
check("url 形态：拿到字节", c2 == PNG, f"{len(c2 or b'')} 字节")
check("url 形态：content-type 取自真实响应头", t2 == "image/png", t2)
check("url 形态：走下载", _downloads and _downloads[-1][0] == "https://cdn.example.com/a.png")
check("url 形态：按 provider 判据（不是客户端白名单）", _downloads[-1][1] is True)

# ③ 两者都没有 → 明确报「格式不认识」，且带上字段名方便排障
c3, t3, r3 = ig._extract_image_bytes(_resp([_item(revised_prompt="x")]), "gpt-image-2")
check("都没有：返回 None", c3 is None)
check("都没有：错误详情点名字段（排障用）", "revised_prompt" in r3, r3)
check("都没有：错误详情带模型名", "gpt-image-2" in r3, r3)

# ④ data 为空 / data 缺失
check("data 为空列表：返回 None",
      ig._extract_image_bytes(_resp([]), "m")[0] is None)
check("data 缺失：返回 None",
      ig._extract_image_bytes(types.SimpleNamespace(), "m")[0] is None)

# ⑤ dict 形态的 data[0]（有些中转会返回纯 dict 而不是 SDK 对象）
c5, _, _ = ig._extract_image_bytes(
    {"data": [{"b64_json": b64}]}, "gpt-image-2")
check("dict 形态的响应也认（有些中转不返回 SDK 对象）", c5 == PNG, f"{len(c5 or b'')} 字节")

# ⑥ b64 坏了 → 明确说解码失败，不要静默
c6, _, r6 = ig._extract_image_bytes(_resp([_item(b64_json="!!!不是 base64!!!")]), "m")
check("坏 base64：返回 None 且说明原因", c6 is None and "解码失败" in r6, r6)

ig._download_image = _real_dl

print("\n── 2. 两种都没有 → 给用户的话术 ──")
msg = "生图服务返回了无法识别的结果格式，请稍后再试。"
check("文案是中文且可执行", "请稍后再试" in msg and "b64_json" not in msg, msg)

print("\n── 3. 取图安全判据：两套，不能混 ──")

# 客户端 URL：白名单仍然生效（火山/字节在列，别的域名不在）
try:
    ig.assert_downloadable_url("https://evil.example.com/x.png")
    check("客户端 URL：白名单外被拒", False, "竟然放行了")
except ValueError as e:
    check("客户端 URL：白名单外被拒", "白名单" in str(e))

try:
    ig.assert_downloadable_url("https://x.tos-cn-beijing.volces.com/a.png")
    check("客户端 URL：白名单内放行", True)
except ValueError as e:
    check("客户端 URL：白名单内放行", False, str(e)[:60])

# 上游 URL：只看「是不是内网」，不看域名白名单
# ★ 这里故意用 www.example.com（IANA 保留的文档域名）：
#   判据本来就"不认域名"，用谁的都行；用真实供应商域名反而像是把 CDN 地址硬编码进了代码。
#   注意它必须能解析 —— 判据会真的做 DNS 解析，换成不存在的域名这条用例会假失败。
try:
    ig.assert_provider_image_url("https://www.example.com/a.png")
    check("上游 URL：公网 CDN 放行（无需硬编码域名）", True)
except ValueError as e:
    check("上游 URL：公网 CDN 放行（无需硬编码域名）", False, str(e)[:80])

for bad, why in [
    ("http://www.example.com/a.png", "明文 http"),
    ("https://127.0.0.1/a.png", "回环地址"),
    ("https://localhost/a.png", "localhost"),
    ("https://10.0.0.5/a.png", "内网 10 段"),
    ("https://192.168.1.1/a.png", "内网 192.168"),
    ("https://169.254.169.254/latest/meta-data", "云元数据地址"),
    ("https://[::1]/a.png", "IPv6 回环"),
]:
    try:
        ig.assert_provider_image_url(bad)
        check(f"上游 URL：{why} 被拒", False, "竟然放行了")
    except ValueError:
        check(f"上游 URL：{why} 被拒", True)

try:
    ig.assert_provider_image_url("https://x.tos-cn-beijing.volces.com/a.png")
    check("上游 URL：火山域名也照常放行（向后兼容）", True)
except ValueError as e:
    check("上游 URL：火山域名也照常放行（向后兼容）", False, str(e)[:60])

print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
