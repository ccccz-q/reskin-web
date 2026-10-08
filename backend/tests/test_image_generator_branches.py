"""深水区测试：image_generator（出图主路径的关键分支）

    python tests/test_image_generator_branches.py

★★ 为什么单独写（独立审查指出："真正把项目跑起来的两块代码，
  测试覆盖反而低于项目均值 —— 看起来工程化，实际重路径靠手测"）：

`image_generator.py` 是**唯一花钱的动作**所在。
它的分支一旦出错，用户看到的是"点了没反应"或"报一个看不懂的错"。

★ 优先补**用户可感知**的分支，不为覆盖率凑数：
  · 尺寸解析的失败路径（用户会直接传错值）
  · 画幅优先 vs 档位优先的**优先级**（错了就是构图被悄悄改掉）
  · 像素下限校验（低于下限的请求会被上游拒绝，浪费一次调用）
  · 错误分类（临时错误该重试，参数错误不该重试——重试是白花钱）

零真实 API 调用，零费用。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import services.image_generator as ig        # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def _size(path, size=None, aspect=None, aspect_wins=False):
    return ig.resolve_size(str(path), size=size, aspect=aspect,
                           aspect_wins=aspect_wins)


# ── 造一张临时图，让尺寸推断有真实输入 ────────────────────────
_tmp = None
try:
    from PIL import Image
    _tmp = Path(__file__).parent / "_ig_tmp.png"
    Image.new("RGB", (2000, 1500), (120, 140, 160)).save(_tmp)
except Exception as e:                                        # noqa: BLE001
    print(f"★ 造临时图失败（跳过依赖原图的用例）：{e}")

print("[测试 1] 尺寸解析：档位与自动")
check("1k 原样返回", _size(_tmp, "1k") == "1k", _size(_tmp, "1k"))
check("大小写归一（2K → 2k）", _size(_tmp, "2K") == "2k", _size(_tmp, "2K"))
check("None → 按原图自动选档", _size(_tmp, None) in ig.VALID_SIZE_TIERS,
      _size(_tmp, None))
check("auto → 按原图自动选档", _size(_tmp, "auto") in ig.VALID_SIZE_TIERS)
check("空串→ 按原图自动选档", _size(_tmp, "") in ig.VALID_SIZE_TIERS)
check("default → 按原图自动选档", _size(_tmp, "default") in ig.VALID_SIZE_TIERS)

print("\n[测试 2] 尺寸解析：像素下限（低于下限 = 白花一次调用）")
for bad, why in [("100x100", "远低于下限"),
                 ("1x1", "极端小"),
                 ("0x100", "含 0"),
                 ("-100x100", "负值")]:
    try:
        r = _size(_tmp, bad)
        check(f"★ {bad}（{why}）应被拒", False, f"竟然返回了 {r}")
    except ValueError:
        check(f"★ {bad}（{why}）被正确拒绝", True)

ok_sz = ig.VALID_SIZE_TIERS[0]
big = ig.ASPECT_TO_PIXELS["3:4"]
try:
    r = _size(_tmp, big)
    check(f"高于下限的 {big} 通过", r == big, r)
except ValueError as e:
    check(f"高于下限的 {big} 通过", False, str(e)[:50])

print("\n[测试 3] 画幅优先 vs 档位优先（★ 优先级错 = 构图被悄悄改）")
# aspect_wins=True 时画幅应压过档位
r = _size(_tmp, size="1k", aspect="3:5", aspect_wins=True)
check("aspect_wins=True → 画幅压过档位", r == ig.ASPECT_TO_PIXELS["3:5"],
      f"{r}（档位给了 1k）")
# 默认 size 优先 —— 避免调用方"顺手传了家族默认画幅"就压掉 follow-original
r2 = _size(_tmp, size="1k", aspect="3:5", aspect_wins=False)
check("默认 → 档位优先（保住 follow-original）", r2 == "1k", f"{r2}")

print("\n[测试 4] 不支持的尺寸值（用户会直接传错）")
for bad in ["8k", "1.5k", "3k", "1024", "abc", "2kx4k"]:
    try:
        r = _size(_tmp, bad)
        check(f"★ {bad} 应被拒", False, f"返回了 {r}")
    except ValueError:
        check(f"★ {bad} 被正确拒绝", True)

print("\n[测试 5] 错误分类（★ 错判= 白花钱重试）")
transient = ig._is_transient_image_error
size_err = ig._looks_like_size_error
# 临时性错误：应判为可重试
for msg, want, why in [
    ("Read timed out. Request timed out.", True, "超时"),
    ("rate limit exceeded, please retry", True, "限流"),
    ("503 Service Unavailable", True, "服务不可用"),
    ("Internal Server Error", True, "上游 500（★ 词表原本漏了它）"),
    ("invalid size: must be 2k", False, "参数错误不该重试"),
    ("content_policy_violation", False, "内容策略不该重试"),
]:
    try:
        got = transient(Exception(msg))
    except Exception:                                         # noqa: BLE001
        got = False
    check(f"临时错误判定：{why}", got == want, f"得到 {got}")

# ★★ 误伤守卫（2026-10-09 实测踩过）：
#   我第一版修 500 时直接往词表加了裸的 "500"，
#   结果「5000ms 预算用尽」「剩余 500 次配额」「prompt 长度 5000」
#   「image 500x400 不支持」**全被误判成瞬时错误**
#   —— 它们都是确定性错误，重试只是白花钱。
#   ⇒ 裸数字子串匹配在这里太危险，已改为只认英文短语 + status_code。
#   这条守卫防的是「有人日后为了简单把它改回裸数字」。
for msg, why in [
    ("当前任务 5000ms 超时预算已用尽", "预算文案里的 5000"),
    ("配额剩余 500 次", "额度文案里的 500"),
    ("prompt 长度 5000 超限", "长度文案里的 5000"),
    ("image 500x400 不支持", "尺寸文案里的 500x400"),
]:
    try:
        got = transient(Exception(msg))
    except Exception:                                         # noqa: BLE001
        got = False
    check(f"★ 不误伤：{why}", got is False, f"得到 {got}")

# 尺寸类错误的识别（走的是专门通道：降档而不是硬失败）
check("尺寸报错能被识别", size_err("size must be 2k or 1k"))
check("非尺寸报错不被误判", not size_err("rate limit exceeded"))

print("\n[测试 6] 用户可见文案（★ 内部类名不得直出）")
for raw, must_not in [
    ("rate limit exceeded", "rate limit"),
    ("Read timed out", "timed out"),
    ("invalid size", "Traceback"),
]:
    txt = ig.friendly_error(Exception(raw))
    check(f"「{raw[:22]}」→ 中文且不含内部术语",
          bool(txt) and must_not.lower() not in txt.lower()
          and not txt.strip().lower().startswith(("traceback", "exception")),
          txt[:44])

check("字符串入参也能处理", bool(ig.friendly_error("普通错误文本")))
check("空输入不崩", bool(ig.friendly_error("")))

print("\n[测试 7] 取图 SSRF 两套判据（★ 安全项，不能退化为只查 http）")
for url, why in [("http://example.com/a.png", "公网http"),
                 ("https://example.com/a.png", "公网 https"),
                 ("http://127.0.0.1:8000/a.png", "回环地址"),
                 ("http://localhost/a.png", "localhost"),
                 ("http://169.254.169.254/latest/meta-data", "云元数据地址"),
                 ("file:///etc/passwd", "本地文件协议")]:
    try:
        ig.assert_downloadable_url(url)
        check(f"★ 拒绝 {why}", False, "竟然放行了")
    except Exception:                                         # noqa: BLE001
        check(f"★ 拒绝 {why}", True)

print("\n[测试 8] 清理")
try:
    if _tmp and _tmp.exists():
        _tmp.unlink()
        check("临时图已删除", not _tmp.exists())
except Exception as e:                                        # noqa: BLE001
    check("临时图清理", False, str(e))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)