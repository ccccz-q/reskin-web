"""后端端到端冒烟 —— TestClient 直连 ASGI，不花任何真实 API 调用"""
import io
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

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")   # 冒烟期间禁止出图
os.environ["MAX_GENERATIONS_PER_SESSION"] = "2"

import services.context_store as cs  # noqa: E402
from pathlib import Path as P  # noqa: E402

# 用系统临时目录，不要写死盘符 —— 写死的话换台机器就跑不起来
tmp = P(tempfile.mkdtemp(prefix="smoke_")) / "smoke.db"
cs.SQLITE_PATH = tmp

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402

config.SQLITE_PATH = tmp

from main import app  # noqa: E402

client = TestClient(app)

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def png_bytes(w=320, h=480, color=(70, 90, 120)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


print("=== 1. 健康检查 ===")
r = client.get("/api/health")
check("健康接口 200", r.status_code == 200, r.status_code)
data = r.json()
check("返回 ready 视图", "ready" in data)
check("返回模板清单", data.get("templates", {}).get("total", 0) >= 7,
      str(data.get("templates", {}).get("by_kind")))
check("返回治理策略", "max_generations_per_session" in data.get("governance", {}))
print("   templates:", data["templates"]["by_kind"])
print("   context:", {k: data["context"][k] for k in ("fts5_enabled", "fts_tokenizer")})

print()
print("=== 2. 家族目录（前端表单的数据源）===")
r = client.get("/api/families")
check("家族列表 200", r.status_code == 200)
fams = r.json()["items"]
# 家族数量会增长（用户可通过模板工坊安装新家族），只断言下限
check(f"家族数量 ≥ 6（实际 {len(fams)}）", len(fams) >= 6, str([f["id"] for f in fams]))
check("带 required 摘要", all("required" in f for f in fams))

r = client.get("/api/families?full=true")
full = r.json()["items"]
check("full 模式带 params", all("params" in f for f in full))

r = client.get("/api/families/second_world")
check("单个家族 200", r.status_code == 200)
sw = r.json()
check("params 带 type", all("type" in p for p in sw["params"]), str(sw["params"][:2]))
check("有 allow_change", isinstance(sw.get("allow_change"), list))

r = client.get("/api/families/no_such_family")
check("未知家族 404", r.status_code == 404, r.status_code)
check("404 里给了可用清单", "available" in r.json().get("detail", {}))

r = client.get("/api/templates")
check("模板列表 200", r.status_code == 200)
print("   模板:", [t["id"] for t in r.json()["items"]])

r = client.get("/api/sources")
check("来源列表 200", r.status_code == 200, r.json().get("count"))

print()
print("=== 3. 上传：教科书 vs 攻击 ===")
r = client.post("/api/chat/upload", files={"file": ("a.png", png_bytes(), "image/png")})
check("正常上传 200", r.status_code == 200, r.status_code)
up = r.json()
check("返回 url", up.get("url", "").startswith("/images/"), up.get("url"))
check("返回 width/height", up.get("width") == 320 and up.get("height") == 480)
img_url = up["url"]

r = client.get(img_url)
check("静态图片可访问 200", r.status_code == 200, r.status_code)

# 扩展名撒谎：其实是 PNG，却声称 .exe / .txt
r = client.post("/api/chat/upload", files={"file": ("evil.exe", png_bytes(), "image/png")})
check("扩展名撒谎也能识别", r.status_code == 200 and r.json()["url"].endswith(".png"),
      f"{r.status_code} {r.json().get('url')}")

# 真・非图片
r = client.post("/api/chat/upload", files={"file": ("x.png", b"not an image at all", "image/png")})
check("非图片被拒 400", r.status_code == 400, r.status_code)

print()
print("=== 4. 提示词渲染：travel_sketch（曾经的 P0-1 崩溃点）===")
r = client.post("/api/image/render", json={"source_id": "travel_sketch", "params": {}})
check("travel_sketch 渲染 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
if r.status_code == 200:
    d = r.json()
    check("解析到 family", d.get("family_id") == "full_restyle", d.get("family_id"))
    check("提示词非空", len(d.get("prompt") or "") > 100, str(d.get("prompt_chars") if "prompt_chars" in d else len(d["prompt"])))
    check("有三段", all(k in (d.get("segments") or {}) for k in ("preserve", "creative", "forbid")))

# 「必填参数 → 422」这条链路用一个**临时家族**来验（portrait_epic 已下线，
# 其余家族没有必填参数）。做法：往运行时模板目录写一个带必填参数的家族，
# 清缓存 → 测 422 / 补齐 200 → 删文件 → 再清缓存。不污染项目文件。
import yaml as _yaml  # noqa: E402
from services.template_manager import TEMPLATES_DIR as _TD, clear_cache as _cc  # noqa: E402

_probe = _TD / "families" / "required_probe_test.yaml"
_probe.write_text(_yaml.safe_dump({
    "kind": "family", "id": "required_probe", "name": "必填探针",
    "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {
        "school": {"type": "string", "required": True},
        "major": {"type": "string", "required": True},
    },
    "segments": {"preserve": "p", "creative": "毕业于{school}的{major}", "forbid": "f"},
}, allow_unicode=True), encoding="utf-8")
_cc()
try:
    r = client.post("/api/image/render", json={"source_id": "required_probe", "params": {}})
    check("带必填参数的家族，空参数 → 422", r.status_code == 422, r.status_code)
    # 顺序不保证（dict 遍历序），只比对集合
    check("422 带缺失清单",
          sorted(r.json()["detail"].get("missing") or []) == ["major", "school"],
          str(r.json()["detail"]))

    r = client.post("/api/image/render", json={
        "source_id": "required_probe",
        "params": {"school": "雪城大学", "major": "建筑学"},
    })
    check("补齐必填后可渲染", r.status_code == 200 and r.json().get("prompt"),
          f"{r.status_code} {r.text[:120]}")
finally:
    _probe.unlink(missing_ok=True)
    _cc()
if r.status_code == 200:
    # 真实断言（旧版这里以 `or True` 结尾，恒真，等于没测）
    prompt_text = r.json()["prompt"]
    check("必填值真的进了提示词",
          "雪城大学" in prompt_text and "建筑学" in prompt_text,
          prompt_text[:90])

print()
print("=== 4b. 提示词注入必须被挡住 ===")
r = client.post("/api/image/render", json={
    "source_id": "second_world",
    "params": {"materialize_anchor": "{hard_forbid_joined}"},
})
ok = r.status_code == 200 and "{hard_forbid" not in (r.json().get("prompt") or "")
check("参数值里的裸占位符被净化", ok, f"{r.status_code}")

print()
print("=== 4c. 越界 / 畸形图片地址必须是 4xx 而不是 500 ===")
for bad in ("/images/../../../../Windows/win.ini", "../x.png", "http://evil.com/a.png"):
    r = client.get("/api/image/size-hint", params={"image_url": bad})
    check(f"拒绝 {bad[:34]}", 400 <= r.status_code < 500, str(r.status_code))

render_plan = [("full_restyle", {}), ("zine", {}), ("split_poster", {}),
               ("second_world", {}), ("surreal_collage", {})]
for fid, params in render_plan:
    r = client.post("/api/image/render", json={"source_id": fid, "params": params})
    ok = r.status_code == 200 and bool(r.json().get("prompt"))
    check(f"家族 {fid} 可渲染", ok, "" if ok else f"{r.status_code} {r.text[:150]}")

print()
print("=== 5. 预览上传+渲染（0 成本路径）===")
r = client.post("/api/image/preview",
                files={"file": ("p.png", png_bytes(), "image/png")},
                data={"source_id": "zine", "params": "{}"})
check("preview 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
if r.status_code == 200:
    check("mode=preview", r.json().get("mode") == "preview")
    check("返回 source + segments", "source" in r.json() and "segments" in r.json())
    # ★ 漂移自检结果必须能走到 API 层（否则只是 build_prompt 内部的死数据）
    check("★ preview 响应带 preflight 字段",
          "preflight" in r.json() and isinstance(r.json()["preflight"], list),
          str(r.json().get("preflight")))
    check("★ 既有家族零漂移告警（preflight 不误报）",
          r.json().get("preflight") == [], str(r.json().get("preflight")))

r = client.post("/api/image/preview",
                files={"file": ("p.png", png_bytes(), "image/png")},
                data={"source_id": "zine", "params": "{bad json"})
check("params JSON 非法 → 400", r.status_code == 400, r.status_code)

print()
print("=== 6. 治理：出图被开关拦下 ===")
r = client.get("/api/chat/policy?thread_id=t9")
check("policy 可读", r.status_code == 200)
check("开关已生效", r.json()["generation_disabled"] is True)

r = client.post("/api/image/generate",
                files={"file": ("g.png", png_bytes(), "image/png")},
                data={"source_id": "zine", "params": "{}", "thread_id": "t9"})
check("生成被拦 403/429", r.status_code in (403, 429), r.status_code)
print("   拦截原因:", str(r.json())[:110])

print()
print("=== 7. 聊天接口（假 LLM）===")
import engine.loop as loop  # noqa: E402


def fake_chat(messages, tools=None, tool_choice="auto", temperature=0.2):
    last = messages[-1]
    if last.get("role") == "tool":
        return {"content": "我看了工具结果。", "tool_calls": [], "usage": {}, "finish_reason": "stop"}
    return {"content": "", "tool_calls": [
        {"id": "c1", "name": "list_families", "arguments": {}},
    ], "usage": {"total_tokens": 5}, "finish_reason": "tool_calls"}


loop.chat_with_tools = fake_chat
r = client.post("/api/chat", json={"message": "帮我选个风格", "thread_id": "s1",
                                   "image_url": img_url, "allow_spend": False})
check("对话 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
if r.status_code == 200:
    d = r.json()
    check("有 reply", bool(d.get("reply")), repr(d.get("reply")[:30]))
    check("stopped_reason=completed", d.get("stopped_reason") == "completed")
    check("记录了工具调用", len(d.get("tool_events", [])) >= 1, str(d.get("tool_events")))

r = client.get("/api/chat/history?thread_id=s1")
check("历史可读", r.status_code == 200 and r.json()["count"] > 0, str(r.json().get("count")))

r = client.get("/api/chat/search?thread_id=s1&q=%E9%A3%8E%E6%A0%BC")
check("全文检索 200", r.status_code == 200, r.status_code)

print()
print("=== 8. 空消息应 422 ===")
r = client.post("/api/chat", json={"message": ""})
check("空消息被拒", r.status_code == 422, r.status_code)

print()
print("=== 9. 审计日志 ===")
r = client.get("/api/audit?limit=5")
check("审计可读", r.status_code == 200 and isinstance(r.json()["events"], list))

print()
print("=== 10. 坏 YAML 隔离：一个文件坏不能拖垮整批 ===")
# 造一个临时模板目录：3 个健康家族 + 1 个语法坏掉的 + 1 个缺字段的
import shutil  # noqa: E402
import tempfile  # noqa: E402
from services import template_manager as tm  # noqa: E402

src_dir = Path(tm.TEMPLATES_DIR) / "families"
tmp_dir = Path(tempfile.mkdtemp(prefix="tmpl_"))
(tmp_dir / "families").mkdir(parents=True)
for name in ("full_restyle.yaml", "zine.yaml", "second_world.yaml"):
    shutil.copy2(src_dir / name, tmp_dir / "families" / name)
(tmp_dir / "families" / "broken_syntax.yaml").write_text(
    "id: broken\nsegments: [unclosed\n", encoding="utf-8")
(tmp_dir / "families" / "missing_fields.yaml").write_text(
    "kind: family\nid: incomplete\nlayout: full\n", encoding="utf-8")

orig = tm.TEMPLATES_DIR
try:
    tm.TEMPLATES_DIR = tmp_dir
    tm.clear_cache()
    fams = tm.load_families()
    errs = tm.load_errors()
    check("健康家族仍全部可用", len(fams) == 3, f"实际 {len(fams)}: {[f['id'] for f in fams]}")
    check("坏文件被记录成错误", len(errs) == 2, str([e['file'] for e in errs]))
    check("错误信息含文件名", any("broken_syntax" in e["file"] for e in errs))
    check("错误信息含原因", all(e["error"] for e in errs))
    inv = tm.inventory()
    check("inventory 暴露 healthy=False", inv["healthy"] is False)
    check("inventory 仍返回可用家族", len(inv["by_kind"].get("family", [])) == 3)
finally:
    tm.TEMPLATES_DIR = orig
    tm.clear_cache()
    tm.load_families()          # 恢复真实目录的缓存

check("恢复后无错误", tm.load_errors() == [], str(tm.load_errors()))
check(f"恢复后家族数量不变（实际 {len(tm.load_families())}）",
      len(tm.load_families()) >= 6)

print()
print("=== 12. ★ 参数中文名必须由后端提供（前端不该有第二份真源）===")
r = client.get("/api/families", params={"full": True})
fams_full = r.json()["items"]
check(f"拿到完整 schema（{len(fams_full)} 个）", len(fams_full) >= 6)

unlabeled_p, unlabeled_o = [], []
for f in fams_full:
    for p in f["params"]:
        # 后端没给 label 时前端会退回显示英文参数名 —— 那正是用户看到的问题
        if not p.get("label") or p["label"] == p["name"]:
            unlabeled_p.append(f"{f['id']}.{p['name']}")
        for opt in p.get("options") or []:
            ol = (p.get("option_labels") or {}).get(opt)
            if not ol or ol == opt:
                unlabeled_o.append(f"{f['id']}.{p['name']}={opt}")
check("★ 所有参数都有中文名", not unlabeled_p, str(unlabeled_p[:6]))
# 选项里有些确实该保持原样（4K、1:1 这种），所以只要「大部分被翻译」即可，
# 但明确列出来让缺失可追溯
translated = sum(
    len([o for o in (p.get("options") or [])
         if (p.get("option_labels") or {}).get(o, o) != o])
    for f in fams_full for p in f["params"]
)
total_opts = sum(len(p.get("options") or []) for f in fams_full for p in f["params"])
check("选项中文名覆盖充分", translated >= total_opts * 0.85,
      f"{translated}/{total_opts} 已翻译；未翻：{unlabeled_o[:5]}")

# `none` 是语境相关的典型：同一个 token 在三个参数下必须是三种意思
by_key = {}
for f in fams_full:
    for p in f["params"]:
        for opt in p.get("options") or []:
            if opt == "none":
                by_key[p["name"]] = (p.get("option_labels") or {}).get("none")
check("★ none 按参数区分（不是一刀切）",
      len({v for v in by_key.values() if v}) >= 2,
      str(by_key))

print()
print("=== 13. ★ 用户自定义提示词 ===")
base = client.post("/api/image/render",
                   json={"source_id": "zine", "params": {}}).json()
check("不带自定义提示词时 extra_applied 为空", not base.get("extra_applied"),
      str(base.get("extra_applied")))

app1 = client.post("/api/image/render", json={
    "source_id": "zine", "params": {},
    "extra_prompt": "天空压暗成靛青，加一层胶片颗粒",
}).json()
check("追加模式：creative 含追加要求",
      "天空压暗成靛青" in (app1["segments"]["creative"] or ""))
check("追加模式：标记了「用户追加要求」",
      "【用户追加要求】" in (app1["segments"]["creative"] or ""))
check("追加模式：给出了落地说明", bool(app1.get("extra_applied")),
      str(app1.get("extra_applied")))
check("追加模式：preserve 未被破坏", bool(app1["segments"]["preserve"]))
check("★ 追加模式：forbid 仍然存在（保真底线不因自定义而失效）",
      bool(app1["segments"]["forbid"]) and app1["segments"]["forbid"] == base["segments"]["forbid"])

rep = client.post("/api/image/render", json={
    "source_id": "zine", "params": {},
    "extra_prompt": "雨夜霓虹街道，主角撑透明伞",
    "extra_mode": "replace",
}).json()
check("替换模式：creative 被替换", (rep["segments"]["creative"] or "").startswith("雨夜霓虹街道"))
check("替换模式：不再包含家族原创作描述",
      "纸张" not in (rep["segments"]["creative"] or "")[:200])
check("★ 替换模式：preserve 仍然保留", bool(rep["segments"]["preserve"]))
check("★ 替换模式：forbid 仍然保留",
      rep["segments"]["forbid"] == base["segments"]["forbid"])
check("替换模式：落地说明措辞不同",
      rep.get("extra_applied") != app1.get("extra_applied"),
      str(rep.get("extra_applied")))

# 自定义提示词与 params 一样是用户可控的 —— 必须过同一个净化闸口
inject = client.post("/api/image/render", json={
    "source_id": "zine", "params": {},
    "extra_prompt": "{hard_forbid_joined} {dynamic_forbid}",
}).json()
inj_text = inject["segments"]["creative"] or ""
check("★ 自定义提示词里的裸占位符被剥除",
      "{hard_forbid_joined}" not in inj_text and "{dynamic_forbid}" not in inj_text,
      repr(inj_text[-60:]))
check("注入尝试没有把 forbid 段搬进 creative",
      inject["segments"]["forbid"] != inj_text)

# 纯空白等于没填
blank = client.post("/api/image/render", json={
    "source_id": "zine", "params": {}, "extra_prompt": "   \n  ",
}).json()
check("纯空白视为未填", not blank.get("extra_applied"))

print()
print("=== 14. /render 与 /preview 都不返回绝对路径 ===")
check("render 响应无绝对路径",
      "C:\\" not in json.dumps(base, ensure_ascii=False))

print()
print("=== 15. 缩略图端点（磁盘缓存 + 白名单校验）===")
# ★ 自己造一张种子图再测（2026-10-05 开源整理时实测）：
#   旧写法直接读仓库里的种子图，于是"仓库没带种子图"（开源版的刻意选择）
#   会让这条用例失败 —— 测试不该依赖仓库数据。
#   造完即删，不留痕迹。
from PIL import Image as _PILImage                                  # noqa: E402
_seed_dir = config.IMAGE_STORAGE_DIR / "_seed"
_seed_dir.mkdir(parents=True, exist_ok=True)
_probe_seed = _seed_dir / "_thumb_probe.png"
_PILImage.new("RGB", (240, 180), (120, 140, 160)).save(_probe_seed)
_seeds = client.get("/api/image/gallery?limit=10&kinds=seed").json()["items"]
check("种子图可用作缩略图素材", len(_seeds) > 0)
_u = _seeds[0]["url"]
_rt = client.get(f"/api/image/thumb?u={_u}&w=320")
check("缩略图 200", _rt.status_code == 200, _rt.status_code)
check("返回 JPEG", _rt.headers.get("content-type") == "image/jpeg", _rt.headers.get("content-type"))
check("体积远小于原图", len(_rt.content) < 400_000, f"{len(_rt.content)} 字节")
_rt2 = client.get(f"/api/image/thumb?u={_u}&w=320")
check("第二次命中磁盘缓存",
      _rt2.status_code == 200 and len(_rt2.content) == len(_rt.content))
_bad = client.get("/api/image/thumb?u=../../etc/passwd&w=320")
check("路径穿越被拦", _bad.status_code == 400, _bad.status_code)
_probe_seed.unlink(missing_ok=True)   # 造完即删，仓库/存储不留痕

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")

# ★ 必须用退出码报告失败 —— 只 print 不 exit 的话，
#   run_all.py 永远看到 returncode=0，整套自测就会假绿。
sys.exit(1 if FAIL else 0)
